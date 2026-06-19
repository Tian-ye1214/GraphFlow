# 质检节点：枚举 status 列 + 下游可见 + 失败全量 jsonl 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 质检节点判定改枚举 status（pass=保存 / 其余回扫），产出 `qc_status`+`qc_feedback` 两列下游可见，并把最终失败样本全量导出为按模型平铺的 jsonl。

**Architecture:** 单模型判定契约从 `{"pass": bool}` 改为 `{"status": "<枚举>"}`，`status=="pass"` 为通过票，聚合到整行仍是二值 pass/failed（K-of-N 不变）。QC 写行时显式覆写两列；列血缘加 status 列；新增 `.jsonl` 导出端点把 `QcFailure.reasons_json`（per-model）平铺成 `model_1/model_1_reason/…`。

**Tech Stack:** FastAPI + SQLAlchemy2 async；pytest（asyncio_mode=auto）；React 19 + AntD 6 + vitest。

## Global Constraints

- KISS：最简实现，无防御性代码、无投机抽象。
- 租户隔离（user_id 校验）为硬红线：新端点必须经 `_get_owned_run`。
- API key / Authorization 绝不进任何日志、响应、提示词。
- 提交记录不含 "claude"、无 Co-Authored-By 尾注。
- 不 `git add` `项目设计.txt`、`.idea/`、`.codegraph/`（只显式 add 本计划列出的文件）。
- 后端测试只本地，不推 origin。
- 唯一通过值 = `pass`；status 自由字符串（不做固定枚举校验）；只存最终失败样本（不存每轮审计）。
- 测试命令：后端 `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider <路径>`；前端 `cd "E:/代码/GraphFlow/frontend" && npx vitest run <路径>`。
- 工具在中文路径 `E:\代码` 偶发不稳；如遇文件"消失"用 `git -C "E:/代码/GraphFlow" status` 核对，勿慌。

---

### Task 1: 判定契约 → 枚举 status（nodes.py + 提示词 + 判定测试）

**Files:**
- Modify: `backend/app/engine/nodes.py`（`run_qc_judge_row` 的 `judge_one` 与聚合；删除 `_truthy`）
- Modify: `backend/app/agent/prompts/qc_empty_anchor.md`
- Modify: `backend/app/agent/prompts/node_assist_qc.md`
- Test: `backend/tests/test_qc.py`, `test_qc_multi.py`, `test_qc_empty.py`, `test_qc_params.py`, `test_qc_adversarial.py`

**Interfaces:**
- Produces: `run_qc_judge_row(config, row, mcs, pass_k, user_sem) -> (ok: bool, reason: str, usage: dict, per_model: list)`，其中 `per_model[i] = {"model_config_id": int, "status": str, "reason": str}`；`ok = (status=="pass" 的票数) >= pass_k`。判定 JSON 契约 `{"status": "<str>", "reason": "<str>"}`。

- [ ] **Step 1: 改判定测试到新契约（先让测试失败）**

把 `backend/tests/test_qc.py` 全文替换为：

```python
import asyncio
import json

from app.engine import nodes
from app.services import llm


class _FakeMC:
    """最小 ModelConfig 替身，仅需 id 属性即可通过 nodes.run_qc_judge_row 内的 mc.id 访问。"""
    def __init__(self, id=1):
        self.id = id


async def test_qc_judge_parses_verdict(monkeypatch):
    async def fake(mc, system, user, params=None, retries=3):
        assert params and params.get("json_mode") is True  # 判定强制 json 模式
        assert "hello" in user  # 用 base 渲染（剥离 _qc_reason）
        return json.dumps({"status": "failed", "reason": "不是中文"}), {"prompt_tokens": 2, "completion_tokens": 3}

    monkeypatch.setattr(llm, "chat", fake)
    ok, reason, usage, per_model = await nodes.run_qc_judge_row(
        {"user_prompt": "译文:{{a}}"}, {"a": "hello", "_qc_reason": "旧"}, [_FakeMC()], 1, asyncio.Semaphore(1))
    assert ok is False and reason == "不是中文"
    assert per_model[0]["status"] == "failed"
    assert usage == {"prompt_tokens": 2, "completion_tokens": 3}


async def test_qc_judge_pass(monkeypatch):
    async def fake(mc, system, user, params=None, retries=3):
        return json.dumps({"status": "pass"}), {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake)
    ok, reason, _, per_model = await nodes.run_qc_judge_row(
        {"user_prompt": "判:{{a}}"}, {"a": "x"}, [_FakeMC()], 1, asyncio.Semaphore(1))
    assert ok is True and reason == "通过"  # 通过时 dissent 为空，返回"通过"
    assert per_model[0]["status"] == "pass"


async def test_qc_judge_status_normalized(monkeypatch):
    """status 归一：大小写/空白不敏感，"PASS"/" pass " 记为通过票。"""
    async def fake(mc, system, user, params=None, retries=3):
        return json.dumps({"status": " PASS "}), {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake)
    ok, *_ = await nodes.run_qc_judge_row(
        {"user_prompt": "判:{{a}}"}, {"a": "x"}, [_FakeMC()], 1, asyncio.Semaphore(1))
    assert ok is True


async def test_qc_judge_non_pass_status_fails(monkeypatch):
    """非 pass 的枚举值（如 factual_error）一律算不通过。"""
    async def fake(mc, system, user, params=None, retries=3):
        return json.dumps({"status": "factual_error", "reason": "事实错误"}), {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake)
    ok, reason, _, per_model = await nodes.run_qc_judge_row(
        {"user_prompt": "判:{{a}}"}, {"a": "x"}, [_FakeMC()], 1, asyncio.Semaphore(1))
    assert ok is False and "事实错误" in reason
    assert per_model[0]["status"] == "factual_error"  # 枚举原值保留供 jsonl 归类


async def test_qc_judge_missing_status_votes_fail(monkeypatch):
    """判定缺 status 字段：重试耗尽后判该模型「不通过」，不再抛错拖垮整个 run。"""
    monkeypatch.setattr(llm, "BACKOFF_BASE", 0)

    async def fake(mc, system, user, params=None, retries=3):
        return json.dumps({"reason": "x"}), {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm, "chat", fake)
    ok, reason, usage, per_model = await nodes.run_qc_judge_row(
        {"user_prompt": "p"}, {"a": "x"}, [_FakeMC()], 1, asyncio.Semaphore(1))
    assert ok is False                                            # 拿不准 → 判不过
    assert usage == {"prompt_tokens": 3, "completion_tokens": 3}  # 3 次重试都真实调用了模型
    assert per_model[0]["status"] == "failed"


async def test_qc_multi_model_metric_and_failures(auth_client, monkeypatch, session_factory):
    """两个判定模型 pass_k=2；部分行仅 1/2 通过 → QcFailure 落库；首轮指标 → QcMetric 落库。"""
    import json as _json

    from app.services import llm as llm_mod

    JSONL = ('{"q": "r0"}\n{"q": "r1"}\n{"q": "r2"}\n').encode("utf-8")
    files = [("files", ("data.jsonl", JSONL, "application/octet-stream"))]
    ds = (await auth_client.post("/api/datasets/upload", files=files)).json()[0]
    mc1 = (await auth_client.post("/api/models", json={
        "name": "judge1", "model_name": "qwen", "base_url": "http://x/v1",
        "api_key": "k1", "default_params": {}})).json()
    mc2 = (await auth_client.post("/api/models", json={
        "name": "judge2", "model_name": "qwen", "base_url": "http://x/v1",
        "api_key": "k2", "default_params": {}})).json()

    graph = {
        "nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [ds["id"]]}},
            {"id": "gen", "type": "llm_synth", "config": {
                "model_config_id": mc1["id"], "user_prompt": "Q:{{q}}",
                "output_column": "a", "concurrency": 4, "retries": 1}},
            {"id": "qc", "type": "qc", "config": {
                "judge_model_ids": [mc1["id"], mc2["id"]],
                "model_config_id": mc1["id"],  # 供 runs.py 资源校验
                "pass_k": 2,
                "user_prompt": "判断:{{a}}"}},
        ],
        "edges": [
            {"source": "in", "target": "gen", "kind": "normal"},
            {"source": "gen", "target": "qc", "kind": "normal"},
        ],
    }
    wf = (await auth_client.post("/api/workflows", json={"name": "qc多模型"})).json()
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})

    call_count = {"n": 0}

    async def fake_chat(mc, system, user, params=None, retries=3):
        if params and params.get("json_mode"):
            # QC 判定：mc1 通过，mc2 不通过
            if mc.id == mc1["id"]:
                return _json.dumps({"status": "pass", "reason": "好"}), {"prompt_tokens": 1, "completion_tokens": 1}
            else:
                return _json.dumps({"status": "failed", "reason": "不合格"}), {"prompt_tokens": 1, "completion_tokens": 1}
        call_count["n"] += 1
        return f"答{call_count['n']}", {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm_mod, "chat", fake_chat)

    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf["id"]})).json()["id"]

    for _ in range(100):
        await asyncio.sleep(0.05)
        r = (await auth_client.get(f"/api/runs/{run_id}")).json()
        if r["status"] in ("completed", "failed", "cancelled"):
            break

    from sqlalchemy import select
    from app.models import QcFailure, QcMetric
    async with session_factory() as s:
        metrics = (await s.execute(select(QcMetric).where(QcMetric.run_id == run_id))).scalars().all()
        failures = (await s.execute(select(QcFailure).where(QcFailure.run_id == run_id))).scalars().all()

    assert metrics, "QcMetric 应写入"
    assert all(0 <= m.first_round_pass <= m.total for m in metrics)
    assert metrics[0].total == 3
    assert metrics[0].first_round_pass == 0
    assert failures, "QcFailure 应写入"
    assert len(failures) == 3
    for f in failures:
        reasons = _json.loads(f.reasons_json)
        assert isinstance(reasons, list)
        assert reasons[0]["status"] == "pass" and reasons[1]["status"] == "failed"  # per-model 存 status
        sample = _json.loads(f.sample_json)
        assert "_qc_reason" not in sample and "_qc_per_model" not in sample
```

把 `backend/tests/test_qc_multi.py` 全文替换为：

```python
import asyncio
import json

import app.engine.nodes as nodes


def _fake_chat_factory(verdict_by_model):
    async def fake_chat(mc, system, user, params=None, retries=3):
        v = verdict_by_model[mc.id]
        return json.dumps(v), {"prompt_tokens": 1, "completion_tokens": 1}
    return fake_chat


class _MC:
    def __init__(self, id): self.id = id


async def test_k_of_n_pass(monkeypatch):
    monkeypatch.setattr(nodes.llm, "chat", _fake_chat_factory({
        1: {"status": "pass", "reason": "好"}, 2: {"status": "failed", "reason": "太短"},
        3: {"status": "pass", "reason": "好"}}))
    sem = asyncio.Semaphore(4)
    cfg = {"system_prompt": "", "user_prompt": "{{q}}"}
    ok, reason, usage, per_model = await nodes.run_qc_judge_row(
        cfg, {"q": "hello"}, [_MC(1), _MC(2), _MC(3)], 2, sem)
    assert ok is True                       # 2/3 通过 ≥ K=2
    assert usage == {"prompt_tokens": 3, "completion_tokens": 3}
    assert {p["model_config_id"] for p in per_model} == {1, 2, 3}


async def test_k_of_n_fail_aggregates_reasons(monkeypatch):
    monkeypatch.setattr(nodes.llm, "chat", _fake_chat_factory({
        1: {"status": "failed", "reason": "太短"}, 2: {"status": "factual_error", "reason": "跑题"}}))
    sem = asyncio.Semaphore(4)
    ok, reason, usage, per_model = await nodes.run_qc_judge_row(
        {"system_prompt": "", "user_prompt": "{{q}}"}, {"q": "x"}, [_MC(1), _MC(2)], 2, sem)
    assert ok is False                      # 0/2 ≥ 2 → 不通过
    assert "太短" in reason and "跑题" in reason
    assert {p["status"] for p in per_model} == {"failed", "factual_error"}
```

在 `backend/tests/test_qc_empty.py`：把 `test_judge_uses_temperature_zero_and_anchor` 内
`return '{"pass": true, "reason": "ok"}', ...` 改为 `return '{"status": "pass", "reason": "ok"}', ...`，
并把断言 `assert "pass:false" in seen["system"]` 改为 `assert "status:failed" in seen["system"]`。
（`test_empty_sample_fails_without_judge` 不改：空样本仍 `ok False`、`per_model == []`、reason 含"空"。）

在 `backend/tests/test_qc_params.py`：两处 `return '{"pass": true, "reason": "ok"}', ...` 改为
`return '{"status": "pass", "reason": "ok"}', ...`（仅改 fake 返回，断言不变）。

在 `backend/tests/test_qc_adversarial.py`：
- `fake_chat` 内 `return json.dumps({"pass": False, "reason": "乱码"}), USAGE` → `{"status": "failed", "reason": "乱码"}`；
- `return json.dumps({"pass": True, "reason": "ok"}), USAGE` → `{"status": "pass", "reason": "ok"}`；
- 把 `test_qc_string_false_verdict_must_count_as_fail` 整个函数替换为：

```python
async def test_qc_non_pass_status_must_count_as_fail(monkeypatch):
    """judge 返回非 pass 的 status（如分类失败值）必须判为不通过。"""
    async def fake(mc, system, user, params=None, retries=3):
        return json.dumps({"status": "factual_error", "reason": "明显不合格"}), USAGE

    monkeypatch.setattr(nodes.llm, "chat", fake)
    ok, reason, _, per_model = await nodes.run_qc_judge_row(
        {"user_prompt": "判:{{a}}"}, {"a": "垃圾内容"}, [_MC(1)], 1, asyncio.Semaphore(1))
    assert ok is False and per_model[0]["status"] == "factual_error"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_qc.py tests/test_qc_multi.py tests/test_qc_empty.py tests/test_qc_params.py tests/test_qc_adversarial.py`
Expected: FAIL（旧 `run_qc_judge_row` 读 `verdict["pass"]`、per_model 用 `pass` 键，新断言 `status` 失败；anchor 文本断言失败）。

- [ ] **Step 3: 改 `run_qc_judge_row`（nodes.py）**

把 `backend/app/engine/nodes.py` 中 `_truthy` 函数（约 220-225 行）整段删除。把 `run_qc_judge_row` 的 `judge_one` 及其后聚合段替换为：

```python
    async def judge_one(mc: ModelConfig):
        """单模型判定：报错/非 JSON/缺 status/空回复/限流等一律重试，retries 次仍失败即投「不通过」票。
        绝不向上抛——一行里某个判定模型抽风不该拖垮整个质检节点与 run（对照 llm_synth 的逐行隔离）。"""
        usage_acc = {"prompt_tokens": 0, "completion_tokens": 0}
        last_err = None
        for attempt in range(retries):
            try:
                async with user_sem:
                    text, usage = await llm.chat(mc, system, user, params=params, retries=1)
                usage_acc["prompt_tokens"] += usage["prompt_tokens"]
                usage_acc["completion_tokens"] += usage["completion_tokens"]
                verdict = _json.loads(text)
                if "status" not in verdict:
                    raise ValueError("判定未返回 status 字段")
                return mc.id, str(verdict["status"]).strip(), str(verdict.get("reason") or "未通过质检"), usage_acc
            except Exception as e:
                last_err = e
                if attempt < retries - 1:
                    await asyncio.sleep(llm.BACKOFF_BASE * 2 ** attempt)
        return mc.id, "failed", f"判定重试 {retries} 次仍失败：{last_err}", usage_acc

    results = await asyncio.gather(*[judge_one(mc) for mc in mcs])
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    per_model, n_pass, dissent = [], 0, []
    for mc_id, status, reason, usage in results:
        usage_total["prompt_tokens"] += usage["prompt_tokens"]
        usage_total["completion_tokens"] += usage["completion_tokens"]
        per_model.append({"model_config_id": mc_id, "status": status, "reason": reason})
        if status.lower() == "pass":
            n_pass += 1
        else:
            dissent.append(reason)
    return n_pass >= pass_k, ("；".join(dissent) if dissent else "通过"), usage_total, per_model
```

（`run_qc_judge_row` 的文档串可保留；空样本分支、`system`/`params` 行不动。）

- [ ] **Step 4: 改提示词**

`backend/app/agent/prompts/qc_empty_anchor.md` 全文改为：

```
硬性规则：若待判定内容为空或缺少必要字段，必须返回 status:failed。
```

`backend/app/agent/prompts/node_assist_qc.md` 第 9 行
`- 提示词要引导模型只输出 {"pass": true|false, "reason": "<不通过原因>"}。`
改为：

```
- 提示词要引导模型只输出 {"status": "<状态>", "reason": "<原因>"}：通过填 "pass"，不通过填 "failed" 或更具体的失败分类（如 "factual_error"）。只有 "pass" 算通过。
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_qc.py tests/test_qc_multi.py tests/test_qc_empty.py tests/test_qc_params.py tests/test_qc_adversarial.py`
Expected: PASS（全绿）。

- [ ] **Step 6: 提交**

```bash
git -C "E:/代码/GraphFlow" add backend/app/engine/nodes.py backend/app/agent/prompts/qc_empty_anchor.md backend/app/agent/prompts/node_assist_qc.md backend/tests/test_qc.py backend/tests/test_qc_multi.py backend/tests/test_qc_empty.py backend/tests/test_qc_params.py backend/tests/test_qc_adversarial.py
git -C "E:/代码/GraphFlow" commit -m "feat(qc): 判定契约改枚举 status（pass=通过/其余不通过），per_model 存 status"
```

---

### Task 2: QC 行写 status + feedback 两列（runner.py）

**Files:**
- Modify: `backend/app/engine/runner.py`（`_run_qc_node`：`status_col` 与 `judge_all` 写行）
- Test: `backend/tests/test_qc_columns.py`（新建）

**Interfaces:**
- Consumes: `run_qc_judge_row(...) -> (ok, reason, usage, per_model)`（Task 1）。
- Produces: QC 节点输出行（通过行）带 `status_column`（默认 `qc_status`）= `"pass"`、`feedback_column`（默认 `qc_feedback`）= `""`；失败行带 `qc_status="failed"`、`qc_feedback=reason`。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_qc_columns.py`：

```python
"""QC 节点输出行的两列（qc_status / qc_feedback）落库语义。"""
import asyncio
import json

from app.engine import runner
from app.services import llm as llm_mod


async def _run(auth_client, monkeypatch, session_factory, *, pass_status):
    JSONL = ('{"q": "r0"}\n{"q": "r1"}\n').encode("utf-8")
    files = [("files", ("d.jsonl", JSONL, "application/octet-stream"))]
    ds = (await auth_client.post("/api/datasets/upload", files=files)).json()[0]
    mc = (await auth_client.post("/api/models", json={
        "name": "j", "model_name": "qwen", "base_url": "http://x/v1",
        "api_key": "k", "default_params": {}})).json()
    graph = {
        "nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [ds["id"]]}},
            {"id": "gen", "type": "llm_synth", "config": {
                "model_config_id": mc["id"], "user_prompt": "Q:{{q}}",
                "output_column": "a", "retries": 1}},
            {"id": "qc", "type": "qc", "config": {
                "judge_model_ids": [mc["id"]], "model_config_id": mc["id"],
                "pass_k": 1, "user_prompt": "判:{{a}}"}},
            {"id": "out", "type": "output", "config": {}},
        ],
        "edges": [
            {"source": "in", "target": "gen", "kind": "normal"},
            {"source": "gen", "target": "qc", "kind": "normal"},
            {"source": "qc", "target": "out", "kind": "normal"},
        ],
    }
    wf = (await auth_client.post("/api/workflows", json={"name": "qc列"})).json()
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})

    async def fake_chat(mc_, system, user, params=None, retries=3):
        if params and params.get("json_mode"):
            return json.dumps({"status": pass_status, "reason": "审稿意见"}), {"prompt_tokens": 1, "completion_tokens": 1}
        return "答", {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(llm_mod, "chat", fake_chat)
    monkeypatch.setattr(llm_mod, "BACKOFF_BASE", 0)
    run_id = (await auth_client.post("/api/runs", json={"workflow_id": wf["id"]})).json()["id"]
    for _ in range(120):
        await asyncio.sleep(0.05)
        r = (await auth_client.get(f"/api/runs/{run_id}")).json()
        if r["status"] in ("completed", "failed", "cancelled"):
            break
    return run_id


async def test_passed_rows_carry_pass_status_and_blank_feedback(auth_client, monkeypatch, session_factory):
    run_id = await _run(auth_client, monkeypatch, session_factory, pass_status="pass")
    rows = await runner._node_outputs(session_factory, run_id, "qc")
    assert rows and all(r["qc_status"] == "pass" and r["qc_feedback"] == "" for r in rows)


async def test_failed_rows_recorded_with_failed_status(auth_client, monkeypatch, session_factory):
    run_id = await _run(auth_client, monkeypatch, session_factory, pass_status="failed")
    # 无 rescan 边、全失败 → qc 输出为空，失败样本入 QcFailure（per-model 含 status）
    rows = await runner._node_outputs(session_factory, run_id, "qc")
    assert rows == []
    from sqlalchemy import select
    from app.models import QcFailure
    async with session_factory() as s:
        failures = (await s.execute(select(QcFailure).where(QcFailure.run_id == run_id))).scalars().all()
    assert len(failures) == 2
    assert json.loads(failures[0].reasons_json)[0]["status"] == "failed"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_qc_columns.py`
Expected: FAIL（`KeyError: 'qc_status'`——通过行尚未写 status 列）。

- [ ] **Step 3: 改 `_run_qc_node`（runner.py）**

在 `backend/app/engine/runner.py` 的 `_run_qc_node` 顶部，`feedback_col = cfg.get("feedback_column") or "qc_feedback"` 之后新增一行：

```python
    status_col = cfg.get("status_column") or "qc_status"
```

把 `judge_all` 内的分流写行段：

```python
        passed_, failed_ = [], []
        for row, (ok, reason, u, per_model) in zip(rows, await asyncio.gather(*[judge(r) for r in rows])):
            fold(u)
            if ok:
                passed_.append({**nodes.strip_qc_internal(row), feedback_col: row.get(feedback_col, "")})
            else:
                failed_.append({**row, feedback_col: reason,
                                "_qc_reason": reason, "_qc_per_model": per_model})
        return passed_, failed_
```

替换为（通过/失败行均显式覆写两列，杜绝回扫残留旧值）：

```python
        passed_, failed_ = [], []
        for row, (ok, reason, u, per_model) in zip(rows, await asyncio.gather(*[judge(r) for r in rows])):
            fold(u)
            if ok:
                passed_.append({**nodes.strip_qc_internal(row), status_col: "pass", feedback_col: ""})
            else:
                failed_.append({**row, status_col: "failed", feedback_col: reason,
                                "_qc_reason": reason, "_qc_per_model": per_model})
        return passed_, failed_
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_qc_columns.py`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git -C "E:/代码/GraphFlow" add backend/app/engine/runner.py backend/tests/test_qc_columns.py
git -C "E:/代码/GraphFlow" commit -m "feat(qc): 输出行显式写 qc_status/qc_feedback 两列"
```

---

### Task 3: 列血缘加 status 列（columns.py）

**Files:**
- Modify: `backend/app/engine/columns.py`（`_typed_output` 的 qc 分支）
- Test: `backend/tests/test_columns.py`（改 `test_qc_passthrough_and_rescan_ignored` + 新增自定义列名用例）

**Interfaces:**
- Produces: qc 节点静态输出列 = `input ∪ [status_column or "qc_status", feedback_column or "qc_feedback"]`。

- [ ] **Step 1: 改/加测试（先失败）**

在 `backend/tests/test_columns.py`：把 `test_qc_passthrough_and_rescan_ignored` 的断言
`assert cols["qc"]["output"] == ["q", "a", "qc_feedback"]`
改为：

```python
    assert cols["qc"]["output"] == ["q", "a", "qc_status", "qc_feedback"]   # qc 产出含状态列+反馈列
```

并在该函数后新增：

```python
def test_qc_custom_status_and_feedback_columns():
    g = _g(
        [{"id": "in", "type": "input", "config": {"dataset_ids": [1]}},
         {"id": "ls", "type": "llm_synth", "config": {"output_column": "a"}},
         {"id": "qc", "type": "qc", "config": {"status_column": "verdict", "feedback_column": "fb"}}],
        [{"source": "in", "target": "ls", "kind": "normal"},
         {"source": "ls", "target": "qc", "kind": "normal"}])
    cols = propagate_columns(g, {1: ["q"]})
    assert cols["qc"]["output"] == ["q", "a", "verdict", "fb"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_columns.py -k qc`
Expected: FAIL（输出列缺 status 列）。

- [ ] **Step 3: 改 `_typed_output`（columns.py）**

把 `backend/app/engine/columns.py` 中 qc 分支：

```python
    if t == "qc":
        return _ordered_union([input_cols, [node.config.get("feedback_column") or "qc_feedback"]])
```

替换为：

```python
    if t == "qc":
        return _ordered_union([input_cols, [node.config.get("status_column") or "qc_status",
                                            node.config.get("feedback_column") or "qc_feedback"]])
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_columns.py`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git -C "E:/代码/GraphFlow" add backend/app/engine/columns.py backend/tests/test_columns.py
git -C "E:/代码/GraphFlow" commit -m "feat(qc): 列血缘 qc 输出含 status 列（下游可见）"
```

---

### Task 4: 失败全量 jsonl 导出端点（runs.py）

**Files:**
- Modify: `backend/app/routers/runs.py`（import `Response`；新增 `GET /{run_id}/qc-failures.jsonl`）
- Test: `backend/tests/test_qc_api.py`（新增 jsonl 导出 + 租户隔离用例）

**Interfaces:**
- Produces: `GET /api/runs/{run_id}/qc-failures.jsonl?node_id=` → `application/x-ndjson`，逐行一个 JSON，每行 = `{...样本字段, "model_1": status1, "model_1_reason": reason1, ...}`（序号 1-based 按 `reasons_json` 顺序）；经 `_get_owned_run` 租户隔离。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_qc_api.py` 末尾追加：

```python
async def test_qc_failures_jsonl_export(auth_client, session_factory):
    import json as _json
    from sqlalchemy import select
    from app.models import Run, User
    async with session_factory() as s:
        uid = (await s.execute(select(User).where(User.username == "tester"))).scalar_one().id
        run = Run(user_id=uid, workflow_id=0, workflow_version_id=0, status="completed")
        s.add(run); await s.commit(); run_id = run.id
        s.add(QcFailure(run_id=run_id, node_id="qc1",
                        sample_json='{"q":"x","a":"答"}',
                        reasons_json=_json.dumps([
                            {"model_config_id": 1, "status": "pass", "reason": "好"},
                            {"model_config_id": 2, "status": "factual_error", "reason": "事实错"}])))
        await s.commit()
    resp = await auth_client.get(f"/api/runs/{run_id}/qc-failures.jsonl")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    lines = [l for l in resp.text.splitlines() if l]
    assert len(lines) == 1
    rec = _json.loads(lines[0])
    assert rec["q"] == "x" and rec["a"] == "答"
    assert rec["model_1"] == "pass" and rec["model_1_reason"] == "好"
    assert rec["model_2"] == "factual_error" and rec["model_2_reason"] == "事实错"


async def test_qc_failures_jsonl_rejects_foreign_run(auth_client, session_factory):
    from sqlalchemy import select
    from app.models import Run, User
    async with session_factory() as s:
        stranger = User(username="stranger2", display_name="s2")
        s.add(stranger); await s.commit()
        run = Run(user_id=stranger.id, workflow_id=0, workflow_version_id=0, status="completed")
        s.add(run); await s.commit(); rid = run.id
    assert (await auth_client.get(f"/api/runs/{rid}/qc-failures.jsonl")).status_code == 404
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_qc_api.py`
Expected: FAIL（404/路由不存在）。

- [ ] **Step 3: 实现端点（runs.py）**

把 `backend/app/routers/runs.py` 的 import 行
`from fastapi.responses import FileResponse`
改为：

```python
from fastapi.responses import FileResponse, Response
```

在现有 `run_qc_failures`（`@router.get("/{run_id}/qc-failures")`）函数之后新增：

```python
@router.get("/{run_id}/qc-failures.jsonl")
async def run_qc_failures_jsonl(run_id: int, node_id: str | None = None,
                                user: User = Depends(get_current_user),
                                session: AsyncSession = Depends(get_session)):
    """最终失败样本全量导出为 jsonl：每行 = 样本字段 + 各判定模型平铺 model_i/model_i_reason。"""
    await _get_owned_run(run_id, user, session)
    stmt = select(QcFailure).where(QcFailure.run_id == run_id)
    if node_id is not None:
        stmt = stmt.where(QcFailure.node_id == node_id)
    rows = (await session.execute(stmt.order_by(QcFailure.id))).scalars().all()
    lines = []
    for f in rows:
        rec = json.loads(f.sample_json)
        for i, pm in enumerate(json.loads(f.reasons_json), start=1):
            rec[f"model_{i}"] = pm.get("status", "")
            rec[f"model_{i}_reason"] = pm.get("reason", "")
        lines.append(json.dumps(rec, ensure_ascii=False))
    return Response(content="\n".join(lines), media_type="application/x-ndjson",
                    headers={"Content-Disposition": f'attachment; filename="run{run_id}_qc_failures.jsonl"'})
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider tests/test_qc_api.py`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git -C "E:/代码/GraphFlow" add backend/app/routers/runs.py backend/tests/test_qc_api.py
git -C "E:/代码/GraphFlow" commit -m "feat(qc): 新增失败样本 .jsonl 全量导出端点（per-model 平铺）"
```

---

### Task 5: 前端 status 列名字段 + 血缘 + jsonl 下载

**Files:**
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx`（`liveOutput` qc 分支；qc 表单加「状态列名」）
- Modify: `frontend/src/pages/RunDetailPage.tsx`（失败样本下载改命中 `.jsonl`）
- Test: `frontend/src/canvas/forms/NodeConfigForm.test.tsx`（改 qc 输出列用例 + 新增状态列名用例）

**Interfaces:**
- Consumes: 后端 `/columns` 已含 `qc_status`（Task 3）；`/api/runs/{id}/qc-failures.jsonl`（Task 4）。

- [ ] **Step 1: 改/加前端测试（先失败）**

在 `frontend/src/canvas/forms/NodeConfigForm.test.tsx`：

把 `shows qc feedback as a produced output column` 用例改为（输出列现含 qc_status）：

```tsx
  it('shows qc feedback and status as produced output columns', async () => {
    mockColumns({ qc: { input: ['q', 'answer'], output: ['q', 'answer', 'qc_status', 'qc_feedback'] } })

    render(<NodeConfigForm type="qc" workflowId={1} nodeId="qc" config={{}} onChange={() => {}} />)

    await screen.findByText('输出列 (4) ▾')
    expect(screen.getByText('qc_status')).toBeInTheDocument()
    expect(screen.getByText('qc_feedback')).toBeInTheDocument()
  })
```

在 `describe('NodeConfigForm QC feedback column', ...)` 内新增：

```tsx
  it('shows configurable status column on qc forms', async () => {
    mockColumns({ qc: { input: ['q', 'answer'], output: ['q', 'answer', 'qc_status', 'qc_feedback'] } })

    render(<NodeConfigForm type="qc" workflowId={1} nodeId="qc" config={{}} onChange={() => {}} />)

    fireEvent.click(await screen.findByText('高级（回扫 / 反馈 / 参数）'))
    expect(await screen.findByText('状态列名')).toBeInTheDocument()
    expect(screen.getByDisplayValue('qc_status')).toBeInTheDocument()
  })
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd "E:/代码/GraphFlow/frontend" && npx vitest run src/canvas/forms/NodeConfigForm.test.tsx`
Expected: FAIL（输出列计数为 3、无「状态列名」）。

- [ ] **Step 3: 改 `NodeConfigForm.tsx`**

把 `liveOutput` 的 qc 分支（约 120 行）：

```tsx
  if (type === 'qc') return sub(uniq([...inputCols, config.feedback_column || 'qc_feedback']))
```

替换为：

```tsx
  if (type === 'qc') return sub(uniq([...inputCols, config.status_column || 'qc_status', config.feedback_column || 'qc_feedback']))
```

在 qc 表单「反馈列名」Field（约 579-582 行）之后新增「状态列名」Field：

```tsx
            <Field label="状态列名">
              <Input value={config.status_column ?? 'qc_status'}
                     onChange={(e) => patch({ status_column: e.target.value || 'qc_status' })} />
            </Field>
```

- [ ] **Step 4: 改 `RunDetailPage.tsx` 失败样本下载为 jsonl**

把「质检失败样本」卡片的 `extra`（约 156-162 行）：

```tsx
        <Card size="small" title={`质检失败样本（${qcFailures.length}）`} style={{ marginBottom: 16 }}
              extra={<Button size="small" onClick={() => {
                const blob = new Blob([JSON.stringify(qcFailures, null, 2)], { type: 'application/json' })
                const url = URL.createObjectURL(blob)
                const a = document.createElement('a'); a.href = url; a.download = `run${id}_qc_failures.json`; a.click()
                URL.revokeObjectURL(url)
              }}>下载</Button>}>
```

替换为（直接命中后端 jsonl 端点，按模型平铺）：

```tsx
        <Card size="small" title={`质检失败样本（${qcFailures.length}）`} style={{ marginBottom: 16 }}
              extra={<Button size="small" onClick={() => window.open(`/api/runs/${id}/qc-failures.jsonl`)}>下载 jsonl</Button>}>
```

- [ ] **Step 5: 跑前端测试 + 构建确认通过**

Run: `cd "E:/代码/GraphFlow/frontend" && npx vitest run src/canvas/forms/NodeConfigForm.test.tsx && npm run build`
Expected: PASS（vitest 全绿）+ build 无类型错误。

- [ ] **Step 6: 提交**

```bash
git -C "E:/代码/GraphFlow" add frontend/src/canvas/forms/NodeConfigForm.tsx frontend/src/canvas/forms/NodeConfigForm.test.tsx frontend/src/pages/RunDetailPage.tsx
git -C "E:/代码/GraphFlow" commit -m "feat(qc): 前端状态列名字段 + 两列血缘 + 失败 jsonl 下载"
```

---

### Task 6: 全量回归

**Files:** 无（仅验证）

- [ ] **Step 1: 后端全量**

Run: `cd "E:/代码/GraphFlow/backend" && python -m pytest -q -p no:cacheprovider`
Expected: 全绿（在批前基线 + 本批新增用例之上无失败）。

- [ ] **Step 2: 前端全量 + 构建**

Run: `cd "E:/代码/GraphFlow/frontend" && npx vitest run && npm run build`
Expected: 全绿 + build 干净。

- [ ] **Step 3: 收尾**

按 superpowers:finishing-a-development-branch 处理（合并到 master / 删分支，按用户选择；不推 origin）。

---

## 自查

**Spec 覆盖：**
- 需求 1（两列下游可见）→ Task 2（落行）+ Task 3（血缘）+ Task 5（前端字段/显示）。✓
- 需求 2（枚举 status：pass 保存其余回扫，聚合二值）→ Task 1（判定契约）+ Task 2（行 status=pass/failed）；回扫路由本就 pass→保存/fail→回扫，未改。✓
- 需求 3（失败全量 jsonl，per-model 平铺）→ Task 1（per_model 存 status）+ Task 4（jsonl 端点）+ Task 5（下载）。✓
- 非目标（不配置通过集合/不固定枚举校验/不存每轮审计/聚合二值）→ 计划未引入，符合。✓

**占位扫描：** 无 TBD/TODO；每个改代码步骤均含完整代码块与确切路径。✓

**类型一致：** `run_qc_judge_row` 返回 4 元组 `(ok, reason, usage, per_model)` 全程一致；`per_model[i]` 键 `{model_config_id, status, reason}` 在 Task 1/2/4 一致；列名默认 `qc_status`/`qc_feedback` 在 runner/columns/前端一致；端点 `/{run_id}/qc-failures.jsonl` 在 Task 4/5 一致。✓
