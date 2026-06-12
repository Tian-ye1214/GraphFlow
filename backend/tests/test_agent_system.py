import json

from pydantic_ai.messages import (ModelRequest, ModelResponse, TextPart, ToolCallPart,
                                  ToolReturnPart)
from pydantic_ai.models.function import FunctionModel

from app.agent.system import AgentSystem


def _tool_returns(messages):
    return [p for m in messages if isinstance(m, ModelRequest)
            for p in m.parts if isinstance(p, ToolReturnPart)]


def _system(tmp_path, coordinator, manager=None, worker=None):
    echo = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("SUCCESS: ok")]))
    return AgentSystem(
        models={"coordinator": coordinator, "manager": manager or echo, "worker": worker or echo},
        workdir=tmp_path, confirm_delete=False, emit=None)


async def test_direct_answer(tmp_path):
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("直答")]))
    system = _system(tmp_path, model)
    history, output = await system.run_turn("你好", [])
    assert output == "直答"
    assert len(history) >= 2  # 请求 + 响应


async def test_coordinator_uses_write_file_tool(tmp_path):
    def fn(messages, info):
        if not _tool_returns(messages):
            return ModelResponse(parts=[ToolCallPart(
                tool_name="write_file", args={"path": "out.txt", "content": "数据"})])
        return ModelResponse(parts=[TextPart("写好了")])

    system = _system(tmp_path, FunctionModel(fn))
    _, output = await system.run_turn("写个文件", [])
    assert output == "写好了"
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "数据"


async def test_history_carries_across_turns(tmp_path):
    model = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(f"轮{len(m)}")]))
    system = _system(tmp_path, model)
    h1, _ = await system.run_turn("一", [])
    h2, _ = await system.run_turn("二", h1)
    assert len(h2) > len(h1)


async def test_manager_three_phases(tmp_path):
    (tmp_path / "cli.json").write_text("{}", encoding="utf-8")

    def coordinator_fn(messages, info):
        if not _tool_returns(messages):
            return ModelResponse(parts=[ToolCallPart(
                tool_name="execute_task_with_manager", args={"user_input": "复杂任务"})])
        return ModelResponse(parts=[TextPart("汇报：" + str(_tool_returns(messages)[-1].content))])

    def manager_fn(messages, info):
        if not _tool_returns(messages):
            return ModelResponse(parts=[ToolCallPart(
                tool_name="create_todo_list",
                args={"tasks_json": json.dumps([{"id": "1", "description": "子任务一"}])})])
        return ModelResponse(parts=[TextPart("最终报告：子任务一已完成")])

    system = _system(tmp_path, FunctionModel(coordinator_fn),
                     manager=FunctionModel(manager_fn),
                     worker=FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("SUCCESS: 子任务一完成")])))
    _, output = await system.run_turn("做个复杂任务", [])
    assert "最终报告" in output
    assert system.task_manager.is_all_completed()


async def test_adhoc_worker_routing(tmp_path):
    (tmp_path / "cli.json").write_text("{}", encoding="utf-8")

    def coordinator_fn(messages, info):
        if not _tool_returns(messages):
            return ModelResponse(parts=[ToolCallPart(
                tool_name="execute_task_with_worker", args={"task_description": "单任务"})])
        return ModelResponse(parts=[TextPart("done")])

    system = _system(tmp_path, FunctionModel(coordinator_fn))
    _, output = await system.run_turn("派个活", [])
    assert output == "done"
