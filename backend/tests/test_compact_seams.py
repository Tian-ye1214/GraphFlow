"""测试 compactor 接入 manager/worker seam 的接线正确性。
spy 替换 maybe_compact，验证当 compactor_mc 非 None 时被调用，None 时不调用。
"""
import json

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from app.agent.orchestrator import Task, TaskManager, WorkerOrchestrator
from app.agent.skills import SKILLS_DIR, SkillsManager
from app.agent.tools import AgentToolkit


# ── 最小假对象 ─────────────────────────────────────────────────────────────

class _FakeModelConfig:
    """足够让 resolve_compactor_model 判为 ModelConfig 的假体（不调用真 LLM）。"""
    model_name = "fake-model"


def _worker_model(reply: str = "SUCCESS: done"):
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(reply)])
    return FunctionModel(fn)


def _make_orch(tmp_path, compactor_mc):
    sm = SkillsManager(SKILLS_DIR)
    tm = TaskManager()

    def make_tools(state_file):
        return AgentToolkit(tmp_path, state_file, confirm_delete=False).tools

    orch = WorkerOrchestrator(
        task_manager=tm, worker_model=_worker_model(),
        workdir=tmp_path, make_tools=make_tools,
        skills_manager=sm, compactor_mc=compactor_mc)
    return tm, orch


# ── Worker seam ──────────────────────────────────────────────────────────────

async def test_worker_seam_calls_compact_when_set(tmp_path, monkeypatch):
    """_run_board_worker 在 compactor_mc 非 None 时应调用 maybe_compact。"""
    (tmp_path / "cli.json").write_text("{}", encoding="utf-8")

    calls = []

    async def spy(history, *, compactor_mc, running_mc, **kw):
        calls.append({"compactor_mc": compactor_mc, "running_mc": running_mc})
        return history

    monkeypatch.setattr("app.agent.orchestrator.maybe_compact", spy)

    fake_mc = _FakeModelConfig()
    tm, orch = _make_orch(tmp_path, compactor_mc=fake_mc)

    task = Task(id="1", description="测试任务")
    from app.agent.orchestrator import _MessageBoard
    board = _MessageBoard()

    await orch._run_board_worker(task, board, "总目标")

    assert len(calls) == 1
    assert calls[0]["compactor_mc"] is fake_mc
    assert calls[0]["running_mc"] is orch._worker_model


async def test_worker_seam_skips_compact_when_none(tmp_path, monkeypatch):
    """_run_board_worker 在 compactor_mc 为 None 时不应调用 maybe_compact。"""
    (tmp_path / "cli.json").write_text("{}", encoding="utf-8")

    calls = []

    async def spy(history, **kw):
        calls.append(True)
        return history

    monkeypatch.setattr("app.agent.orchestrator.maybe_compact", spy)

    tm, orch = _make_orch(tmp_path, compactor_mc=None)

    task = Task(id="1", description="测试任务")
    from app.agent.orchestrator import _MessageBoard
    board = _MessageBoard()

    await orch._run_board_worker(task, board, "总目标")

    assert calls == []


# ── Manager seam (via AgentSystem._compact helper) ───────────────────────────

async def test_manager_seam_calls_compact_when_set(tmp_path, monkeypatch):
    """execute_task_with_manager 在 compactor_mc 非 None 时 _compact 被调用两次（两个 manager.run）。"""
    (tmp_path / "cli.json").write_text("{}", encoding="utf-8")

    # 拦截 system 模块里的 maybe_compact
    calls = []

    async def spy(history, *, compactor_mc, running_mc, **kw):
        calls.append({"running_mc": running_mc})
        return history

    monkeypatch.setattr("app.agent.system.maybe_compact", spy)

    # 同时让 orchestrator 的 maybe_compact 也是 no-op spy（不影响 worker）
    monkeypatch.setattr("app.agent.orchestrator.maybe_compact", spy)

    from app.agent.system import AgentSystem
    from app.models import ModelConfig

    # 用真 ModelConfig 让 resolve_compactor_model 返回非 None
    # 但 _compact 内部不会走真 LLM（spy 直接返回 history）
    fake_mc = _FakeModelConfig()

    def manager_fn(messages, info):
        # 第一次调用：创建 todo；第二次（summary）：直接返回文本
        from pydantic_ai.messages import ModelRequest, ToolReturnPart
        tool_returns = [p for m in messages if isinstance(m, ModelRequest)
                        for p in m.parts if isinstance(p, ToolReturnPart)]
        if not tool_returns:
            from pydantic_ai.messages import ToolCallPart
            return ModelResponse(parts=[ToolCallPart(
                tool_name="create_todo_list",
                args={"tasks_json": json.dumps([{"id": "1", "description": "子任务"}])})])
        return ModelResponse(parts=[TextPart("最终报告")])

    echo = _worker_model("SUCCESS: 子任务完成")
    coord = _worker_model("done")

    # 直接构造 AgentSystem，但把 _compactor_mc 强行替换成 fake_mc
    system = AgentSystem(
        models={"coordinator": coord, "manager": FunctionModel(manager_fn), "worker": echo},
        workdir=tmp_path, confirm_delete=False, emit=None)
    system._compactor_mc = fake_mc
    system.orchestrator._compactor_mc = fake_mc

    await system.execute_task_with_manager("复杂任务")

    # 两次 manager.run 各触发一次 _compact -> spy，过滤出 running_mc = manager model 的调用
    manager_calls = [c for c in calls if c["running_mc"] is system.models["manager"]]
    assert len(manager_calls) >= 2
