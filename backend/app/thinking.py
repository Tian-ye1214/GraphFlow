"""思考模式配置：把「是否开启思考 + 力度」翻译成 OpenAI 兼容请求的 extra_body。
默认开启、力度 high；关闭则整段不发（返回 None）。节点路径与 Agent 路径共用。"""


def thinking_extra_body(params: dict) -> dict | None:
    if not params.get("thinking_enabled", True):
        return None
    return {"thinking": {"type": "enabled"},
            "reasoning_effort": params.get("reasoning_effort", "high")}
