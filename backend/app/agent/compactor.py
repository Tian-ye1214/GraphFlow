"""上下文 Compactor：在 agent.run 前压缩过长历史。规则=工具输出只留结果 + 首尾保护 + 结构化摘要。
所有 Agent 角色统一复用。不依赖已废弃的 history_processor 构造参数。"""
from pydantic_ai.messages import ModelRequest, UserPromptPart

KEEP_TAIL = 6            # 末尾逐字保留的消息条数
COMPACT_RATIO = 0.75


def estimate_tokens(history: list) -> int:
    """字符启发式：所有 part.content 字符数 / 3（中英混合粗估）。"""
    chars = 0
    for m in history:
        for p in getattr(m, "parts", []):
            c = getattr(p, "content", None)
            if isinstance(c, str):
                chars += len(c)
    return chars // 3


def _strip_to_text(history: list) -> str:
    """把中间段消息拍平成纯文本（工具调用/返回只留其文本结果），喂给 compactor 总结。"""
    lines = []
    for m in history:
        for p in getattr(m, "parts", []):
            c = getattr(p, "content", None)
            if isinstance(c, str) and c.strip():
                lines.append(c.strip())
    return "\n".join(lines)


async def _default_summarize(compactor_mc, text: str, params: dict | None = None) -> str:
    from app.services import llm
    from app.agent.prompts import load_prompt
    from app.thinking import force_xhigh
    system = load_prompt("compactor_system.md")
    out, _usage = await llm.chat(compactor_mc, system, text, params=force_xhigh(params), retries=2)
    return out


async def maybe_compact(history: list, *, compactor_mc, running_mc, window: int | None = None,
                        summarize=None, emit=None, compactor_params: dict | None = None) -> list:
    """达 75% 窗口才压缩；否则原样返回。summarize 可注入（测试用），默认走 compactor LLM。
    压缩失败时返回原历史。"""
    if window is None:
        from app.agent.model_meta import model_window
        window = await model_window(running_mc.model_name)
    if estimate_tokens(history) < COMPACT_RATIO * window:
        return history
    if len(history) <= KEEP_TAIL + 1:
        return history
    head, middle, tail = history[:1], history[1:-KEEP_TAIL], history[-KEEP_TAIL:]
    if not middle:
        return history
    if summarize is None:
        async def summarize(text):
            return await _default_summarize(compactor_mc, text, params=compactor_params)
    try:
        if emit:
            await emit("compacting", {"before": len(history)})
        summary = await summarize(_strip_to_text(middle))
    except Exception:
        return history
    summary_msg = ModelRequest(parts=[UserPromptPart(content=f"[上下文摘要]\n{summary}")])
    return head + [summary_msg] + tail


def resolve_compactor_model(models: dict):
    """models: {role: ModelConfig 或 pydantic-ai Model}。返回 compactor 模型（默认复用 coordinator）。
    非 ModelConfig（测试用 Model 实例）一律返回 None，调用方据此跳过压缩。"""
    from app.models import ModelConfig
    mc = models.get("compactor") or models.get("coordinator")
    return mc if isinstance(mc, ModelConfig) else None
