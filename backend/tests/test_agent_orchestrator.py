import json

from pydantic_ai.models.function import FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart

from app.agent.orchestrator import Task, TaskManager, TaskStatus, WorkerOrchestrator
from app.agent.skills import SKILLS_DIR, SkillsManager
from app.agent.tools import AgentToolkit


async def test_create_todo_validates():
    tm = TaskManager()
    assert "解析失败" in await tm.create_todo_list("not json")
    assert "重复" in await tm.create_todo_list(json.dumps(
        [{"id": "1", "description": "a"}, {"id": "1", "description": "b"}]))
    assert "未知依赖" in await tm.create_todo_list(json.dumps(
        [{"id": "1", "description": "a", "dependencies": ["9"]}]))
    assert "循环依赖" in await tm.create_todo_list(json.dumps(
        [{"id": "1", "description": "a", "dependencies": ["2"]},
         {"id": "2", "description": "b", "dependencies": ["1"]}]))
    assert "非法字符" in await tm.create_todo_list(json.dumps(
        [{"id": "a/b", "description": "a"}]))  # id 进文件名，斜杠会越界
    out = await tm.create_todo_list(json.dumps(
        [{"id": "1", "description": "搜 A"}, {"id": "2", "description": "搜 B"},
         {"id": "3", "description": "写报告", "dependencies": ["1", "2"]}]))
    assert "搜 A" in out and len(tm.tasks) == 3


def test_ready_waves_and_retry():
    tm = TaskManager()
    for tid, deps in (("1", []), ("2", []), ("3", ["1", "2"])):
        tm.tasks[tid] = Task(id=tid, description=f"t{tid}", dependencies=deps)
        tm.task_order.append(tid)
    assert [t.id for t in tm.get_all_ready_tasks()] == ["1", "2"]
    tm.mark_task_complete("1", "r1")
    assert [t.id for t in tm.get_all_ready_tasks()] == ["2"]
    tm.mark_task_failed("2", "boom")
    assert tm.tasks["2"].status is TaskStatus.PENDING  # 还有重试机会
    for _ in range(3):
        tm.mark_task_failed("2", "boom")
    assert tm.tasks["2"].status is TaskStatus.FAILED
    assert tm.has_failed_tasks() and not tm.is_all_completed()


def _worker_model(reply: str):
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(reply)])
    return FunctionModel(fn)


def _orch(tmp_path, model):
    sm = SkillsManager(SKILLS_DIR)
    tm = TaskManager()

    def make_tools(state_file):
        return AgentToolkit(tmp_path, state_file, confirm_delete=False).tools

    return tm, WorkerOrchestrator(task_manager=tm, worker_model=model, workdir=tmp_path,
                                  make_tools=make_tools, skills_manager=sm)


async def test_adhoc_worker_success(tmp_path):
    (tmp_path / "cli.json").write_text("{}", encoding="utf-8")
    tm, orch = _orch(tmp_path, _worker_model("SUCCESS: 完成了"))
    ok, out = await orch.execute_task_with_worker("做点事", user_goal="目标")
    assert ok and out.startswith("SUCCESS:")
    assert (tmp_path / "worker_adhoc_1_cli.json").exists()  # 独立 gf 状态副本


async def test_parallel_waves_complete(tmp_path):
    (tmp_path / "cli.json").write_text("{}", encoding="utf-8")
    tm, orch = _orch(tmp_path, _worker_model("SUCCESS: done"))
    await tm.create_todo_list(json.dumps(
        [{"id": "1", "description": "a"}, {"id": "2", "description": "b"},
         {"id": "3", "description": "c", "dependencies": ["1", "2"]}]))
    summary = await orch.execute_all_tasks_parallel("总目标")
    assert tm.is_all_completed()
    assert "3/3" in summary
    assert (tmp_path / "worker_1_cli.json").exists()
    assert (tmp_path / "worker_2_cli.json").exists()


async def test_failed_worker_marks_failed(tmp_path):
    (tmp_path / "cli.json").write_text("{}", encoding="utf-8")
    tm, orch = _orch(tmp_path, _worker_model("FAILED: 不行"))
    await tm.create_todo_list(json.dumps([{"id": "1", "description": "a"}]))
    summary = await orch.execute_all_tasks_parallel("总目标")
    assert tm.has_failed_tasks()
    assert "失败" in summary
