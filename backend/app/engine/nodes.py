import asyncio
import json as _json
import random
import re

from app.models import ModelConfig
from app.services import llm


def _dedup(rows, op, rng):
    cols = op.get("columns") or []
    all_cols = cols or sorted({k for r in rows for k in r})
    seen, out = set(), []
    for row in rows:
        key = tuple(str(row.get(c)) for c in all_cols)
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def _filter(rows, op, rng):
    col, mode, value = op["column"], op["mode"], op["value"]

    def keep(row) -> bool:
        s = str(row.get(col, ""))
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
        raise ValueError(f"未知过滤模式: {mode}")

    return [r for r in rows if keep(r)]


def _rename(rows, op, rng):
    mapping = op["mapping"]
    return [{mapping.get(k, k): v for k, v in r.items()} for r in rows]


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


def apply_operations(rows: list[dict], operations: list[dict], seed: int | None = None) -> list[dict]:
    rng = random.Random(seed)
    for op in operations:
        fn = _OPS.get(op.get("op"))
        if fn is None:
            raise ValueError(f"未知操作: {op.get('op')}")
        rows = fn(rows, op, rng)
    return rows


TEMPLATE_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def render_template(template: str, row: dict) -> str:
    return TEMPLATE_RE.sub(lambda m: str(row.get(m.group(1), "")), template)


async def run_llm_synth_row(config: dict, row: dict, mc: ModelConfig,
                            user_sem: asyncio.Semaphore) -> tuple[list[dict], dict]:
    """处理一条输入行：扇出 fanout_n 次调用，返回 (输出行列表, usage 汇总)。失败抛异常由 runner 记为行失败。"""
    system = render_template(config.get("system_prompt", ""), row)
    user = render_template(config.get("user_prompt", ""), row)
    params = config.get("params", {})
    retries = config.get("retries", 3)
    fanout = config.get("fanout_n", 1)

    async def one() -> tuple[str, dict]:
        async with user_sem:
            return await llm.chat(mc, system, user, params=params, retries=retries)

    results = await asyncio.gather(*[one() for _ in range(fanout)])

    out_rows: list[dict] = []
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    for text, usage in results:
        usage_total["prompt_tokens"] += usage["prompt_tokens"]
        usage_total["completion_tokens"] += usage["completion_tokens"]
        if config.get("output_mode") == "json":
            parsed = _json.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError("LLM 返回的不是 JSON 对象")
            out_rows.append({**row, **parsed})
        else:
            out_rows.append({**row, config.get("output_column", "output"): text})
    return out_rows, usage_total
