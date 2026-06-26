"""节点操作结构化工具(Phase1) 真实活体测试：对正在运行的后端(:8000)+真实 DB+真实 DeepSeek。

Part A 直接驱动 GraphToolkit 全部工具(确定性)对真实 DB 搭 input→llm→output 链路、设模型/提示词/连线、
       看图/列血缘/op、错误路径兜底、跨租户拒绝；再经 REST 真跑(真实 DeepSeek 出答案)；最后 delete_workflow
       验级联零孤儿(复审揪出的批13回归点)。
Part B 驱动真实 RedLotus 主 Agent(真实模型)用工具从零搭一条链路，验证 Agent 真能调用这些新工具。
全程 admin 用户复用其 deepseek 模型，建即删回基线，绝不输出任何密钥。

用法(backend 目录，后端须在 :8000 运行且已合并+重启)：
    PYTHONIOENCODING=utf-8 python tools/agent_node_tools_live.py
"""
import asyncio
import io
import json
import re
import sys
import time

import httpx

BASE = "http://localhost:8000"
USER = "admin"
TAG = "_agtlive_"


def _wfid(msg: str) -> int:
    return int(re.search(r"#(\d+)", msg).group(1))


async def part_a(sf, uid, ds_name, model_name, rest):
    """直接 GraphToolkit 工具覆盖 + 真跑 + 级联删除验证。返回 (results, run_clean_ok)。"""
    from app.agent.graph_tools import GraphToolkit
    from app.models import ModelCallLog, Run, RunRow, Workflow, WorkflowVersion
    from sqlalchemy import func, select

    tk = GraphToolkit(sf, uid)
    R = []

    def ck(name, ok, detail=""):
        R.append((name, ok)); print(f"[{'PASS' if ok else 'FAIL'}] A.{name}: {detail}")

    # 1. create_workflow + add_node ×3
    wf_id = _wfid(await tk.create_workflow(f"{TAG}tools"))
    await tk.add_node(wf_id, "input", "in")
    await tk.add_node(wf_id, "llm", "g")
    await tk.add_node(wf_id, "output", "o")
    shown = json.loads(await tk.show_workflow_graph(wf_id))
    ck("create+add_node×3", {n["id"] for n in shown["rows"]} == {"in", "g", "o"},
       f"nodes={[n['id'] for n in shown['rows']]}")

    # 2. set_node_config：名解析(dataset/model) + 提示词/产出列/参数
    await tk.set_node_config(wf_id, "in", {"dataset": ds_name})
    m2 = await tk.set_node_config(wf_id, "g", {
        "model": model_name, "out": "ans", "prompt": "用一句话回答：{{q}}。只输出答案。",
        "max_tokens": "300", "think": "off"})
    async with sf() as s:
        g = json.loads((await s.get(Workflow, wf_id)).graph_json)
    gn = next(n for n in g["nodes"] if n["id"] == "g")["config"]
    inn = next(n for n in g["nodes"] if n["id"] == "in")["config"]
    ck("set_node_config 名解析", isinstance(gn.get("model_config_id"), int)
       and isinstance(inn.get("dataset_ids"), list) and inn["dataset_ids"]
       and gn.get("output_column") == "ans" and gn["params"]["max_tokens"] == 300,
       f"in.dataset_ids={inn.get('dataset_ids')} g.model={gn.get('model_config_id')} out={gn.get('output_column')}")

    # 3. connect_nodes ×2
    await tk.connect_nodes(wf_id, "in", "g")
    await tk.connect_nodes(wf_id, "g", "o")
    shown = json.loads(await tk.show_workflow_graph(wf_id))
    ck("connect_nodes×2", len(shown["edges"]) == 2, f"edges={shown['edges']}")

    # 4. workflow_columns 列血缘：llm 输入含 q、输出含 ans
    cols = json.loads(await tk.workflow_columns(wf_id, "g"))
    row = cols["rows"][0]
    ck("workflow_columns", "q" in row["input"] and "ans" in row["output"],
       f"input={row['input']} output={row['output']}")

    # 5. 错误路径：畸形数值键 → 返回错误串而非抛异常
    em = await tk.set_node_config(wf_id, "g", {"conc": "high"})
    ck("数值键错误兜底", isinstance(em, str) and em.startswith("Error"), f"msg={em[:60]}")

    # 6. auto 节点 + op 增删 + list_node_ops
    await tk.add_node(wf_id, "auto", "p")
    await tk.add_node_op(wf_id, "p", "dedup", ["q"])
    await tk.add_node_op(wf_id, "p", "shuffle", [])
    ops = json.loads(await tk.list_node_ops(wf_id, "p"))["rows"]
    await tk.remove_node_op(wf_id, "p", 1)
    ops2 = json.loads(await tk.list_node_ops(wf_id, "p"))["rows"]
    ck("op 增删", len(ops) == 2 and len(ops2) == 1 and ops2[0]["op"] == "shuffle",
       f"add→{len(ops)} rm→{[o['op'] for o in ops2]}")
    await tk.remove_node(wf_id, "p")

    # 7. set_node_prompt 内联
    await tk.set_node_prompt(wf_id, "g", "system", body="你是简洁的助手")
    async with sf() as s:
        g = json.loads((await s.get(Workflow, wf_id)).graph_json)
    sysp = next(n for n in g["nodes"] if n["id"] == "g")["config"].get("system_prompt")
    ck("set_node_prompt", sysp == "你是简洁的助手", f"system={sysp}")

    # 8. 跨租户拒绝 + 受害数据不变
    intruder = await GraphToolkit(sf, uid + 999999).add_node(wf_id, "input", "hacked")
    async with sf() as s:
        g = json.loads((await s.get(Workflow, wf_id)).graph_json)
    ck("跨租户拒绝", intruder == "工作流不存在" and not any(n["id"] == "hacked" for n in g["nodes"]),
       f"intruder={intruder}")

    # 9. 真跑(真实 DeepSeek)：经 REST 跑刚搭的链路，验证产出 ans
    rid, run_status, rows = rest.run_workflow(wf_id, budget=150)
    ans_ok = run_status == "completed" and rows and all(r.get("ans") for r in rows)
    ck("真实 DeepSeek 跑通产出 ans", ans_ok,
       f"status={run_status} n={len(rows)} sample_ans={(rows[0].get('ans') if rows else '')[:40]!r}")

    # 10. delete_workflow 级联：工作流 + run 子表 + 版本 + 日志零孤儿
    dm = await tk.delete_workflow(wf_id)
    async with sf() as s:
        gone = await s.get(Workflow, wf_id) is None
        orphans = {}
        for Model, where in (("Run", Run.workflow_id == wf_id),
                             ("WorkflowVersion", WorkflowVersion.workflow_id == wf_id),
                             ("ModelCallLog", ModelCallLog.workflow_id == wf_id)):
            orphans[Model] = (await s.execute(
                select(func.count()).select_from({"Run": Run, "WorkflowVersion": WorkflowVersion,
                                                  "ModelCallLog": ModelCallLog}[Model])
                .where(where))).scalar()
        if rid:
            orphans["RunRow"] = (await s.execute(
                select(func.count()).select_from(RunRow).where(RunRow.run_id == rid))).scalar()
    ck("delete_workflow 级联零孤儿", gone and all(v == 0 for v in orphans.values()),
       f"deleted={'已删除' in dm} gone={gone} orphans={orphans}")
    return R


class Rest:
    def __init__(self):
        self.c = httpx.Client(base_url=BASE, timeout=200, trust_env=False)
        self.c.post("/api/auth/login", json={"username": USER}).raise_for_status()
        self.smoke_ds, self.smoke_wf, self.smoke_sess = [], [], []

    def model_id(self):
        for m in self.c.get("/api/models").json():
            if "deepseek" in m["base_url"].lower():
                return m["id"], m["name"]
        return None, None

    def make_dataset(self, name, rows):
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
        ds = self.c.post("/api/datasets/upload",
                         files=[("files", (f"{name}.jsonl", io.BytesIO(body), "application/octet-stream"))]).json()[0]
        self.smoke_ds.append(ds["id"])
        for _ in range(60):
            if self.c.get(f"/api/datasets/{ds['id']}").json().get("status") == "ready":
                break
            time.sleep(0.5)
        return ds["id"], ds["name"]

    def run_workflow(self, wf_id, budget=150):
        r = self.c.post("/api/runs", json={"workflow_id": wf_id})
        if r.status_code != 200:
            return None, f"create_rejected:{r.json().get('detail','')}", []
        rid = r.json()["id"]
        t0 = time.monotonic()
        while time.monotonic() - t0 < budget:
            d = self.c.get(f"/api/runs/{rid}").json()
            if d["status"] in ("completed", "failed", "cancelled"):
                rows = []
                if d["status"] == "completed":
                    ex = self.c.get(f"/api/runs/{rid}/export", params={"node_id": "o", "format": "jsonl"})
                    rows = [json.loads(x) for x in ex.text.splitlines() if x.strip()]
                return rid, d["status"], rows
            time.sleep(2)
        self.c.post(f"/api/runs/{rid}/cancel")
        return rid, "timeout", []

    def agent_build(self, model_id, ds_name, model_name, budget=200):
        """创建 Agent 会话并下指令搭链；返回(被建工作流名, 工具调用名集合, 最终文本)。"""
        sid = self.c.post("/api/agent/sessions", json={"model_config_id": model_id}).json()["id"]
        self.smoke_sess.append(sid)
        wf_name = f"{TAG}byagent"
        msg = (f"请用你的工具从零搭一条数据处理链路并完成配置，不要问我，直接做：\n"
               f"1) 新建工作流，名字叫「{wf_name}」；\n"
               f"2) 加三个节点：一个输入(input)节点、一个 LLM(llm) 节点、一个输出(output)节点；\n"
               f"3) 输入节点用数据集「{ds_name}」；\n"
               f"4) LLM 节点用模型「{model_name}」，用户提示词设为「用一句话回答：{{{{q}}}}」，产出列名 ans；\n"
               f"5) 连线：输入→LLM→输出。\n完成后用一句话回报你建好的工作流 id。")
        self.c.post(f"/api/agent/sessions/{sid}/messages", json={"text": msg})
        t0 = time.monotonic()
        while time.monotonic() - t0 < budget:
            d = self.c.get(f"/api/agent/sessions/{sid}").json()
            if d.get("status") != "running":
                break
            time.sleep(3)
        detail = self.c.get(f"/api/agent/sessions/{sid}").json()
        tools = set()
        final = ""
        for m in detail.get("messages", []):
            cont = m.get("content")
            if m["role"] == "tool" and isinstance(cont, dict):
                tools.add(cont.get("tool", ""))
            if m["role"] == "assistant":
                final = cont.get("text", "") if isinstance(cont, dict) else str(cont)
        return wf_name, tools, final

    def find_wf(self, name):
        for w in self.c.get("/api/workflows").json():
            if w["name"] == name:
                return self.c.get(f"/api/workflows/{w['id']}").json()
        return None

    def cleanup(self):
        for r in self.c.get("/api/runs").json():
            if r["status"] in ("running", "queued"):
                self.c.post(f"/api/runs/{r['id']}/cancel")
        for w in self.c.get("/api/workflows").json():
            if w["name"].startswith(TAG):
                self.c.delete(f"/api/workflows/{w['id']}")
        for sid in self.smoke_sess:
            self.c.delete(f"/api/agent/sessions/{sid}")
        for did in self.smoke_ds:
            self.c.delete(f"/api/datasets/{did}")


async def main():
    sys.path.insert(0, ".")
    from app.db import init_db, get_session_factory
    from app.models import User
    from sqlalchemy import select
    await init_db()
    sf = get_session_factory()
    async with sf() as s:
        uid = (await s.execute(select(User).where(User.username == USER))).scalar_one().id

    rest = Rest()
    base_wf = len(rest.c.get("/api/workflows").json())
    base_ds = len(rest.c.get("/api/datasets").json())
    mid, mname = rest.model_id()
    if not mid:
        print("[ABORT] admin 无 deepseek 模型"); sys.exit(2)
    print(f"[模型] id={mid} name={mname}  [用户] {USER}#{uid}")
    ds_id, ds_name = rest.make_dataset(f"{TAG}seed",
                                       [{"q": "中国的首都是哪里？"}, {"q": "1+1 等于几？"}, {"q": "水的化学式？"}])
    print(f"[数据集] {ds_name}#{ds_id} (3 行)")

    results = []
    try:
        results += await part_a(sf, uid, ds_name, mname, rest)

        # Part B：真实 Agent 用工具搭链
        print("\n--- Part B: 真实主 Agent 驱动工具搭链 ---")
        wf_name, tools, final = rest.agent_build(mid, ds_name, mname)
        wf = rest.find_wf(wf_name)
        graph_tools_used = tools & {"create_workflow", "add_node", "set_node_config",
                                    "connect_nodes", "set_node_prompt"}
        built_ok = bool(wf) and len(wf["graph"]["nodes"]) >= 3 and len(wf["graph"]["edges"]) >= 2
        llm_ok = bool(wf) and any(
            n["type"] == "llm_synth" and n["config"].get("model_config_id")
            and n["config"].get("output_column") for n in wf["graph"]["nodes"])
        print(f"[Agent] 调用工具={sorted(tools)} 最终回复={final[:80]!r}")
        results.append(("B.Agent 调用了图工具", bool(graph_tools_used)))
        print(f"[{'PASS' if graph_tools_used else 'FAIL'}] B.Agent 调用了图工具: {sorted(graph_tools_used)}")
        results.append(("B.Agent 建出 ≥3 节点 ≥2 边链路", built_ok))
        print(f"[{'PASS' if built_ok else 'FAIL'}] B.Agent 建出链路: "
              f"nodes={len(wf['graph']['nodes']) if wf else 0} edges={len(wf['graph']['edges']) if wf else 0}")
        results.append(("B.LLM 节点配好模型+产出列", llm_ok))
        print(f"[{'PASS' if llm_ok else 'FAIL'}] B.LLM 节点配置: {llm_ok}")
    finally:
        rest.cleanup()
        time.sleep(1)
        aw = len(rest.c.get("/api/workflows").json())
        ad = len(rest.c.get("/api/datasets").json())
        print(f"\n[CLEANUP] 工作流 {base_wf}->{aw} 数据集 {base_ds}->{ad} "
              f"回基线={aw == base_wf and ad == base_ds}")
        results.append(("回基线(建即删)", aw == base_wf and ad == base_ds))

    passed = sum(1 for _, ok in results if ok)
    print(f"\n[结果] {passed}/{len(results)} 通过")
    for n, ok in results:
        if not ok:
            print(f"   ✗ {n}")
    sys.exit(0 if passed == len(results) else 1)


asyncio.run(main())
