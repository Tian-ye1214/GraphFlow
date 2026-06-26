"""http_fetch 轮询+展开+起始数据源 真实链路 LIVE 测试：驱动正在运行的后端(:8000) 真打外网
(httpbin / GitHub API)，覆盖本批三能力的端到端管线。建即删、跑完回基线，绝不输出任何密钥。

覆盖：
  1. 起始数据源·单对象取数+提取(无 input 节点，topo 一次取数)
  2. 起始数据源·records_path 展开数组成多行
  3. 轮询·状态字段已达完成值→首轮即完成并提取
  4. 逐行取数(回归)·input→http_fetch 每行渲染 {{列}}→output
  5. 起始数据源→LLM·取数结果喂下游 DeepSeek 逐行(需 zrs 配 DeepSeek 模型)

用法（backend 目录，后端需在 :8000 运行、已合并+重启）：
    PYTHONIOENCODING=utf-8 python tools/http_fetch_polling_live.py
"""
import io
import json
import sys
import time

import httpx

BASE = "http://localhost:8000"
USER = "zrs"
HTTPBIN = "https://httpbin.org"
GH = "https://api.github.com"
E = lambda a, b: {"source": a, "target": b, "kind": "normal"}  # noqa: E731


def http_node(nid, endpoint, extract, **extra):
    c = {"method": "GET", "endpoint": endpoint, "extract": extract, "concurrency": 4, "retries": 2, "timeout": 30}
    c.update(extra)
    return {"id": nid, "type": "http_fetch", "config": c}


def out(**extra):
    return {"id": "o", "type": "output", "config": dict(extra)}


class Live:
    def __init__(self):
        self.c = httpx.Client(base_url=BASE, timeout=180, trust_env=False)  # trust_env=False：本地请求绕过 Clash 代理
        self.c.post("/api/auth/login", json={"username": USER}).raise_for_status()
        self.wfs, self.dss = [], []

    def mc(self):
        models = self.c.get("/api/models").json()
        m = next((x for x in models if "deepseek" in x["base_url"].lower()), models[0] if models else None)
        if m:
            print(f"[模型] id={m['id']} {m['model_name']} {m['base_url']}")
            return m["id"]
        print("[WARN] zrs 无可用模型——跳过场景 5(起始数据源→LLM)")
        return None

    def dataset(self, name, rows):
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
        ds = self.c.post("/api/datasets/upload",
                         files=[("files", (f"{name}.jsonl", io.BytesIO(body), "application/octet-stream"))]).json()[0]
        self.dss.append(ds["id"])
        for _ in range(60):  # 上传异步摄入：轮询到 ready 再用，否则 run 抢跑读到 0 行
            if self.c.get(f"/api/datasets/{ds['id']}").json().get("status") == "ready":
                break
            time.sleep(0.5)
        return ds["id"]

    def run(self, name, graph, budget=80):
        wf = self.c.post("/api/workflows", json={"name": f"_hflive_{name}"}).json()
        self.wfs.append(wf["id"])
        self.c.put(f"/api/workflows/{wf['id']}", json={"graph": graph})
        r = self.c.post("/api/runs", json={"workflow_id": wf["id"]})
        if r.status_code != 200:
            return {"status": "create_rejected", "error": str(r.json().get("detail", "")), "rid": None}
        rid = r.json()["id"]
        t0 = time.monotonic()
        while time.monotonic() - t0 < budget:
            d = self.c.get(f"/api/runs/{rid}").json()
            if d["status"] in ("completed", "failed", "cancelled"):
                d["rid"] = rid
                return d
            time.sleep(2)
        self.c.post(f"/api/runs/{rid}/cancel")
        return {"status": "timeout", "error": "", "rid": rid}

    def rows(self, rid, node):
        r = self.c.get(f"/api/runs/{rid}/export", params={"node_id": node, "format": "jsonl"})
        return [json.loads(x) for x in r.text.splitlines() if x.strip()]

    def cleanup(self):
        for r in self.c.get("/api/runs").json():
            if r["status"] in ("running", "queued"):
                self.c.post(f"/api/runs/{r['id']}/cancel")
        for wid in self.wfs:
            self.c.delete(f"/api/workflows/{wid}")
        for did in self.dss:
            self.c.delete(f"/api/datasets/{did}")


def main():
    L = Live()
    base_wf = len(L.c.get("/api/workflows").json())
    base_ds = len(L.c.get("/api/datasets").json())
    mc = L.mc()
    results = []

    def check(name, ok, detail):
        results.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")

    try:
        # 1. 起始数据源·单对象取数+提取（无 input 节点）
        d = L.run("src_single", {"nodes": [
            http_node("src", f"{HTTPBIN}/get?q=hello&status=done", {"q": "args.q"}),
            out()], "edges": [E("src", "o")]})
        r = L.rows(d["rid"], "o") if d["status"] == "completed" else []
        check("起始数据源·单对象", d["status"] == "completed" and r == [{"q": "hello"}],
              f"status={d['status']} rows={r}")

        # 2. 起始数据源·records_path 展开数组成多行
        d = L.run("src_explode", {"nodes": [
            http_node("src", f"{GH}/search/repositories?q=language:python&per_page=3&sort=stars",
                      {"repo": "full_name"}, records_path="items"),
            out()], "edges": [E("src", "o")]})
        r = L.rows(d["rid"], "o") if d["status"] == "completed" else []
        ok2 = d["status"] == "completed" and len(r) == 3 and all("repo" in x and "/" in str(x["repo"]) for x in r)
        check("起始数据源·展开3行", ok2, f"status={d['status']} n={len(r)} sample={r[0] if r else None}")

        # 3. 轮询·状态字段已达完成值 → 首轮即完成并提取
        d = L.run("poll_done", {"nodes": [
            http_node("src", f"{HTTPBIN}/get?status=done", {"st": "args.status"},
                      poll_status_path="args.status", poll_until="done", poll_interval=0, poll_max_attempts=5),
            out()], "edges": [E("src", "o")]})
        r = L.rows(d["rid"], "o") if d["status"] == "completed" else []
        check("轮询·首轮达成", d["status"] == "completed" and r == [{"st": "done"}],
              f"status={d['status']} rows={r}")

        # 4. 逐行取数(回归)·input→http_fetch 每行渲染 {{列}}→output
        ds = L.dataset("hflive_rows", [{"city": "London"}, {"city": "Paris"}])
        d = L.run("per_row", {"nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [ds]}},
            http_node("src", f"{HTTPBIN}/get?city={{{{city}}}}", {"echo": "args.city"}),
            out()], "edges": [E("in", "src"), E("src", "o")]})
        r = L.rows(d["rid"], "o") if d["status"] == "completed" else []
        echos = sorted(x.get("echo") for x in r)
        check("逐行取数渲染{{列}}", d["status"] == "completed" and echos == ["London", "Paris"],
              f"status={d['status']} rows={r}")

        # 5. 起始数据源→LLM：取数结果喂下游 DeepSeek 逐行
        if mc:
            d = L.run("src_to_llm", {"nodes": [
                http_node("src", f"{HTTPBIN}/get?topic=ocean", {"topic": "args.topic"}),
                {"id": "g", "type": "llm_synth", "config": {
                    "model_config_id": mc, "output_column": "s", "concurrency": 4, "retries": 2,
                    "user_prompt": "用一句中文介绍主题：{{topic}}。只输出这句话。",
                    "params": {"thinking_enabled": False, "max_tokens": 400}}},
                out()], "edges": [E("src", "g"), E("g", "o")]}, budget=160)
            r = L.rows(d["rid"], "o") if d["status"] == "completed" else []
            ok5 = d["status"] == "completed" and len(r) == 1 and r[0].get("topic") == "ocean" and bool(r[0].get("s"))
            check("起始数据源→LLM", ok5, f"status={d['status']} topic={r[0].get('topic') if r else None} s_len={len(r[0].get('s','')) if r else 0}")
    finally:
        L.cleanup()
        aw, ad = len(L.c.get("/api/workflows").json()), len(L.c.get("/api/datasets").json())
        print(f"\n[CLEANUP] 工作流 {base_wf}->{aw} 数据集 {base_ds}->{ad} 回基线={aw == base_wf and ad == base_ds}")

    passed = sum(1 for _, ok in results if ok)
    print(f"\n[结果] {passed}/{len(results)} 通过")
    sys.exit(0 if passed == len(results) else 1)


main()
