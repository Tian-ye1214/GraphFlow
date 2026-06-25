"""无输入起始节点（生成到达 count）真实链路 LIVE 测试：驱动正在运行的后端(:8000) 真跑多条
「无 input 节点」的生成链 + 一条回归链，真实调用 DeepSeek。建即删、跑完回基线，绝不输出任何密钥。

覆盖：纯生成达 count / 生成+质检(淘汰即补·生成>count) / fanout / 质检撞列报错 / 默认思考路径 /
回归(input 工作流含游离生成节点仍 completed)。

用法（backend 目录，后端需在 :8000 运行、分支已合并+重启，且 zrs 账户已配可用 DeepSeek 模型）：
    PYTHONIOENCODING=utf-8 python tools/inputless_gen_live.py
"""
import io
import json
import sys
import time

import httpx

BASE = "http://localhost:8000"
USER = "zrs"
# 生成节点参数：关思考(避开推理模型 token 耗尽空内容)、给足 max_tokens、高温取多样(种子是同一空行，靠采样区分)
GEN = {"thinking_enabled": False, "max_tokens": 400, "temperature": 0.95}
QC = {"thinking_enabled": False, "max_tokens": 400, "temperature": 0}
E = lambda a, b: {"source": a, "target": b, "kind": "normal"}  # noqa: E731


def synth(mc, col, prompt, **extra):
    c = {"model_config_id": mc, "output_column": col, "concurrency": 4, "retries": 2,
         "user_prompt": prompt, "params": GEN}
    c.update(extra)
    return {"id": "g", "type": "llm_synth", "config": c}


def qc(mc, prompt, sysp):
    return {"id": "q", "type": "qc", "config": {
        "judge_model_ids": [mc], "pass_k": 1, "max_rounds": 1, "concurrency": 4,
        "system_prompt": sysp, "user_prompt": prompt, "params": QC}}


def out(count=None, **extra):
    cfg = {**({"count": count} if count else {}), **extra}
    return {"id": "o", "type": "output", "config": cfg}


class Live:
    def __init__(self):
        self.c = httpx.Client(base_url=BASE, timeout=180)
        self.c.post("/api/auth/login", json={"username": USER}).raise_for_status()
        self.wfs, self.dss = [], []

    def mc(self):
        models = self.c.get("/api/models").json()
        if not models:
            print("[ABORT] zrs 账户无可用模型——请先在 zrs 下配一个 DeepSeek 模型再跑")
            sys.exit(2)
        m = next((x for x in models if "deepseek" in x["base_url"].lower()), models[0])
        print(f"[模型] id={m['id']} {m['model_name']} {m['base_url']}")
        return m["id"]

    def dataset(self, name, rows):
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
        ds = self.c.post("/api/datasets/upload",
                         files=[("files", (f"{name}.jsonl", io.BytesIO(body), "application/octet-stream"))]).json()[0]
        self.dss.append(ds["id"])
        return ds["id"]

    def run(self, name, graph, budget=160):
        wf = self.c.post("/api/workflows", json={"name": f"_iglive_{name}"}).json()
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

    def qc_total(self, rid):
        return sum(m["total"] for m in self.c.get(f"/api/runs/{rid}/qc-metrics").json())

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
        # 1. 纯生成达 count
        d = L.run("synth8", {"nodes": [synth(mc, "s", "用一句中文写一句关于大自然的话，只输出这句话。"), out(8)],
                             "edges": [E("g", "o")]})
        n = len(L.rows(d["rid"], "o")) if d["status"] == "completed" else -1
        check("纯生成 count=8", d["status"] == "completed" and n == 8, f"status={d['status']} output={n}")

        # 2. 生成+质检：偶数才过 → 生成>count、接收=count
        d = L.run("synthqc6", {"nodes": [
            synth(mc, "n", "随机生成一个 1 到 100 之间的整数，只输出这个数字本身，不要任何多余文字。"),
            qc(mc, '答案：{{n}}\n返回 JSON：{"status":"pass" 或 "failed","reason":"理由"}',
               "你是严格质检员：当且仅当答案是一个偶数时通过(status=pass)，奇数或非数字一律不通过。"),
            out(6)], "edges": [E("g", "q"), E("q", "o")]})
        o = len(L.rows(d["rid"], "o")) if d["status"] == "completed" else -1
        gen = len(L.rows(d["rid"], "g")) if d["status"] == "completed" else -1
        check("生成+质检 count=6", d["status"] == "completed" and o == 6 and gen > 6,
              f"status={d['status']} output={o} 生成={gen} qc判定={L.qc_total(d['rid']) if d['rid'] else 0}")

        # 3. fanout=2：3 种子 ×2 = 6
        d = L.run("fanout6", {"nodes": [synth(mc, "s", "写一句关于星空的短句，只输出该句。", fanout_n=2), out(6)],
                              "edges": [E("g", "o")]})
        n = len(L.rows(d["rid"], "o")) if d["status"] == "completed" else -1
        check("fanout=2 count=6", d["status"] == "completed" and n == 6, f"status={d['status']} output={n}")

        # 4. 质检状态列撞生成列 → 整 run failed 点名
        d = L.run("collide", {"nodes": [
            synth(mc, "qc_status", "随便输出一个词。"),
            qc(mc, '答案：{{qc_status}}\n返回 JSON：{"status":"pass"}', "一律通过 status=pass。"),
            out(3)], "edges": [E("g", "q"), E("q", "o")]})
        check("质检撞列报错", d["status"] == "failed" and "qc_status" in (d.get("error") or ""),
              f"status={d['status']} error={(d.get('error') or '')[:60]}")

        # 5. 默认思考路径（不设 params）：验证默认 UX 也能达 count（给足时间，推理模型慢）
        d = L.run("default", {"nodes": [
            {"id": "g", "type": "llm_synth", "config": {
                "model_config_id": mc, "output_column": "s", "concurrency": 4, "retries": 2,
                "user_prompt": "用一句话写个关于海洋的事实。"}}, out(4)], "edges": [E("g", "o")]}, budget=200)
        n = len(L.rows(d["rid"], "o")) if d["status"] == "completed" else -1
        check("默认思考 count=4", d["status"] == "completed" and n == 4, f"status={d['status']} output={n}")

        # 6. 回归：input 工作流含游离生成节点 → 仍 completed（不被误判生成链）
        ds = L.dataset("iglive_reg", [{"q": "问A"}, {"q": "问B"}])
        d = L.run("regression", {"nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [ds]}},
            synth(mc, "a", "回答：{{q}}"),
            {"id": "o", "type": "output", "config": {}},
            {"id": "stray", "type": "llm_synth", "config": {"model_config_id": mc, "user_prompt": "x"}}],
            "edges": [E("in", "g"), E("g", "o")]})
        n = len(L.rows(d["rid"], "o")) if d["status"] == "completed" else -1
        check("回归:游离节点不破坏input工作流", d["status"] == "completed" and n == 2,
              f"status={d['status']} output={n}")
    finally:
        L.cleanup()
        aw, ad = len(L.c.get("/api/workflows").json()), len(L.c.get("/api/datasets").json())
        print(f"\n[CLEANUP] 工作流 {base_wf}->{aw} 数据集 {base_ds}->{ad} 回基线={aw == base_wf and ad == base_ds}")

    passed = sum(1 for _, ok in results if ok)
    print(f"\n[结果] {passed}/{len(results)} 通过")
    sys.exit(0 if passed == len(results) else 1)


main()
