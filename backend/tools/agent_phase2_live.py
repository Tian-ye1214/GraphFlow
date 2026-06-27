"""Phase2 全生命周期工具真实活体测试：对正在运行的后端(:8000)+真实 DB+真实 DeepSeek。

覆盖：
  Part A1: GraphToolkit 搭 input→llm→output + RunToolkit start_run/get_run/read_run_rows 真跑验产出
           + export_workflow→import_workflow 往返 + restore_workflow_from_run(门禁两态)
  Part A2: PromptToolkit create→update(出新版)→list_versions→rollback→delete(门禁两态)
  Part A3: ModelToolkit create_model(带key)→返回串不含明文key→delete(门禁两态)
  Part A4: DatasetToolkit write_jsonl→upload_dataset→轮询ready→export_dataset→delete(门禁两态)
  Part B:  真实主 Agent(真实 DeepSeek)下指令「建链→启跑→查结果→建提示词」，
           验证 Phase2 工具被 Agent 自主调用

全程 admin 用户复用其 deepseek 模型，_p2live_ 标签资源建即删回基线，绝不输出任何密钥。

用法(backend 目录，后端须在 :8000 运行且已合并+重启)：
    PYTHONIOENCODING=utf-8 python tools/agent_phase2_live.py
"""
import asyncio
import io
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import httpx

BASE = "http://localhost:8000"
USER = "admin"
TAG = "_p2live_"


def _eid(msg: str) -> int:
    """从「... (#123)」串里提取整数 id，找不到返回 0。"""
    m = re.search(r"#(\d+)", msg)
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------------------
# Part A1: GraphToolkit 搭链 + RunToolkit 直驱真跑
# ---------------------------------------------------------------------------

async def part_a1_run(sf, uid, ds_name, model_name, wd):
    """A1: 搭链 + start_run/get_run/read_run_rows + export/import 往返 + restore 门禁两态。
    返回 (results, wf_id, imported_id, run_id)。"""
    from app.agent.graph_tools import GraphToolkit
    from app.agent.run_tools import RunToolkit

    tk = GraphToolkit(sf, uid, confirm_delete=True, workdir=wd)
    rtk = RunToolkit(sf, uid, confirm_delete=True)
    rtk_no = RunToolkit(sf, uid, confirm_delete=False)
    R = []

    def ck(name, ok, detail=""):
        R.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] A1.{name}: {detail}")

    # 1. 搭 input→llm→output
    wf_id = _eid(await tk.create_workflow(f"{TAG}run"))
    await tk.add_node(wf_id, "input", "in")
    await tk.add_node(wf_id, "llm", "g")
    await tk.add_node(wf_id, "output", "o")
    await tk.set_node_config(wf_id, "in", {"dataset": ds_name})
    await tk.set_node_config(wf_id, "g", {
        "model": model_name,
        "out": "ans",
        "prompt": "用一句话回答：{{q}}。只输出答案。",
        "max_tokens": "300",
        "think": "off",
    })
    await tk.connect_nodes(wf_id, "in", "g")
    await tk.connect_nodes(wf_id, "g", "o")
    shown = json.loads(await tk.show_workflow_graph(wf_id))
    node_ids = {n["id"] for n in shown["rows"]}
    ck("搭链(3节点+2边)", node_ids == {"in", "g", "o"} and len(shown["edges"]) == 2,
       f"nodes={sorted(node_ids)} edges={len(shown['edges'])}")

    # 2. RunToolkit.start_run 直驱
    start_msg = await rtk.start_run(wf_id)
    rid_m = re.search(r"#(\d+)", start_msg)
    ck("start_run 返回 run_id", bool(rid_m), f"msg={start_msg[:80]}")
    run_id = int(rid_m.group(1)) if rid_m else None

    # 3. 轮询 get_run 到终态（manager 在本进程 event loop 里跑）
    final_status = None
    if run_id:
        for _ in range(90):
            info = json.loads(await rtk.get_run(run_id))
            status = info.get("status")
            if status in ("completed", "failed", "cancelled"):
                final_status = status
                break
            await asyncio.sleep(2)
    ck("get_run 轮询 completed", final_status == "completed", f"status={final_status}")

    # 4. read_run_rows 验 LLM 节点 g 的 ans 产出列有值
    #    RunRow.data_json 存「该输入行产出的若干输出行」列表(fanout 模型)，故每行 data 是 list[dict]
    rows_data = []
    if run_id:
        rj = json.loads(await rtk.read_run_rows(run_id, "g"))
        rows_data = rj.get("rows", [])
    out_rows = [d for r in rows_data for d in (r.get("data") or []) if isinstance(d, dict)]
    ans_ok = bool(out_rows) and all(d.get("ans") for d in out_rows)
    sample = (out_rows[0].get("ans") if out_rows else "")
    ck("read_run_rows 产出 ans", ans_ok, f"n={len(out_rows)} sample={sample[:40]!r}")

    # 5. export_workflow → import_workflow 往返
    exp_msg = await tk.export_workflow(wf_id)
    ck("export_workflow 生成 gfpkg", "已导出" in exp_msg, f"msg={exp_msg}")
    pkg_rel = f"workflow_{wf_id}.gfpkg"
    imp_msg = await tk.import_workflow(pkg_rel)
    imported_id = _eid(imp_msg) if "已导入" in imp_msg else None
    ck("import_workflow 往返出新工作流", imported_id is not None, f"msg={imp_msg}")

    # 6. restore_workflow_from_run 门禁两态
    if run_id:
        guard_msg = await rtk_no.restore_workflow_from_run(run_id)
        ck("restore 未确认被拦", "确认" in guard_msg, f"guard={guard_msg[:60]}")
        restore_msg = await rtk.restore_workflow_from_run(run_id)
        ck("restore 确认后执行", "已把工作流恢复" in restore_msg, f"msg={restore_msg}")
    else:
        R.append(("restore 未确认被拦", False))
        R.append(("restore 确认后执行", False))
        print("[FAIL] A1.restore*: 跳过(run_id=None)")

    return R, wf_id, imported_id, run_id


# ---------------------------------------------------------------------------
# Part A2: PromptToolkit 全生命周期
# ---------------------------------------------------------------------------

async def part_a2_prompt(sf, uid):
    """A2: create→update(出新版)→list_versions→rollback→delete(门禁两态)。"""
    from app.agent.prompt_tools import PromptToolkit

    tk = PromptToolkit(sf, uid, confirm_delete=True)
    tk_no = PromptToolkit(sf, uid, confirm_delete=False)
    R = []

    def ck(name, ok, detail=""):
        R.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] A2.{name}: {detail}")

    # create
    c_msg = await tk.create_prompt(f"{TAG}hello", "你好 {{name}}", "测试提示词")
    pid = _eid(c_msg)
    ck("create_prompt", pid > 0, f"msg={c_msg}")

    # update → 出新版本
    await tk.update_prompt(pid, body="你好 {{name}}，欢迎！")
    vers_j = json.loads(await tk.list_prompt_versions(pid))
    vers = vers_j.get("rows", [])
    ck("update_prompt 出新版本", len(vers) >= 2, f"versions={[v['version'] for v in vers]}")

    # rollback 到 v1
    rb_msg = await tk.rollback_prompt(pid, 1)
    vers2_j = json.loads(await tk.list_prompt_versions(pid))
    vers2 = vers2_j.get("rows", [])
    latest_body = vers2[-1]["body"] if vers2 else ""
    ck("rollback_prompt 到 v1", "已回滚" in rb_msg and "你好 {{name}}" in latest_body,
       f"rb={rb_msg} latest={latest_body[:30]!r}")

    # delete 未确认拦
    guard = await tk_no.delete_prompt(pid)
    ck("delete_prompt 未确认被拦", "确认" in guard, f"guard={guard[:60]}")

    # 确认删
    del_msg = await tk.delete_prompt(pid)
    ck("delete_prompt 确认后删除", "已删除" in del_msg, f"msg={del_msg}")

    return R


# ---------------------------------------------------------------------------
# Part A3: ModelToolkit 门禁两态
# ---------------------------------------------------------------------------

async def part_a3_model(sf, uid, real_mid):
    """A3: create_model(带key)→返回串不含明文key→test_model(真实模型连通)→delete(门禁两态)。
    test_model 用 admin 现有真实 deepseek 模型 real_mid（fake key 模型连通必失败，不能用它测）。"""
    from app.agent.model_tools import ModelToolkit

    tk = ModelToolkit(sf, uid, confirm_delete=True)
    tk_no = ModelToolkit(sf, uid, confirm_delete=False)
    FAKE_KEY = "sk-p2live-testkey-9999"
    R = []

    def ck(name, ok, detail=""):
        R.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] A3.{name}: {detail}")

    c_msg = await tk.create_model(
        name=f"{TAG}testmodel",
        base_url="https://api.deepseek.com/v1",
        model_name="deepseek-chat",
        api_key=FAKE_KEY,
    )
    mid = _eid(c_msg)
    ck("create_model 成功有 id", mid > 0, f"msg={c_msg}")
    ck("create_model 返回串不含明文 key", FAKE_KEY not in c_msg, f"msg={c_msg}")

    # test_model：用 admin 真实 deepseek 模型连通（成功返回「连通正常：...」）
    t_msg = await tk.test_model(real_mid)
    ck("test_model 真实模型连通", t_msg.startswith("连通正常"), f"msg={t_msg[:80]}")

    # delete 未确认拦
    guard = await tk_no.delete_model(mid)
    ck("delete_model 未确认被拦", "确认" in guard, f"guard={guard[:60]}")

    # 确认删
    del_msg = await tk.delete_model(mid)
    ck("delete_model 确认后删除", "已删除" in del_msg, f"msg={del_msg}")

    return R


# ---------------------------------------------------------------------------
# Part A4: DatasetToolkit 上传/摄入/导出
# ---------------------------------------------------------------------------

async def part_a4_dataset(sf, uid, wd):
    """A4: write jsonl→upload_dataset→轮询ready→export_dataset→delete(门禁两态)。"""
    from app.agent.dataset_tools import DatasetToolkit
    from app.models import Dataset

    tk = DatasetToolkit(sf, uid, wd, confirm_delete=True)
    tk_no = DatasetToolkit(sf, uid, wd, confirm_delete=False)
    R = []

    def ck(name, ok, detail=""):
        R.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] A4.{name}: {detail}")

    # 写 jsonl 文件到 workdir
    fname = f"{TAG}sample.jsonl"
    (Path(wd) / fname).write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n"
                for r in [{"q": "苹果是什么颜色？"}, {"q": "天空是什么颜色？"}]),
        encoding="utf-8",
    )

    # upload_dataset
    up_msg = await tk.upload_dataset(fname, name=f"{TAG}ds")
    ck("upload_dataset 已摄入", "已上传摄入" in up_msg, f"msg={up_msg}")
    ds_id = _eid(up_msg)
    ck("upload_dataset 返回 id", ds_id > 0, f"msg={up_msg}")

    if ds_id:
        # 轮询 status=ready（后台 to_thread 摄入任务在本进程 event loop 里跑）
        ready = False
        ds_status = "unknown"
        for _ in range(60):
            async with sf() as s:
                ds = await s.get(Dataset, ds_id)
                if ds is not None:
                    ds_status = ds.status
                    if ds.status == "ready":
                        ready = True
                        break
                    if ds.status == "failed":
                        break
            await asyncio.sleep(0.5)
        ck("upload_dataset status=ready", ready, f"ds_id={ds_id} status={ds_status}")

        # export_dataset
        exp_msg = await tk.export_dataset(ds_id, format="jsonl")
        ck("export_dataset 写出文件", "已导出" in exp_msg, f"msg={exp_msg}")

        # delete 未确认拦
        guard = await tk_no.delete_dataset(ds_id)
        ck("delete_dataset 未确认被拦", "确认" in guard, f"guard={guard[:60]}")

        # 确认删
        del_msg = await tk.delete_dataset(ds_id)
        ck("delete_dataset 确认后删除", "已删除" in del_msg, f"msg={del_msg}")
    else:
        for item in ("upload_dataset status=ready", "export_dataset 写出文件",
                     "delete_dataset 未确认被拦", "delete_dataset 确认后删除"):
            R.append((item, False))
            print(f"[FAIL] A4.{item}: 跳过(ds_id=0)")

    return R


# ---------------------------------------------------------------------------
# REST 辅助类（登录/数据集/Agent 会话/清理）
# ---------------------------------------------------------------------------

class Rest:
    def __init__(self):
        self.c = httpx.Client(base_url=BASE, timeout=200, trust_env=False)
        self.c.post("/api/auth/login", json={"username": USER}).raise_for_status()
        self.smoke_ds: list[int] = []
        self.smoke_sess: list[int] = []

    def model_id(self):
        """返回 admin 的第一个 deepseek 模型 (id, name)，找不到返回 (None, None)。"""
        for m in self.c.get("/api/models").json():
            if "deepseek" in m["base_url"].lower():
                return m["id"], m["name"]
        return None, None

    def make_dataset(self, name: str, rows: list) -> tuple[int, str]:
        """上传 jsonl 数据集并轮询到 ready；返回 (ds_id, ds_name)。"""
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")
        ds = self.c.post(
            "/api/datasets/upload",
            files=[("files", (f"{name}.jsonl", io.BytesIO(body), "application/octet-stream"))],
        ).json()[0]
        self.smoke_ds.append(ds["id"])
        for _ in range(60):
            if self.c.get(f"/api/datasets/{ds['id']}").json().get("status") == "ready":
                break
            time.sleep(0.5)
        return ds["id"], ds["name"]

    def agent_build(self, model_id: int, ds_name: str, model_name: str,
                    budget: int = 300) -> tuple[int, set, str]:
        """创建会话、下多步指令；返回 (session_id, 工具调用名集合, 最终 assistant 文本)。"""
        sid = self.c.post(
            "/api/agent/sessions", json={"model_config_id": model_id}
        ).json()["id"]
        self.smoke_sess.append(sid)
        wf_name = f"{TAG}byagent"
        msg = (
            f"请用你的工具完成以下任务，不要问我，直接做完：\n"
            f"1) 新建工作流「{wf_name}」，加三节点(input/llm/output)，"
            f"输入节点用数据集「{ds_name}」，LLM 节点用模型「{model_name}」，"
            f"提示词「用一句话回答：{{{{q}}}}」，产出列名 ans，连线 input→llm→output；\n"
            f"2) 启动这条工作流运行，用 get_run 查状态直到完成（最多等 3 分钟）；\n"
            f"3) 用 read_run_rows 查看 LLM 节点 g 的输出行并告诉我结果；\n"
            f"4) 在提示词库里新建一个条目，名字「{TAG}summary」，"
            f"正文「对以下内容作一句话总结：{{{{text}}}}」；\n"
            f"完成后用一句话回报。"
        )
        self.c.post(f"/api/agent/sessions/{sid}/messages", json={"text": msg})
        t0 = time.monotonic()
        while time.monotonic() - t0 < budget:
            d = self.c.get(f"/api/agent/sessions/{sid}").json()
            if d.get("status") != "running":
                break
            time.sleep(3)
        detail = self.c.get(f"/api/agent/sessions/{sid}").json()
        tools: set[str] = set()
        final = ""
        for m in detail.get("messages", []):
            cont = m.get("content")
            if m["role"] == "tool" and isinstance(cont, dict):
                tools.add(cont.get("tool", ""))
            if m["role"] == "assistant":
                final = cont.get("text", "") if isinstance(cont, dict) else str(cont)
        return sid, tools, final

    def find_wf(self, name: str) -> dict | None:
        for w in self.c.get("/api/workflows").json():
            if w["name"] == name:
                return self.c.get(f"/api/workflows/{w['id']}").json()
        return None

    def cleanup(self):
        """删所有 _p2live_ 相关资源，确保回基线。只动标签资源，绝不误伤 admin 真实数据。"""
        # 只取消标签工作流的 run（不误伤 admin 其它真实 run）
        tagged_wf_ids = {w["id"] for w in self.c.get("/api/workflows").json()
                         if w["name"].startswith(TAG)}
        for r in self.c.get("/api/runs").json():
            if r["status"] in ("running", "queued") and r.get("workflow_id") in tagged_wf_ids:
                self.c.post(f"/api/runs/{r['id']}/cancel")
        # 删 _p2live_ 工作流
        for w in self.c.get("/api/workflows").json():
            if w["name"].startswith(TAG):
                self.c.delete(f"/api/workflows/{w['id']}")
        # 删 Agent 会话
        for sid in self.smoke_sess:
            self.c.delete(f"/api/agent/sessions/{sid}")
        # 删种子数据集（REST 上传）
        for did in self.smoke_ds:
            self.c.delete(f"/api/datasets/{did}")
        # 清残余 _p2live_ 数据集（DatasetToolkit 删除失败的）
        for d in self.c.get("/api/datasets").json():
            if d.get("name", "").startswith(TAG):
                self.c.delete(f"/api/datasets/{d['id']}")
        # 清 _p2live_ 提示词（Agent Part B 建的）
        for p in self.c.get("/api/prompts").json():
            if p.get("name", "").startswith(TAG):
                self.c.delete(f"/api/prompts/{p['id']}")
        # 清 _p2live_ 模型（A3 若中途失败，fake 模型不留在 admin 列表）
        for m in self.c.get("/api/models").json():
            if m.get("name", "").startswith(TAG):
                self.c.delete(f"/api/models/{m['id']}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

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
        print("[ABORT] admin 无 deepseek 模型")
        sys.exit(2)
    print(f"[模型] id={mid} name={mname}  [用户] {USER}#{uid}")

    ds_id, ds_name = rest.make_dataset(
        f"{TAG}seed",
        [{"q": "中国的首都是哪里？"}, {"q": "1+1 等于几？"}, {"q": "水的化学式？"}],
    )
    print(f"[数据集] {ds_name}#{ds_id} (3 行)")

    wd = Path(tempfile.mkdtemp(prefix="p2live_"))
    print(f"[工作目录] {wd}")

    results: list[tuple[str, bool]] = []
    try:
        print("\n--- Part A1: GraphToolkit 搭链 + RunToolkit 直驱真跑 ---")
        r1, _wf_id, _imported_id, _run_id = await part_a1_run(sf, uid, ds_name, mname, wd)
        results += r1

        print("\n--- Part A2: PromptToolkit 全生命周期 ---")
        results += await part_a2_prompt(sf, uid)

        print("\n--- Part A3: ModelToolkit 创建+连通+门禁+删除 ---")
        results += await part_a3_model(sf, uid, mid)

        print("\n--- Part A4: DatasetToolkit 上传+摄入+导出+门禁+删除 ---")
        results += await part_a4_dataset(sf, uid, wd)

        print("\n--- Part B: 真实主 Agent 驱动 Phase2 工具 ---")
        _sid, tools, final = rest.agent_build(mid, ds_name, mname)
        phase2_expected = {
            "start_run", "get_run", "read_run_rows", "create_prompt",
            "create_workflow", "add_node", "set_node_config", "connect_nodes",
        }
        used = tools & phase2_expected
        print(f"[Agent] 调用工具={sorted(tools)}")
        print(f"[Agent] 最终回复={final[:80]!r}")
        results.append(("B.Agent 调用了 Phase2 工具", bool(used)))
        print(f"[{'PASS' if used else 'FAIL'}] B.Agent 调用了 Phase2 工具: {sorted(used)}")
        wf_agent = rest.find_wf(f"{TAG}byagent")
        built_ok = bool(wf_agent)
        results.append(("B.Agent 建出工作流", built_ok))
        print(f"[{'PASS' if built_ok else 'FAIL'}] B.Agent 建出工作流: {built_ok}")

    finally:
        rest.cleanup()
        time.sleep(1)
        aw = len(rest.c.get("/api/workflows").json())
        ad = len(rest.c.get("/api/datasets").json())
        baseline_ok = aw == base_wf and ad == base_ds
        print(f"\n[CLEANUP] 工作流 {base_wf}→{aw} 数据集 {base_ds}→{ad} 回基线={baseline_ok}")
        results.append(("回基线(建即删)", baseline_ok))
        shutil.rmtree(wd, ignore_errors=True)

    passed = sum(1 for _, ok in results if ok)
    print(f"\n[结果] {passed}/{len(results)} 通过")
    for n, ok in results:
        if not ok:
            print(f"   ✗ {n}")
    sys.exit(0 if passed == len(results) else 1)


asyncio.run(main())
