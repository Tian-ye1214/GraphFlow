"""gf 公共：HTTP 客户端、资源解析、参数转换、常量表。"""
import json
import sys
import time
from pathlib import Path

import httpx

from app.cli import load_state, save_state, STATE_FILE  # noqa: F401  状态原语由包顶层提供

NODE_TYPES = {"input": "input", "llm": "llm_synth", "auto": "auto_process", "output": "output",
              "qc": "qc", "llm_synth": "llm_synth", "auto_process": "auto_process",
              "http": "http_fetch", "http_fetch": "http_fetch"}
NODE_LABELS = {"input": "输入", "llm_synth": "LLM 合成", "auto_process": "自动处理",
               "output": "输出", "qc": "质检", "http_fetch": "HTTP 取数"}
KIND_LABELS = {"workflows": "工作流", "datasets": "数据集", "models": "模型配置"}
STATUS_LABELS = {"queued": "排队中", "running": "运行中", "completed": "已完成",
                 "failed": "失败", "cancelled": "已取消", "pending": "等待", "done": "完成"}


def die(msg: str):
    print(msg, file=sys.stderr)
    sys.exit(1)


class Cli:
    def __init__(self):
        self.state = load_state()
        if not self.state.get("cookie"):
            die("未登录，先执行: gf login <用户名>")
        # trust_env=False：gf 访问用户显式指定的服务器（常为本地），绝不走系统代理
        # （否则开了 Clash 等系统代理时，127.0.0.1 请求被代理拦截返回 502）。
        self.http = httpx.Client(base_url=self.state["server"], trust_env=False,
                                 cookies={"gf_session": self.state["cookie"]}, timeout=30)

    def check(self, r: httpx.Response) -> httpx.Response:
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            die(str(detail))
        return r

    def req(self, method: str, path: str, **kw):
        return self.check(self.http.request(method, path, **kw)).json()

    def resolve(self, kind: str, ref: str) -> int:
        """纯数字按 ID，否则按名字精确匹配。kind: workflows/datasets/models。"""
        if ref.isdigit():
            return int(ref)
        hits = [i for i in self.req("GET", f"/api/{kind}") if i["name"] == ref]
        if len(hits) == 1:
            return hits[0]["id"]
        if not hits:
            die(f"找不到名为「{ref}」的{KIND_LABELS[kind]}")
        die(f"「{ref}」有 {len(hits)} 个同名项，请改用 ID: {[h['id'] for h in hits]}")

    def current_wf(self) -> int:
        wf_id = self.state.get("workflow_id")
        if not wf_id:
            die("未选择工作流，先执行: gf use <名|ID>")
        return wf_id

    def get_wf(self) -> dict:
        return self.req("GET", f"/api/workflows/{self.current_wf()}")

    def put_graph(self, wf_id: int, graph: dict) -> None:
        self.req("PUT", f"/api/workflows/{wf_id}", json={"graph": graph})


def summarize(n: dict) -> str:
    c = n["config"]
    if n["type"] == "input":
        return f"数据集 {c.get('dataset_ids', [])}"
    if n["type"] == "llm_synth":
        return f"模型 #{c.get('model_config_id', '?')} -> {c.get('output_column', 'output')}"
    if n["type"] == "auto_process":
        return f"{len(c.get('operations', []))} 个操作"
    if n["type"] == "http_fetch":
        return f"{c.get('method', 'GET')} {c.get('url', '?')} -> {list((c.get('extract') or {}).keys())}"
    return f"保存为数据集「{c['dataset_name']}」" if c.get("save_as_dataset") else ""


LLM_CONFIG_KEYS = {"system": "system_prompt", "prompt": "user_prompt", "out": "output_column",
                   "mode": "output_mode", "fanout": "fanout_n", "conc": "concurrency",
                   "retries": "retries"}
LLM_PARAM_KEYS = {"temp": "temperature", "top_p": "top_p", "max_tokens": "max_tokens",
                  "timeout": "timeout", "json_mode": "json_mode"}
INT_KEYS = {"fanout_n", "concurrency", "retries", "max_tokens", "timeout"}
FLOAT_KEYS = {"temperature", "top_p"}
HTTP_STR_KEYS = {"url", "method", "body"}


def convert(field: str, v: str):
    if field in INT_KEYS:
        return int(v)
    if field in FLOAT_KEYS:
        return float(v)
    if field == "json_mode":
        return v.lower() in ("true", "1", "yes")
    return v


def parse_kv(pairs: list[str]) -> dict:
    out = {}
    for p in pairs:
        if "=" not in p:
            die(f"参数格式应为 key=value: {p}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def find_node(graph: dict, node_id: str) -> dict:
    for n in graph["nodes"]:
        if n["id"] == node_id:
            return n
    die(f"节点 {node_id} 不存在")


OP_LABELS = {"dedup": "去重", "filter": "过滤", "rename": "重命名", "drop": "删除列",
             "concat": "拼接列", "cast": "类型转换", "sample": "随机采样", "shuffle": "打乱"}


def build_op(op: str, params: list[str]) -> dict:
    if op == "dedup":
        return {"op": "dedup", "columns": params[0].split(",") if params else []}
    if op == "filter":
        if len(params) != 3:
            die("用法: gf op add <节点> filter <列> <min_len|max_len|contains|not_contains|regex> <值>")
        col, mode, value = params
        return {"op": "filter", "column": col, "mode": mode,
                "value": int(value) if mode in ("min_len", "max_len") else value}
    if op == "rename":
        if len(params) != 2:
            die("用法: gf op add <节点> rename <原列> <新列>")
        return {"op": "rename", "mapping": {params[0]: params[1]}}
    if op == "drop":
        if len(params) != 1:
            die("用法: gf op add <节点> drop <列1,列2>")
        return {"op": "drop", "columns": params[0].split(",")}
    if op == "concat":
        if len(params) < 2:
            die("用法: gf op add <节点> concat <列1,列2> <目标列> [分隔符]")
        return {"op": "concat", "columns": params[0].split(","), "target": params[1],
                "sep": params[2] if len(params) > 2 else ""}
    if op == "cast":
        if len(params) != 2 or params[1] not in ("str", "int", "float"):
            die("用法: gf op add <节点> cast <列> <str|int|float>")
        return {"op": "cast", "column": params[0], "to": params[1]}
    if op == "sample":
        if len(params) != 1:
            die("用法: gf op add <节点> sample <n>")
        return {"op": "sample", "n": int(params[0])}
    if op == "shuffle":
        return {"op": "shuffle"}
    die(f"未知操作 {op}（可选: dedup/filter/rename/drop/concat/cast/sample/shuffle）")


def _auto_node(cli: Cli, node_id: str) -> tuple[dict, list]:
    wf = cli.get_wf()
    node = find_node(wf["graph"], node_id)
    if node["type"] != "auto_process":
        die(f"{node_id} 不是自动处理节点")
    return wf, node["config"].setdefault("operations", [])


MODEL_KEYS = {"name": "name", "model": "model_name", "url": "base_url", "key": "api_key",
              "provider": "provider", "api_version": "api_version", "version": "api_version"}


def watch_run(cli: Cli, run_id: int):
    lines = 0
    while True:
        d = cli.req("GET", f"/api/runs/{run_id}")
        if lines:
            print(f"\x1b[{lines}F\x1b[J", end="")  # 光标回退并清除旧进度表
        rows = [f"  {s['node_id']:<18} {STATUS_LABELS.get(s['status'], s['status']):<4} "
                f"{s['done']}/{s['total']}" + (f" 失败{s['failed']}" if s["failed"] else "")
                for s in d["node_states"]]
        print("\n".join([f"运行 #{run_id}  {STATUS_LABELS.get(d['status'], d['status'])}"] + rows))
        lines = 1 + len(rows)
        if d["status"] in ("completed", "failed", "cancelled"):
            if d["error"]:
                print(f"错误: {d['error']}")
            return
        time.sleep(1)
