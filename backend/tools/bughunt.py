"""链路 CRUD / 复杂链路 / 大量跑数 缺陷复现脚本（隔离临时库 + 确定性假模型）。

不碰真实库：启动前把 settings.data_dir 指到临时目录，db_url 随之指向临时 graphflow.db。
确定性假模型（monkeypatch llm.chat）用于把「引擎逻辑缺陷」与「模型质量」隔离开——
这正是查 CRUD/复杂图/累计计数 这类 bug 的正确工具。

用法（backend 目录）：PYTHONIOENCODING=utf-8 PYTHONPATH=. python tools/bughunt.py
"""
import asyncio
import json
import tempfile
from pathlib import Path

from app.config import settings

U = {"prompt_tokens": 1, "completion_tokens": 1}


def banner(t):
    print("\n" + "=" * 72 + f"\n{t}\n" + "=" * 72)


async def _client():
    import httpx
    from app.main import create_app
    transport = httpx.ASGITransport(app=create_app())
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _login(c, name):
    await c.post("/api/auth/login", json={"username": name})


async def _mk_model(c):
    return (await c.post("/api/models", json={
        "name": "m", "model_name": "fake", "base_url": "http://x/v1",
        "api_key": "k", "default_params": {}})).json()


async def _upload(c, rows):
    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
    import io
    files = [("files", ("seed.jsonl", io.BytesIO(body), "application/octet-stream"))]
    return (await c.post("/api/datasets/upload", files=files)).json()[0]


async def _set_graph(c, wf_id, graph):
    await c.put(f"/api/workflows/{wf_id}", json={"graph": graph})


async def _run_and_wait(c, wf_id):
    from app.engine.manager import manager
    rid = (await c.post("/api/runs", json={"workflow_id": wf_id})).json()["id"]
    await manager.wait(rid)
    return rid


def _patch(fn):
    from app.services import llm

    async def fake(mc, system, user, params=None, retries=3):
        return fn(user)

    llm.chat = fake


# --------------------------------------------------------------------------- B
async def bug_delete_workflow_orphans():
    banner("B) delete_workflow 是否级联删除子数据（版本/运行/行/日志/指标）")
    from app.db import get_session_factory
    from sqlalchemy import func, select
    from app.models import (QcFailure, QcMetric, Run, RunLog, RunNodeState, RunRow,
                            WorkflowVersion)
    _patch(lambda u: (f"答[{u}]", U) if not u.startswith("判定:")
           else (json.dumps({"pass": True, "reason": ""}), U))
    c = await _client()
    await _login(c, "userB")
    mc = await _mk_model(c)
    ds = await _upload(c, [{"q": f"问{i}"} for i in range(3)])
    wf = (await c.post("/api/workflows", json={"name": "待删流"})).json()
    graph = {"nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": [ds["id"]]}},
        {"id": "gen", "type": "llm_synth", "config": {
            "model_config_id": mc["id"], "user_prompt": "Q:{{q}}", "output_column": "a"}},
        {"id": "qc", "type": "qc", "config": {
            "model_config_id": mc["id"], "user_prompt": "判定:{{a}}"}},
        {"id": "out", "type": "output", "config": {}},
    ], "edges": [
        {"source": "in", "target": "gen", "kind": "normal"},
        {"source": "gen", "target": "qc", "kind": "normal"},
        {"source": "qc", "target": "out", "kind": "normal"}]}
    await _set_graph(c, wf["id"], graph)
    rid = await _run_and_wait(c, wf["id"])
    # 导出一份产物，制造磁盘文件
    await c.get(f"/api/runs/{rid}/export?format=jsonl")

    sf = get_session_factory()

    async def counts():
        async with sf() as s:
            out = {}
            out["WorkflowVersion"] = (await s.execute(select(func.count()).select_from(WorkflowVersion)
                                      .where(WorkflowVersion.workflow_id == wf["id"]))).scalar()
            for M in (Run, RunRow, RunNodeState, RunLog, QcMetric, QcFailure):
                col = M.workflow_id if M is Run else M.run_id
                pred = (col == wf["id"]) if M is Run else (col == rid)
                out[M.__name__] = (await s.execute(select(func.count()).select_from(M).where(pred))).scalar()
            return out

    before = await counts()
    print("删除前子数据计数:", before)
    resp = await c.delete(f"/api/workflows/{wf['id']}")
    print("DELETE /api/workflows 状态码:", resp.status_code, resp.json())
    after = await counts()
    print("删除后子数据计数:", after)
    listed = (await c.get("/api/runs")).json()
    direct = await c.get(f"/api/runs/{rid}")
    exports = list((settings.data_dir / "exports").glob(f"run{rid}_*"))
    print(f"删除后 GET /api/runs 列表含该 run? {any(r['id'] == rid for r in listed)}（共 {len(listed)} 条）")
    print(f"删除后 GET /api/runs/{rid} 直链状态码: {direct.status_code}（仍可直接访问=孤儿）")
    print(f"删除后遗留导出文件: {[p.name for p in exports]}")
    leaked = sum(after.values())
    verdict = "BUG 复现：子数据全部成为孤儿" if leaked else "无孤儿（已级联）"
    print(f"==> {verdict}；孤儿行合计 {leaked}")
    await c.aclose()


# --------------------------------------------------------------------------- A
async def bug_rerun_failed_dup_qc_metric():
    banner("A) rerun-failed 后 QC 首轮指标是否被重复累计")
    from app.db import get_session_factory
    from app.services import run_service
    from sqlalchemy import select
    from app.models import QcMetric
    broken = {"on": True}

    def fn(user):
        if user.startswith("判定:"):
            return json.dumps({"pass": True, "reason": ""}), U
        if broken["on"] and "问1" in user:
            raise RuntimeError("临时故障")
        return f"答[{user}]", U

    _patch(fn)
    c = await _client()
    await _login(c, "userA")
    mc = await _mk_model(c)
    ds = await _upload(c, [{"q": f"问{i}"} for i in range(3)])
    wf = (await c.post("/api/workflows", json={"name": "重跑流"})).json()
    graph = {"nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": [ds["id"]]}},
        {"id": "gen", "type": "llm_synth", "config": {
            "model_config_id": mc["id"], "user_prompt": "Q:{{q}}", "output_column": "a", "retries": 1}},
        {"id": "qc", "type": "qc", "config": {
            "model_config_id": mc["id"], "user_prompt": "判定:{{a}}"}},
        {"id": "out", "type": "output", "config": {}},
    ], "edges": [
        {"source": "in", "target": "gen", "kind": "normal"},
        {"source": "gen", "target": "qc", "kind": "normal"},
        {"source": "qc", "target": "out", "kind": "normal"}]}
    await _set_graph(c, wf["id"], graph)
    rid = await _run_and_wait(c, wf["id"])
    m1 = (await c.get(f"/api/runs/{rid}/qc-metrics")).json()
    print("首跑 qc-metrics:", m1)

    broken["on"] = False
    from app.engine.manager import manager
    r = await c.post(f"/api/runs/{rid}/rerun-failed")
    print("rerun-failed 状态码:", r.status_code)
    await manager.wait(rid)
    m2 = (await c.get(f"/api/runs/{rid}/qc-metrics")).json()
    rate = await run_service.first_round_rate(get_session_factory(), rid)
    async with get_session_factory()() as s:
        n = len((await s.execute(select(QcMetric).where(QcMetric.run_id == rid))).scalars().all())
    print("重跑后 qc-metrics:", m2)
    print(f"重跑后 QcMetric 行数={n}；first_round_rate(goal 模式读)= {rate}")
    verdict = "BUG 复现：QcMetric 重复累计（≥2 行 / 同节点多条）" if n >= 2 else "无重复"
    print(f"==> {verdict}")
    await c.aclose()


# --------------------------------------------------------------------------- E
async def bug_rescan_fanout_inflation():
    banner("E) 回扫目标带 fanout_n>1 时 通过数膨胀 / failed 计数为负")
    import json as J
    from app.engine import runner
    from app.db import get_session_factory
    from app import crypto
    from sqlalchemy import select
    from app.models import (Dataset, DatasetRow, ModelConfig, Run, RunNodeState, User,
                            Workflow, WorkflowVersion)

    def fn(user):
        if user.startswith("判定:"):
            bad = "bad" in user
            return J.dumps({"pass": not bad, "reason": "bad" if bad else ""}), U
        if "质检未通过" in user:   # 回扫重生成
            return "good", U
        return "bad", U            # 首轮生成

    _patch(fn)
    graph = {"nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": []}},
        {"id": "gen", "type": "llm_synth", "config": {
            "model_config_id": 0, "user_prompt": "Q:{{q}}", "output_column": "a", "fanout_n": 2}},
        {"id": "qc", "type": "qc", "config": {
            "model_config_id": 0, "user_prompt": "判定:{{a}}", "max_rounds": 3}},
        {"id": "out", "type": "output", "config": {}},
    ], "edges": [
        {"source": "in", "target": "gen", "kind": "normal"},
        {"source": "gen", "target": "qc", "kind": "normal"},
        {"source": "qc", "target": "out", "kind": "normal"},
        {"source": "qc", "target": "gen", "kind": "rescan"}]}
    sf = get_session_factory()
    async with sf() as s:
        u = User(username="userE")
        s.add(u); await s.flush()
        mc = ModelConfig(user_id=u.id, name="m", model_name="f", base_url="x",
                         api_key_enc=crypto.encrypt("k"))
        s.add(mc); await s.flush()
        ds = Dataset(user_id=u.id, name="d", row_count=1)
        s.add(ds); await s.flush()
        s.add(DatasetRow(dataset_id=ds.id, idx=0, data_json=J.dumps({"q": "问0"})))
        g = J.loads(J.dumps(graph))
        for n in g["nodes"]:
            if n["type"] == "input":
                n["config"]["dataset_ids"] = [ds.id]
            if n["type"] in ("llm_synth", "qc"):
                n["config"]["model_config_id"] = mc.id
        wf = Workflow(user_id=u.id, name="wf", graph_json=J.dumps(g))
        s.add(wf); await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json=J.dumps(g))
        s.add(ver); await s.flush()
        run = Run(user_id=u.id, workflow_id=wf.id, workflow_version_id=ver.id)
        s.add(run); await s.commit()
        rid = run.id
    await runner.execute_run(rid, sf, asyncio.Semaphore(8), asyncio.Event())
    out_rows = await runner._node_outputs(sf, rid, "qc")
    async with sf() as s:
        ns = (await s.execute(select(RunNodeState).where(
            RunNodeState.run_id == rid, RunNodeState.node_id == "qc"))).scalar_one()
    print(f"输入行数=1，fanout_n=2 → qc 输入应为 2 行")
    print(f"qc 节点状态: total={ns.total} done={ns.done} failed={ns.failed}")
    print(f"qc 最终输出行数={len(out_rows)}（从 1 条输入膨胀而来）")
    verdict = "BUG 复现：failed 为负 且 输出膨胀" if ns.failed < 0 else "计数正常"
    print(f"==> {verdict}")


# --------------------------------------------------------------------------- D
async def bug_resume_clobbers_qc_state():
    banner("D) 断点续跑/再执行 会把已完成 QC 节点的通过/拒绝计数清成「全通过」")
    import json as J
    from app.engine import runner
    from app.db import get_session_factory
    from app import crypto
    from sqlalchemy import select
    from app.models import (Dataset, DatasetRow, ModelConfig, Run, RunNodeState, User,
                            Workflow, WorkflowVersion)

    def fn(user):
        if user.startswith("判定:"):
            ok = "问1" not in user
            return J.dumps({"pass": ok, "reason": "" if ok else "坏"}), U
        return f"答[{user}]", U

    _patch(fn)
    graph = {"nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": []}},
        {"id": "gen", "type": "llm_synth", "config": {
            "model_config_id": 0, "user_prompt": "Q:{{q}}", "output_column": "a"}},
        {"id": "qc", "type": "qc", "config": {
            "model_config_id": 0, "user_prompt": "判定:{{a}}", "max_rounds": 1}},
        {"id": "out", "type": "output", "config": {}},
    ], "edges": [
        {"source": "in", "target": "gen", "kind": "normal"},
        {"source": "gen", "target": "qc", "kind": "normal"},
        {"source": "qc", "target": "out", "kind": "normal"}]}
    sf = get_session_factory()
    async with sf() as s:
        u = User(username="userD")
        s.add(u); await s.flush()
        mc = ModelConfig(user_id=u.id, name="m", model_name="f", base_url="x",
                         api_key_enc=crypto.encrypt("k"))
        s.add(mc); await s.flush()
        ds = Dataset(user_id=u.id, name="d", row_count=3)
        s.add(ds); await s.flush()
        for i in range(3):
            s.add(DatasetRow(dataset_id=ds.id, idx=i, data_json=J.dumps({"q": f"问{i}"}, ensure_ascii=False)))
        g = J.loads(J.dumps(graph))
        for n in g["nodes"]:
            if n["type"] == "input":
                n["config"]["dataset_ids"] = [ds.id]
            if n["type"] in ("llm_synth", "qc"):
                n["config"]["model_config_id"] = mc.id
        wf = Workflow(user_id=u.id, name="wf", graph_json=J.dumps(g))
        s.add(wf); await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json=J.dumps(g))
        s.add(ver); await s.flush()
        run = Run(user_id=u.id, workflow_id=wf.id, workflow_version_id=ver.id)
        s.add(run); await s.commit()
        rid = run.id

    async def qc_state():
        async with sf() as s:
            ns = (await s.execute(select(RunNodeState).where(
                RunNodeState.run_id == rid, RunNodeState.node_id == "qc"))).scalar_one()
            return ns.total, ns.done, ns.failed

    await runner.execute_run(rid, sf, asyncio.Semaphore(8), asyncio.Event())
    before = await qc_state()
    print(f"首跑后 qc 状态 (total,done,failed)={before}  ← 真实：3 输入 2 通过 1 拒绝")
    # 模拟断点续跑：对同一 run 再次执行（resume_unfinished 的等效路径）
    await runner.execute_run(rid, sf, asyncio.Semaphore(8), asyncio.Event())
    after = await qc_state()
    print(f"再执行(续跑)后 qc 状态 (total,done,failed)={after}")
    verdict = "BUG 复现：通过/拒绝被清成 done=total failed=0" if after != before and after[2] == 0 else "状态稳定"
    print(f"==> {verdict}")


# ------------------------------------------------------------------- 大量跑数
async def stress_volume():
    banner("大量跑数：200 行 / 并发 16 / fanout 跑通性与计数自洽")
    import json as J
    from app.engine import runner
    from app.db import get_session_factory
    from app import crypto
    from sqlalchemy import func, select
    from app.models import (Dataset, DatasetRow, ModelConfig, Run, RunNodeState, RunRow, User,
                            Workflow, WorkflowVersion)
    _patch(lambda u: (f"答[{u}]", U) if not u.startswith("判定:")
           else (J.dumps({"pass": True, "reason": ""}), U))
    N = 200
    graph = {"nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": []}},
        {"id": "gen", "type": "llm_synth", "config": {
            "model_config_id": 0, "user_prompt": "Q:{{q}}", "output_column": "a", "concurrency": 16}},
        {"id": "qc", "type": "qc", "config": {
            "model_config_id": 0, "user_prompt": "判定:{{a}}", "concurrency": 16}},
        {"id": "out", "type": "output", "config": {}},
    ], "edges": [
        {"source": "in", "target": "gen", "kind": "normal"},
        {"source": "gen", "target": "qc", "kind": "normal"},
        {"source": "qc", "target": "out", "kind": "normal"}]}
    sf = get_session_factory()
    async with sf() as s:
        u = User(username="userV", max_llm_concurrency=16)
        s.add(u); await s.flush()
        mc = ModelConfig(user_id=u.id, name="m", model_name="f", base_url="x",
                         api_key_enc=crypto.encrypt("k"))
        s.add(mc); await s.flush()
        ds = Dataset(user_id=u.id, name="d", row_count=N)
        s.add(ds); await s.flush()
        await s.execute(__import__("sqlalchemy").insert(DatasetRow), [
            {"dataset_id": ds.id, "idx": i, "data_json": J.dumps({"q": f"问{i}"}, ensure_ascii=False)}
            for i in range(N)])
        g = J.loads(J.dumps(graph))
        for n in g["nodes"]:
            if n["type"] == "input":
                n["config"]["dataset_ids"] = [ds.id]
            if n["type"] in ("llm_synth", "qc"):
                n["config"]["model_config_id"] = mc.id
        wf = Workflow(user_id=u.id, name="wf", graph_json=J.dumps(g))
        s.add(wf); await s.flush()
        ver = WorkflowVersion(workflow_id=wf.id, version=1, graph_json=J.dumps(g))
        s.add(ver); await s.flush()
        run = Run(user_id=u.id, workflow_id=wf.id, workflow_version_id=ver.id)
        s.add(run); await s.commit()
        rid = run.id
    import time
    t0 = time.monotonic()
    await runner.execute_run(rid, sf, asyncio.Semaphore(16), asyncio.Event())
    dt = time.monotonic() - t0
    async with sf() as s:
        run = await s.get(Run, rid)
        gen_done = (await s.execute(select(func.count()).select_from(RunRow).where(
            RunRow.run_id == rid, RunRow.node_id == "gen", RunRow.status == "done"))).scalar()
        ns = (await s.execute(select(RunNodeState).where(
            RunNodeState.run_id == rid, RunNodeState.node_id == "qc"))).scalar_one()
    out_rows = await runner._node_outputs(sf, rid, "out")
    print(f"status={run.status} 耗时={dt:.1f}s gen_done={gen_done}/{N} "
          f"qc(total={ns.total},done={ns.done},failed={ns.failed}) out={len(out_rows)}")
    ok = run.status == "completed" and gen_done == N and len(out_rows) == N and ns.failed == 0
    print(f"==> {'计数自洽、跑通' if ok else '存在异常'}")


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="gf_bughunt_"))
    settings.data_dir = tmp
    print(f"[隔离临时库] {settings.db_url}")
    from app import db
    await db.init_db()
    for fn in (bug_delete_workflow_orphans, bug_rerun_failed_dup_qc_metric,
               bug_rescan_fanout_inflation, bug_resume_clobbers_qc_state, stress_volume):
        try:
            await fn()
        except Exception as e:
            import traceback
            print(f"[场景异常] {fn.__name__}: {e}")
            traceback.print_exc()
    await db.engine.dispose()
    print(f"\n[完成] 临时库 {tmp}")


asyncio.run(main())
