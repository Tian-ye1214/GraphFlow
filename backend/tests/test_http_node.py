import json

import pytest
from sqlalchemy import select

from app.engine import nodes, runner
from app.models import RunRow
from test_runner import get_run, make_run, run_it


def test_json_path_get_dotted_and_index():
    obj = {"data": {"temp": 25, "weather": [{"desc": "晴"}, {"desc": "雨"}]}}
    assert nodes.json_path_get(obj, "data.temp") == 25
    assert nodes.json_path_get(obj, "data.weather.0.desc") == "晴"
    assert nodes.json_path_get(obj, "data.weather.1.desc") == "雨"


def test_json_path_get_missing_returns_none():
    obj = {"data": {"temp": 25}}
    assert nodes.json_path_get(obj, "data.humidity") is None      # 缺键
    assert nodes.json_path_get(obj, "data.weather.0") is None      # 在非 list/dict 上下钻
    assert nodes.json_path_get(obj, "data.temp.x") is None         # 在 int 上下钻
    assert nodes.json_path_get(obj, "data.list.5") is None         # 索引越界（且 list 不存在）


def test_json_path_get_negative_and_root():
    assert nodes.json_path_get([10, 20, 30], "-1") == 30
    assert nodes.json_path_get({"a": 1}, "a") == 1


async def test_run_http_fetch_row_nan_response_neutralized(monkeypatch):
    """接口响应含非法 NaN token：归一为 None→空串，不让非法浮点落库致读行端点 500。"""
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        return 200, '{"v": NaN}'
    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    out, _ = await nodes.run_http_fetch_row({"url": "http://x", "extract": {"got": "v"}}, {"q": "a"})
    assert out == [{"q": "a", "got": ""}]


async def test_run_http_fetch_row_renders_and_extracts(monkeypatch):
    seen = {}

    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        seen.update(method=method, url=url, headers=headers)
        return 200, json.dumps({"data": {"temp": 25, "weather": [{"desc": "晴"}]}})

    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    cfg = {"method": "GET", "url": "http://api/{{city}}",
           "headers": {"X-City": "{{city}}"},
           "extract": {"temp": "data.temp", "desc": "data.weather.0.desc"}}
    out, usage = await nodes.run_http_fetch_row(cfg, {"city": "北京"})
    assert seen["url"] == "http://api/北京"          # url 模板渲染
    assert seen["headers"]["X-City"] == "北京"        # header 值模板渲染
    assert out == [{"city": "北京", "temp": 25, "desc": "晴"}]  # 保原类型，并入行
    assert usage == {}                                # 无 token


async def test_run_http_fetch_row_missing_field_becomes_empty(monkeypatch):
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        return 200, json.dumps({"data": {"temp": 25}})

    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    cfg = {"url": "http://api", "extract": {"temp": "data.temp", "missing": "data.nope"}}
    out, _ = await nodes.run_http_fetch_row(cfg, {"id": "1"})
    assert out == [{"id": "1", "temp": 25, "missing": ""}]  # 字段缺失→空串，不算失败


async def test_run_http_fetch_row_non_json_raises(monkeypatch):
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        return 200, "<html>not json</html>"

    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    with pytest.raises(ValueError):
        await nodes.run_http_fetch_row({"url": "http://api", "extract": {"x": "a"}}, {"id": "1"})


HTTP_GRAPH = {
    "nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": []}},
        {"id": "fetch", "type": "http_fetch",
         "config": {"method": "GET", "url": "http://api/{{q}}",
                    "extract": {"echo": "data.echo"}, "concurrency": 4, "retries": 1}},
        {"id": "out", "type": "output", "config": {}},
    ],
    "edges": [{"source": "in", "target": "fetch", "kind": "normal"},
              {"source": "fetch", "target": "out", "kind": "normal"}],
}


async def test_http_node_fetches_each_row(session_factory, monkeypatch):
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        q = url.rsplit("/", 1)[-1]
        return 200, json.dumps({"data": {"echo": f"E{q}"}})

    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    run_id = await make_run(session_factory, graph=HTTP_GRAPH)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "completed"
    out = await runner._node_outputs(session_factory, run_id, "out")
    assert {r["echo"] for r in out} == {"E问0", "E问1", "E问2"}
    assert all("q" in r and "echo" in r for r in out)
    assert json.loads(run.stats_json) == {"prompt_tokens": 0, "completion_tokens": 0}  # http 无 token


async def test_http_node_row_failure_isolated(session_factory, monkeypatch):
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        if "问1" in url:
            from app.services.http import HTTPFetchError
            raise HTTPFetchError("HTTP 500 GET " + url)
        return 200, json.dumps({"data": {"echo": "ok"}})

    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    run_id = await make_run(session_factory, graph=HTTP_GRAPH)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "completed"                       # 单行失败不挂整 run
    out = await runner._node_outputs(session_factory, run_id, "out")
    assert len(out) == 2
    async with session_factory() as s:
        rec = (await s.execute(select(RunRow).where(
            RunRow.run_id == run_id, RunRow.node_id == "fetch", RunRow.status == "failed"))).scalar_one()
    assert rec.row_idx == 1


async def test_http_node_resume_skips_done(session_factory, monkeypatch):
    calls = []

    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        calls.append(url)
        return 200, json.dumps({"data": {"echo": "x"}})

    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    run_id = await make_run(session_factory, graph=HTTP_GRAPH)
    async with session_factory() as s:   # 预置 idx0 已完成
        s.add(RunRow(run_id=run_id, node_id="fetch", row_idx=0, status="done",
                     data_json=json.dumps([{"q": "问0", "echo": "旧"}], ensure_ascii=False)))
        await s.commit()
    await run_it(session_factory, run_id)
    assert len(calls) == 2                                 # 只跑未完成的两行


async def test_http_fetch_merges_params_with_apikey(monkeypatch):
    seen = {}
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        seen.update(url=url, headers=headers, body=body)
        return 200, json.dumps({"v": 1})
    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    cfg = {"method": "GET", "endpoint": "http://api/{{city}}",
           "params": {"api_key": "SECRET", "q": "{{city}}"}, "extract": {"v": "v"}}
    out, _ = await nodes.run_http_fetch_row(cfg, {"city": "bj"})
    assert seen["url"].startswith("http://api/bj?")          # endpoint 渲染
    assert "api_key=SECRET" in seen["url"] and "q=bj" in seen["url"]  # params 合并(含 api_key)+模板渲染
    assert out == [{"city": "bj", "v": 1}]


async def test_http_fetch_body_format_sets_content_type(monkeypatch):
    seen = {}
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        seen.update(headers=headers, body=body)
        return 200, "{}"
    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    cfg = {"method": "POST", "endpoint": "http://api", "body": '{"a":1}', "body_format": "json", "extract": {}}
    await nodes.run_http_fetch_row(cfg, {})
    assert seen["headers"]["Content-Type"] == "application/json"
    assert seen["body"] == '{"a":1}'


async def test_http_fetch_user_content_type_wins(monkeypatch):
    seen = {}
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        seen.update(headers=headers)
        return 200, "{}"
    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    cfg = {"method": "POST", "endpoint": "http://api", "body": "x", "body_format": "json",
           "headers": {"Content-Type": "text/plain"}, "extract": {}}
    await nodes.run_http_fetch_row(cfg, {})
    assert seen["headers"]["Content-Type"] == "text/plain"   # 用户显式设置不被覆盖


async def test_http_fetch_legacy_url_still_works(monkeypatch):
    seen = {}
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        seen.update(url=url)
        return 200, json.dumps({"v": 9})
    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    out, _ = await nodes.run_http_fetch_row({"url": "http://api/{{q}}", "extract": {"v": "v"}}, {"q": "z"})
    assert seen["url"] == "http://api/z"                     # 无 params 时 endpoint=url 原样
    assert out == [{"q": "z", "v": 9}]


@pytest.mark.parametrize("bad_cfg, kw", [
    ({"endpoint": {"bad": 1}}, "endpoint"),
    ({"url": {"bad": 1}}, "endpoint"),                 # 旧 url 走兼容，仍按 endpoint 报
    ({"endpoint": "http://x", "params": ["a"]}, "params"),
    ({"endpoint": "http://x", "body": [1]}, "body"),
    ({"endpoint": "http://x", "body_format": "xml"}, "body_format"),
    ({"endpoint": "http://x", "headers": ["a"]}, "headers"),
    ({"endpoint": "http://x", "extract": ["a"]}, "extract"),
])
async def test_http_node_dirty_config_fails_run_named(session_factory, monkeypatch, bad_cfg, kw):
    """脏草稿 config(非字符串 endpoint/body、非 dict params/headers/extract、非法 body_format)是节点配置错误，
    应整 run failed 并点名节点/键(对照 _run_llm_node 的 fanout_n 预校验)，而非逐行裸 Python 错误且 run 误报 completed。"""
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        return 200, "{}"
    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    graph = json.loads(json.dumps(HTTP_GRAPH))
    for n in graph["nodes"]:
        if n["type"] == "http_fetch":
            cfg = {k: v for k, v in n["config"].items() if k != "url"}  # 去掉基础 url，避免覆盖被测键
            n["config"] = {**cfg, **bad_cfg}
    run_id = await make_run(session_factory, graph=graph)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "failed"
    assert "fetch" in (run.error or "") and kw in (run.error or "")


async def test_http_poll_until_status_done(monkeypatch):
    """配了 poll_status_path：反复发同一请求，直到状态字段达 poll_until 才提取。"""
    calls = {"n": 0}

    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        calls["n"] += 1
        if calls["n"] < 3:
            return 200, json.dumps({"status": "pending"})
        return 200, json.dumps({"status": "completed", "result": 42})

    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    cfg = {"url": "http://job", "poll_status_path": "status", "poll_until": "completed",
           "poll_interval": 0, "poll_max_attempts": 10, "extract": {"r": "result"}}
    out, _ = await nodes.run_http_fetch_row(cfg, {"id": "1"})
    assert calls["n"] == 3
    assert out == [{"id": "1", "r": 42}]


async def test_http_poll_exhausts_attempts_raises(monkeypatch):
    """状态恒不就绪：发满 poll_max_attempts 次后抛 ValueError（含「轮询」），由 runner 记为行/run 失败。"""
    calls = {"n": 0}

    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        calls["n"] += 1
        return 200, json.dumps({"status": "pending"})

    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    cfg = {"url": "http://job", "poll_status_path": "status", "poll_until": "completed",
           "poll_interval": 0, "poll_max_attempts": 3, "extract": {"r": "result"}}
    with pytest.raises(ValueError, match="轮询"):
        await nodes.run_http_fetch_row(cfg, {"id": "1"})
    assert calls["n"] == 3


async def test_http_poll_non_json_treated_as_not_ready(monkeypatch):
    """轮询期间非 JSON 响应（如 202 空体）视为「未就绪」继续轮询，而非立刻失败。"""
    calls = {"n": 0}

    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        calls["n"] += 1
        if calls["n"] < 2:
            return 202, "Accepted"
        return 200, json.dumps({"status": "completed", "v": 1})

    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    cfg = {"url": "http://job", "poll_status_path": "status", "poll_until": "completed",
           "poll_interval": 0, "poll_max_attempts": 5, "extract": {"v": "v"}}
    out, _ = await nodes.run_http_fetch_row(cfg, {"id": "1"})
    assert calls["n"] == 2
    assert out == [{"id": "1", "v": 1}]
