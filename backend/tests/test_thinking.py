from app.thinking import (agent_chat_settings, agent_responses_settings, chat_thinking_kwargs,
                          force_xhigh, reasoning_effort, thinking_enabled, with_thinking_defaults)


def test_with_thinking_defaults_fills_enabled_high():
    assert with_thinking_defaults({}) == {"thinking_enabled": True, "reasoning_effort": "high"}
    assert with_thinking_defaults(None) == {"thinking_enabled": True, "reasoning_effort": "high"}


def test_with_thinking_defaults_keeps_existing():
    out = with_thinking_defaults({"thinking_enabled": False, "reasoning_effort": "low", "temperature": 0.3})
    assert out == {"thinking_enabled": False, "reasoning_effort": "low", "temperature": 0.3}


def test_thinking_enabled_default_true():
    assert thinking_enabled({}) is True
    assert thinking_enabled({"thinking_enabled": False}) is False


def test_reasoning_effort_default_and_validation():
    assert reasoning_effort({}) == "high"
    assert reasoning_effort({"reasoning_effort": "low"}) == "low"
    assert reasoning_effort({"reasoning_effort": "xhigh"}) == "xhigh"
    assert reasoning_effort({"reasoning_effort": "不存在"}) == "high"   # 非法值回落默认


def test_reasoning_effort_azure_max_downgrades_to_xhigh():
    assert reasoning_effort({"reasoning_effort": "max"}, provider="azure") == "xhigh"
    assert reasoning_effort({"reasoning_effort": "max"}) == "max"        # openai 保留 max


def test_chat_thinking_kwargs_openai():
    assert chat_thinking_kwargs({}) == {
        "reasoning_effort": "high", "extra_body": {"thinking": {"type": "enabled"}}}
    assert chat_thinking_kwargs({"reasoning_effort": "medium"}) == {
        "reasoning_effort": "medium", "extra_body": {"thinking": {"type": "enabled"}}}


def test_chat_thinking_kwargs_azure_only_effort():
    assert chat_thinking_kwargs({}, provider="azure") == {"reasoning_effort": "high"}


def test_chat_thinking_kwargs_disabled_empty():
    assert chat_thinking_kwargs({"thinking_enabled": False}) == {}
    assert chat_thinking_kwargs({"thinking_enabled": False}, provider="azure") == {}


def test_agent_chat_settings():
    assert agent_chat_settings({}) == {
        "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}}
    assert agent_chat_settings({}, provider="azure") == {}            # azure 走 responses 设置
    assert agent_chat_settings({"thinking_enabled": False}) == {}


def test_force_xhigh_overrides():
    assert force_xhigh(None) == {"thinking_enabled": True, "reasoning_effort": "xhigh"}
    assert force_xhigh({"thinking_enabled": False, "reasoning_effort": "low", "temperature": 0.3}) == {
        "thinking_enabled": True, "reasoning_effort": "xhigh", "temperature": 0.3}


def test_agent_chat_settings_carries_effort():
    assert agent_chat_settings({"reasoning_effort": "xhigh"}) == {
        "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": "xhigh"}}


def test_agent_responses_settings():
    assert agent_responses_settings({}) == {"openai_reasoning_effort": "high"}
    assert agent_responses_settings({"reasoning_effort": "xhigh"}) == {"openai_reasoning_effort": "xhigh"}
    assert agent_responses_settings({"thinking_enabled": False}) == {}
