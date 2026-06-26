"""gf 公共：HTTP 客户端、资源解析、参数转换、常量表。"""
import sys
import time
from pathlib import Path

import httpx

from app.cli import load_state
# 图变更逻辑/常量单点在 graph_ops；client 仅为旧调用方(model.py)再导出仍需的项，避免重复定义。
from app.services.graph_ops import _convert as convert, LLM_PARAM_KEYS  # noqa: F401

NODE_LABELS = {"input": "输入", "llm_synth": "LLM 合成", "auto_process": "自动处理",
               "output": "输出", "qc": "质检", "http_fetch": "HTTP 取数"}
KIND_LABELS = {"workflows": "工作流", "datasets": "数据集", "models": "模型配置", "prompts": "提示词"}
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

    def download(self, path: str, out: Path, *, params=None) -> int:
        """GET 二进制流式落盘到 out，返回写入字节数。内存恒定 ~1MB，对 1-10G 导出友好
        （旧实现 r.content 把整个响应体读进内存，大文件必 OOM）。读超时关闭，避免大文件被砍断。"""
        written = 0
        with self.http.stream("GET", path, params=params,
                              timeout=httpx.Timeout(30.0, read=None)) as r:
            if r.status_code >= 400:
                r.read()
                self.check(r)        # 统一错误处理（die）
            with out.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    written += len(chunk)
        return written

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
        endpoint = c.get("endpoint") or c.get("url", "?")
        return f"{c.get('method', 'GET')} {endpoint} -> {list((c.get('extract') or {}).keys())}"
    return f"保存为数据集「{c['dataset_name']}」" if c.get("save_as_dataset") else ""


def parse_kv(pairs: list[str]) -> dict:
    out = {}
    for p in pairs:
        if "=" not in p:
            die(f"参数格式应为 key=value: {p}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


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
