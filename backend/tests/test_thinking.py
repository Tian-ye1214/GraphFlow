from app.thinking import thinking_extra_body


def test_default_enabled_high():
    assert thinking_extra_body({}) == {
        "thinking": {"type": "enabled"}, "reasoning_effort": "high"}


def test_disabled_returns_none():
    assert thinking_extra_body({"thinking_enabled": False}) is None


def test_custom_effort():
    assert thinking_extra_body({"reasoning_effort": "low"}) == {
        "thinking": {"type": "enabled"}, "reasoning_effort": "low"}


def test_enabled_explicit_xhigh():
    assert thinking_extra_body({"thinking_enabled": True, "reasoning_effort": "xhigh"}) == {
        "thinking": {"type": "enabled"}, "reasoning_effort": "xhigh"}
