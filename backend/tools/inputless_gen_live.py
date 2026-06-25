"""无输入起始节点（生成到达 count）真实链路 LIVE 测试：驱动正在运行的后端(:8000) 真跑两条
「无 input 节点」的生成链，真实调用 DeepSeek。建即删、跑完回基线，绝不输出任何密钥。

两条链：
  ① llm_synth(zrs) → output(count=8)            纯生成：输出应恰好 8 行。
  ② llm_synth → qc(zrs:偶数才过) → output(count=8) 生成+质检：约一半被淘汰 → 生成 > 8、接收 = 8。

用法（backend 目录，后端需在 :8000 运行，且分支已合并+重启）：
    PYTHONIOENCODING=utf-8 python tools/inputless_gen_live.py
"""
import sys
import time

import httpx

BASE = "http://localhost:8000"
USER = "zrs"
COUNT = 8


def _synth_only(mc_id):
    return {
        "nodes": [
            {"id": "g", "type": "llm_synth", "config": {
                "model_config_id": mc_id, "output_column": "sentence", "concurrency": 4, "retries": 2,
                "user_prompt": "用一句中文写一句关于大自然的话，只输出这句话本身。"}},
            {"id": "o", "type": "output", "config": {"count": COUNT}},
        ],
        "edges": [{"source": "g", "target": "o", "kind": "normal"}],
    }


def _synth_qc(mc_id):
    return {
        "nodes": [
            {"id": "g", "type": "llm_synth", "config": {
                "model_config_id": mc_id, "output_column": "n", "concurrency": 4, "retries": 2,
                "user_prompt": "随机生成一个 1 到 100 之间的整数，只输出这个数字本身，不要任何多余文字。"}},
            {"id": "q", "type": "qc", "config": {
                "judge_model_ids": [mc_id], "pass_k": 1, "max_rounds": 1, "concurrency": 4,
                "system_prompt": "你是严格质检员：当且仅当【答案是一个偶数】时通过，奇数或非数字一律不通过。",
                "user_prompt": '答案：{{n}}\n返回 JSON：{"status": "pass" 或 "failed", "reason": "理由"}',
                "params": {"temperature": 0}}},
            {"id": "o", "type": "output", "config": {"count": COUNT}},
        ],
        "edges": [{"source": "g", "target": "q", "kind": "normal"},
                  {"source": "q", "target": "o", "kind": "normal"}],
    }


def _run_and_wait(c, wf_id, budget_s=300):
    run_id = c.post("/api/runs", json={"workflow_id": wf_id}).json()["id"]
    t0 = time.monotonic()
    while time.monotonic() - t0 < budget_s:
        st = c.get(f"/api/runs/{run_id}").json()["status"]
        if st in ("completed", "failed", "cancelled"):
            return run_id, st
        time.sleep(2)
    return run_id, "timeout"


def _count_rows(c, run_id, node_id):
    """导出某节点 done 行数（jsonl 每行一条）。"""
    r = c.get(f"/api/runs/{run_id}/export", params={"node_id": node_id, "format": "jsonl"})
    r.raise_for_status()
    return sum(1 for line in r.text.splitlines() if line.strip())


def _case(c, mc_id, name, graph, check):
    wf = c.post("/api/workflows", json={"name": f"无输入生成-live-{name}"}).json()
    c.put(f"/api/workflows/{wf['id']}", json={"graph": graph})
    run_id, st = _run_and_wait(c, wf["id"])
    print(f"[{name}] run={run_id} status={st}")
    ok = st == "completed" and check(c, run_id)
    c.delete(f"/api/workflows/{wf['id']}")          # 建即删回基线
    print(f"[{name}] {'✓ PASS' if ok else '✗ FAIL'}（已删除工作流回基线）")
    return ok


def main():
    with httpx.Client(base_url=BASE, timeout=120) as c:
        c.post("/api/auth/login", json={"username": USER}).raise_for_status()
        models = c.get("/api/models").json()
        mc = next((m for m in models if "deepseek" in m["base_url"].lower()), models[0])
        print(f"[模型] id={mc['id']} {mc['model_name']} {mc['base_url']}")

        def check_synth(c, run_id):
            out = _count_rows(c, run_id, "o")
            print(f"  纯生成 output={out}（期望 {COUNT}）")
            return out == COUNT

        def check_qc(c, run_id):
            out = _count_rows(c, run_id, "o")
            gen = _count_rows(c, run_id, "g")
            print(f"  生成+质检 output={out}（期望 {COUNT}）, 生成 g={gen}（期望 > {COUNT}）")
            return out == COUNT and gen > COUNT

        r1 = _case(c, mc["id"], "纯生成", _synth_only(mc["id"]), check_synth)
        r2 = _case(c, mc["id"], "生成+质检", _synth_qc(mc["id"]), check_qc)
        print("\n[完成] 全部通过" if r1 and r2 else "\n[失败] 见上方 ✗")
        sys.exit(0 if r1 and r2 else 1)


main()
