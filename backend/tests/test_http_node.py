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
