"""_summarize_node http_fetch 分支：endpoint/param_keys 不泄漏值。"""
import json


def test_summarize_http_uses_endpoint_and_param_keys():
    from app.agent.node_info import _summarize_node
    from app.engine.graph import Node
    n = Node(id="f", type="http_fetch", config={
        "method": "GET", "endpoint": "http://api", "params": {"api_key": "S", "q": "x"},
        "extract": {"v": "data.v"}})
    s = _summarize_node(n)
    assert s.get("endpoint") == "http://api"
    assert set(s.get("param_keys", [])) == {"api_key", "q"}   # 只给键名，不给值
    assert "S" not in json.dumps(s, ensure_ascii=False)        # 不泄漏 api_key 值
