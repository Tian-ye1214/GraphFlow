"""目标模式真实链路 LIVE 测试：驱动正在运行的后端(:8000) 真跑 agent 目标优化 loop。

必须真实调用 DeepSeek：agent 每轮用 gf 工具改进工作流 → 系统自动跑数（真实合成+质检）→ 指标喂回
→ goal_loop.decide 跳出。工作流刻意设成"答案≤15字才过检"+ 模糊合成提示，逼 agent 真去优化提示词。
绝不输出任何密钥。

用法（backend 目录，后端需在 :8000 运行）：
    PYTHONIOENCODING=utf-8 python tools/goal_loop_live.py            # 建链 + 起目标 + 观察
    PYTHONIOENCODING=utf-8 python tools/goal_loop_live.py watch <sid>  # 仅继续观察某会话
"""
import io
import json
import sys
import time

import httpx

BASE = "http://localhost:8000"
USER = "zrs"
GOAL = "把首轮质检通过率提到 80% 以上"
DATA = [
    {"q": "中国的首都是哪座城市？"},
    {"q": "水在标准大气压下的沸点是多少摄氏度？"},
    {"q": "一年有多少个月？"},
    {"q": "太阳系中最大的行星是哪一颗？"},
    {"q": "光在真空中的速度大约是多少千米每秒？"},
]


def _graph(ds_id, mc_id):
    return {
        "nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [ds_id]}},
            {"id": "gen", "type": "llm_synth", "config": {
                "model_config_id": mc_id, "output_column": "answer", "concurrency": 4, "retries": 2,
                "user_prompt": "{{q}}"}},  # 故意模糊：不限长度，DeepSeek 往往长篇 → 过不了 ≤15字
            {"id": "qc", "type": "qc", "config": {
                "judge_model_ids": [mc_id], "pass_k": 1, "max_rounds": 1, "concurrency": 4,
                "system_prompt": "你是严格质检员：答案必须【正确】且【不超过15个汉字】才算通过，超长一律不通过。",
                "user_prompt": '问题：{{q}}\n答案：{{answer}}\n返回 JSON：{"status": "pass" 或 "failed", "reason": "理由"}',
                "params": {"temperature": 0}}},
            {"id": "out", "type": "output", "config": {}},
        ],
        "edges": [{"source": "in", "target": "gen", "kind": "normal"},
                  {"source": "gen", "target": "qc", "kind": "normal"},
                  {"source": "qc", "target": "out", "kind": "normal"}],
    }


IMPOSSIBLE_GOAL = "把首轮质检通过率提到 60% 以上"


def _impossible_graph(ds_id, mc_id):
    """不可达：要求答案本身是负数且正确——事实题无正确负数答案，agent 怎么改都过不了。"""
    g = _graph(ds_id, mc_id)
    for n in g["nodes"]:
        if n["id"] == "qc":
            n["config"]["system_prompt"] = (
                "你是严格质检员：答案必须【本身是一个负数】（带负号的数字，如 -3）"
                "且【正确回答了问题】，二者同时满足才算通过；否则一律不通过。")
    return g


def _setup(c, mc_id, mode):
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in DATA).encode("utf-8")
    files = [("files", ("目标live.jsonl", io.BytesIO(body), "application/octet-stream"))]
    ds = c.post("/api/datasets/upload", files=files).json()[0]
    goal = IMPOSSIBLE_GOAL if mode == "impossible" else GOAL
    graph = _impossible_graph(ds["id"], mc_id) if mode == "impossible" else _graph(ds["id"], mc_id)
    wf = c.post("/api/workflows", json={"name": f"目标模式-live-{mode}"}).json()
    c.put(f"/api/workflows/{wf['id']}", json={"graph": graph})
    sid = c.post("/api/agent/sessions", json={"model_config_id": mc_id}).json()["id"]
    c.post(f"/api/agent/sessions/{sid}/goal", json={"workflow_id": wf["id"], "goal_text": goal})
    return sid, wf["id"]


def _watch(c, sid, budget_s=480):
    seen, t0 = 0, time.monotonic()
    while time.monotonic() - t0 < budget_s:
        d = c.get(f"/api/agent/sessions/{sid}").json()
        for m in d["messages"][seen:]:
            txt = (m["content"].get("text") or "") if isinstance(m["content"], dict) else str(m["content"])
            print(f"  [{m['role']}] {txt[:400].replace(chr(10), ' ')}")
        seen = len(d["messages"])
        if d["status"] == "idle":
            return True
        time.sleep(4)
    return False


def _report(c, wf_id):
    runs = c.get("/api/runs", params={"workflow_id": wf_id}).json()
    print(f"\n— 各轮真实跑数指标（共 {len(runs)} 次 run）—")
    for r in sorted(runs, key=lambda x: x["id"]):
        mets = c.get(f"/api/runs/{r['id']}/qc-metrics").json()
        for m in mets:
            print(f"  run {r['id']} status={r['status']} 节点{m['node_id']} "
                  f"首轮通过率={m['first_round_rate']:.0%}（{m['first_round_pass']}/{m['total']}）")


def main():
    with httpx.Client(base_url=BASE, timeout=60) as c:
        r = c.post("/api/auth/login", json={"username": USER})
        r.raise_for_status()
        mode = sys.argv[1] if len(sys.argv) > 1 else "run"
        if mode == "watch":
            sid = int(sys.argv[2])
            wf_id = None
            print(f"[继续观察] session={sid}")
        else:
            models = c.get("/api/models").json()
            mc = next((m for m in models if "deepseek" in m["base_url"].lower()), models[0])
            print(f"[模型] id={mc['id']} {mc['model_name']} {mc['base_url']} 模式={mode}")
            sid, wf_id = _setup(c, mc["id"], mode)
            goal = IMPOSSIBLE_GOAL if mode == "impossible" else GOAL
            print(f"[已起目标] session={sid} workflow={wf_id} 目标={goal!r}\n— agent 回合/轮次消息 —")
        done = _watch(c, sid)
        if wf_id:
            _report(c, wf_id)
        print("\n[完成]" if done else f"\n[仍在跑] 续看：python tools/goal_loop_live.py watch {sid}")


main()
