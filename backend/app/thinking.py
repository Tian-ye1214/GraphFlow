DEFAULT_REASONING_EFFORT = "high"
EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def with_thinking_defaults(params: dict | None) -> dict:
    out = dict(params or {})
    out.setdefault("thinking_enabled", True)
    out.setdefault("reasoning_effort", DEFAULT_REASONING_EFFORT)
    return out


def force_max_thinking(params: dict | None) -> dict:
    """RedLotus 相关 Agent（协调/构建各角色 + 节点助手 + codegen + compactor）专用：写死开启思考、
    力度 max、max_tokens 65536，覆盖任何传入值（保留其余键）。Agent 路径思考不暴露给用户调整
    （与各节点「参数默认值可被用户覆盖」相区分）。注：azure 下 reasoning_effort 会在 reasoning_effort() 内回落 xhigh。"""
    return {**(params or {}), "thinking_enabled": True, "reasoning_effort": "max", "max_tokens": 65536}


def thinking_enabled(params: dict | None) -> bool:
    return bool(with_thinking_defaults(params).get("thinking_enabled"))


def reasoning_effort(params: dict | None, *, provider: str = "openai") -> str:
    effort = str(with_thinking_defaults(params).get("reasoning_effort") or DEFAULT_REASONING_EFFORT)
    if effort not in EFFORTS:
        effort = DEFAULT_REASONING_EFFORT
    if provider == "azure" and effort == "max":
        return "xhigh"
    return effort


def chat_thinking_kwargs(params: dict | None, *, provider: str = "openai") -> dict:
    if not thinking_enabled(params):
        return {}
    effort = reasoning_effort(params, provider=provider)
    if provider == "azure":
        return {"reasoning_effort": effort}
    return {
        "reasoning_effort": effort,
        "extra_body": {"thinking": {"type": "enabled"}},
    }


def agent_chat_settings(params: dict | None, *, provider: str = "openai") -> dict:
    if not thinking_enabled(params):
        return {}
    if provider == "azure":
        return {}
    return {"extra_body": {"thinking": {"type": "enabled"},
                           "reasoning_effort": reasoning_effort(params, provider=provider)}}


def agent_responses_settings(params: dict | None, *, provider: str = "openai") -> dict:
    if not thinking_enabled(params):
        return {}
    return {"openai_reasoning_effort": reasoning_effort(params, provider=provider)}
