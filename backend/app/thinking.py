DEFAULT_REASONING_EFFORT = "high"
EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def with_thinking_defaults(params: dict | None) -> dict:
    out = dict(params or {})
    out.setdefault("thinking_enabled", True)
    out.setdefault("reasoning_effort", DEFAULT_REASONING_EFFORT)
    return out


def force_xhigh(params: dict | None) -> dict:
    """RedLotus + 节点助手专用：强制开启思考、力度 xhigh，覆盖任何传入值（保留其余键）。"""
    return {**(params or {}), "thinking_enabled": True, "reasoning_effort": "xhigh"}


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
