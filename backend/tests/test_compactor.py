from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
import app.agent.compactor as cp


def _user(text):
    return ModelRequest(parts=[UserPromptPart(content=text)])


def _assistant(text):
    return ModelResponse(parts=[TextPart(content=text)])


async def test_below_threshold_passthrough():
    history = [_user("目标"), _assistant("做了点事")]
    out = await cp.maybe_compact(history, compactor_mc=None, running_mc=None,
                                 window=1_000_000, summarize=None)
    assert out is history                          # 未达 75%，原样返回


async def test_compaction_protects_head_and_tail():
    async def fake_summarize(text):
        return "【已完成】A【待完成】B"
    history = [_user("总目标")] + [_assistant(f"中间{i}") for i in range(50)] + [_user("最近一步")]
    out = await cp.maybe_compact(history, compactor_mc=object(), running_mc=object(),
                                 window=10, summarize=fake_summarize)  # window 极小 -> 必触发
    assert len(out) < len(history)                 # 确实压缩了
    assert out[0] is history[0]                    # 首条（目标）逐字保留
    assert out[-1] is history[-1]                  # 尾条逐字保留
    joined = "".join(p.content for m in out for p in m.parts if hasattr(p, "content"))
    assert "已完成" in joined                       # 结构化摘要插入


async def test_compaction_skips_on_summarize_failure():
    async def boom(text):
        raise RuntimeError("llm down")
    history = [_user("目标")] + [_assistant(f"x{i}") for i in range(50)]
    out = await cp.maybe_compact(history, compactor_mc=object(), running_mc=object(),
                                 window=10, summarize=boom)
    assert out is history                          # 压缩失败 -> 用原历史
