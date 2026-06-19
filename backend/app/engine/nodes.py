import asyncio
import json as _json
import random
import re

from app.agent.prompts import load_prompt
from app.engine import pycode
from app.models import ModelConfig
from app.services import http, llm

QC_EMPTY_ANCHOR = "\n\n" + load_prompt("qc_empty_anchor.md")


def _dedup(rows, op, rng):
    cols = op.get("columns") or []
    all_cols = cols or sorted({k for r in rows for k in r})
    seen, out = set(), []
    for row in rows:
        # 用 JSON 串作键：None→"null" 与字符串 "None" 不撞键，且兼容嵌套（list/dict）值。
        key = tuple(_json.dumps(row.get(c), ensure_ascii=False, sort_keys=True) for c in all_cols)
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def _predicate(s: str, mode: str, value) -> bool:
    if mode == "min_len":
        return len(s) >= value
    if mode == "max_len":
        return len(s) <= value
    if mode == "contains":
        return value in s
    if mode == "not_contains":
        return value not in s
    if mode == "regex":
        return re.search(value, s) is not None
    if mode == "not_empty":
        return s.strip() != ""
    if mode == "equals":
        return s == str(value)
    raise ValueError(f"未知判定模式: {mode}")


def _filter(rows, op, rng):
    col, mode, value = op["column"], op["mode"], op["value"]
    return [r for r in rows if _predicate(str(r.get(col, "")), mode, value)]


def _rename(rows, op, rng):
    mapping = op["mapping"]
    out = []
    for r in rows:
        new_keys = [mapping.get(k, k) for k in r]
        if len(set(new_keys)) != len(new_keys):   # 多列映射到同名：报错点名，不静默后写覆盖丢列
            dup = sorted({k for k in new_keys if new_keys.count(k) > 1})
            raise ValueError(f"rename 列名冲突：多列映射到同名 {dup}（请改用不同目标列名）")
        out.append(dict(zip(new_keys, r.values())))
    return out


def _drop(rows, op, rng):
    cols = set(op["columns"])
    return [{k: v for k, v in r.items() if k not in cols} for r in rows]


def _concat(rows, op, rng):
    sep = op.get("sep", "")
    return [{**r, op["target"]: sep.join(str(r.get(c, "")) for c in op["columns"])} for r in rows]


def _cast(rows, op, rng):
    col = op["column"]
    caster = {"str": str, "int": int, "float": float}[op["to"]]

    def cast_one(value):
        if value is None:
            raise ValueError(f"类型转换: 列 '{col}' 存在缺失值")
        return caster(value)

    return [{**r, col: cast_one(r.get(col))} for r in rows]


def _sample(rows, op, rng):
    n = op["n"]
    return rows if n >= len(rows) else rng.sample(rows, n)


def _shuffle(rows, op, rng):
    out = list(rows)
    rng.shuffle(out)
    return out


_OPS = {"dedup": _dedup, "filter": _filter, "rename": _rename, "drop": _drop,
        "concat": _concat, "cast": _cast, "sample": _sample, "shuffle": _shuffle}


def _apply_one(rows: list[dict], op: dict, rng) -> list[dict]:
    fn = _OPS.get(op.get("op"))
    if fn is None:
        raise ValueError(f"未知操作: {op.get('op')}")
    return fn(rows, op, rng)


def apply_operations(rows: list[dict], operations: list[dict], seed: int | None = None) -> list[dict]:
    rng = random.Random(seed)
    for op in operations:
        rows = _apply_one(rows, op, rng)
    return rows


async def apply_operations_with_agent(rows: list[dict], operations: list[dict],
                                      seed: int | None = None) -> list[dict]:
    """同 apply_operations，但支持 {"op": "agent", "code": ...}（子进程执行固化代码）。"""
    rng = random.Random(seed)
    for op in operations:
        if op.get("op") == "agent":
            rows = await pycode.run_process_code(op.get("code") or "", rows)
        else:
            rows = _apply_one(rows, op, rng)
    return rows


TEMPLATE_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def render_template(template: str, row: dict) -> str:
    # 缺失值（CSV 空单元格 → NaN → None）渲染成空串，而非字面量 "None" 污染提示词。
    return TEMPLATE_RE.sub(lambda m: _cell(row.get(m.group(1))), template)


def _cell(v) -> str:
    return "" if v is None else str(v)


def json_path_get(obj, path: str):
    """点号路径取值：data.weather.0.desc —— 数字段对 list 当索引、对 dict 当键。
    任一级类型不符或缺失返回 None（落列时再归一成空串）。不支持通配/过滤（YAGNI）。"""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            if not part.lstrip("-").isdigit():
                return None
            idx = int(part)
            if not -len(cur) <= idx < len(cur):
                return None
            cur = cur[idx]
        elif isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        else:
            return None
    return cur


# 运行期注入的内部 QC 簿记列：渲染/判定/落库前剔除。只剔这两个确切键——
# 用旧的 startswith("_qc") 会把用户同前缀列（如 _qc_score）一并静默吃掉。
_QC_INTERNAL_KEYS = ("_qc_reason", "_qc_per_model")


def strip_qc_internal(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in _QC_INTERNAL_KEYS}


async def run_llm_synth_row(config: dict, row: dict, mc: ModelConfig,
                            user_sem: asyncio.Semaphore) -> tuple[list[dict], dict]:
    """处理一条输入行：扇出 fanout_n 次调用，返回 (输出行列表, usage 汇总)。失败抛异常由 runner 记为行失败。"""
    base = strip_qc_internal(row)
    system = render_template(config.get("system_prompt", ""), base)
    user = render_template(config.get("user_prompt", ""), base)
    if row.get("_qc_reason"):
        user += f"\n\n上一轮质检未通过，原因：{row['_qc_reason']}\n请针对此改进后重新生成。"
    params = config.get("params", {})
    retries = config.get("retries", 3)
    fanout = config.get("fanout_n", 1)
    if not isinstance(fanout, int) or fanout < 1:   # <1 会 range(0) 静默产出 0 行却记 done，输入行凭空丢失
        raise ValueError(f"fanout_n 必须为 ≥1 的整数，当前为 {fanout!r}")

    async def one() -> tuple[str, dict]:
        async with user_sem:
            return await llm.chat(mc, system, user, params=params, retries=retries)

    tasks = [asyncio.create_task(one()) for _ in range(fanout)]
    try:
        results = await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:  # 首个异常即取消兄弟任务，立刻释放用户信号量槽位
            t.cancel()
        raise

    out_rows: list[dict] = []
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    for text, usage in results:
        usage_total["prompt_tokens"] += usage["prompt_tokens"]
        usage_total["completion_tokens"] += usage["completion_tokens"]
        if config.get("output_mode") == "json":
            parsed = _json.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError("LLM 返回的不是 JSON 对象")
            out_rows.append({**base, **parsed})
        else:
            # falsy 兜底 'output'（与 columns.py 血缘一致）：空串 output_column 不应落进无名 '' 列
            out_rows.append({**base, (config.get("output_column") or "output"): text})
    return out_rows, usage_total


async def run_http_fetch_row(config: dict, row: dict) -> tuple[list[dict], dict]:
    """处理一条输入行：渲染 url/headers/body 后调接口，按 extract 的 JSON 路径提取落列。
    返回 (输出行列表, 空 usage)。请求失败/响应非 JSON 抛异常由 runner 记为行失败（逐行隔离）。"""
    base = strip_qc_internal(row)
    method = config.get("method", "GET")
    url = render_template(config.get("url", ""), base)
    headers = {k: render_template(str(v), base) for k, v in (config.get("headers") or {}).items()}
    body = render_template(config["body"], base) if config.get("body") else None
    status, text = await http.fetch(method, url, headers=headers, body=body,
                                    timeout=config.get("timeout", 30), retries=config.get("retries", 2))
    try:
        data = _json.loads(text)
    except (ValueError, TypeError):
        raise ValueError(f"接口响应非 JSON，无法提取（HTTP {status} {url}）")
    extracted = {}
    for col, path in (config.get("extract") or {}).items():
        v = json_path_get(data, path)
        extracted[col] = "" if v is None else v   # 字段缺失→空串（同 render 缺失语义），非缺失保原类型
    return [{**base, **extracted}], {}


async def run_qc_judge_row(config: dict, row: dict, mcs: list[ModelConfig], pass_k: int,
                           user_sem: asyncio.Semaphore) -> tuple[bool, str, dict, list]:
    """多模型 K-of-N 质检判定：N 个模型共用提示词并发判定，≥pass_k 个通过即整行通过。
    返回 (是否通过, 聚合理由, usage 汇总, per_model 列表)。"""
    base = strip_qc_internal(row)
    if not any(str(v).strip() for v in base.values()):   # 空/全空白样本：直接判不通过，不调 judge
        return False, "样本内容为空", {"prompt_tokens": 0, "completion_tokens": 0}, []
    system = render_template(config.get("system_prompt", ""), base) + QC_EMPTY_ANCHOR
    user = render_template(config.get("user_prompt", ""), base)
    params = {"temperature": 0, **config.get("params", {}), "json_mode": True}
    retries = config.get("retries", 3)

    async def judge_one(mc: ModelConfig):
        """单模型判定：报错/非 JSON/缺 status/空回复/限流等一律重试，retries 次仍失败即投「不通过」票。
        绝不向上抛——一行里某个判定模型抽风不该拖垮整个质检节点与 run（对照 llm_synth 的逐行隔离）。"""
        usage_acc = {"prompt_tokens": 0, "completion_tokens": 0}
        last_err = None
        for attempt in range(retries):
            try:
                async with user_sem:
                    text, usage = await llm.chat(mc, system, user, params=params, retries=1)
                usage_acc["prompt_tokens"] += usage["prompt_tokens"]
                usage_acc["completion_tokens"] += usage["completion_tokens"]
                verdict = _json.loads(text)
                if "status" not in verdict:
                    raise ValueError("判定未返回 status 字段")
                return mc.id, str(verdict["status"]).strip(), str(verdict.get("reason") or "未通过质检"), usage_acc
            except Exception as e:
                last_err = e
                if attempt < retries - 1:
                    await asyncio.sleep(llm.BACKOFF_BASE * 2 ** attempt)
        return mc.id, "failed", f"判定重试 {retries} 次仍失败：{last_err}", usage_acc

    results = await asyncio.gather(*[judge_one(mc) for mc in mcs])
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    per_model, n_pass, dissent = [], 0, []
    for mc_id, status, reason, usage in results:
        usage_total["prompt_tokens"] += usage["prompt_tokens"]
        usage_total["completion_tokens"] += usage["completion_tokens"]
        per_model.append({"model_config_id": mc_id, "status": status, "reason": reason})
        if status.lower() == "pass":
            n_pass += 1
        else:
            dissent.append(reason)
    return n_pass >= pass_k, ("；".join(dissent) if dissent else "通过"), usage_total, per_model
