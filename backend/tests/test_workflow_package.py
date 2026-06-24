import io
import json
import zipfile

import pytest
from conftest import wait_ready

import app.services.workflow_package as wp
from app.config import settings
from app.engine.graph import parse_graph
from app.models import (Dataset, DatasetRow, ModelConfig, Prompt, PromptVersion, User, Workflow)
from app.services.dataset_store import iter_jsonl_lines


# ---------------- Task 1: 纯函数 ----------------

def test_collect_refs_gathers_all_kinds_and_skips_dirty():
    graph = parse_graph({"nodes": [
        {"id": "in", "type": "input", "config": {"dataset_ids": [3, 5, "x", True]}},
        {"id": "g", "type": "llm_synth", "config": {"model_config_id": 1, "system_prompt_ref": 7}},
        {"id": "q", "type": "qc", "config": {"judge_model_ids": [1, 2], "user_prompt_ref": 7}},
        {"id": "bad", "type": "llm_synth", "config": "not-a-dict"},
    ], "edges": []})
    ds, models, prompts = wp.collect_refs(graph)
    assert ds == {3, 5}            # "x"/True(bool) 被跳过
    assert models == {1, 2}
    assert prompts == {7}


def test_redact_secrets_headers_only_sensitive_literal_values():
    g = {"nodes": [
        {"id": "h", "type": "http_fetch", "config": {"headers": {
            "Authorization": "Bearer sk-secret", "X-Api-Key": "abc", "X-Signature": "sig123",
            "Content-Type": "application/json", "X-Token": "{{tok}}"}}},
        {"id": "x", "type": "input", "config": {}},
    ], "edges": []}
    red = wp.redact_secrets(g)
    h = g["nodes"][0]["config"]["headers"]
    assert h["Authorization"] == wp.REDACTED
    assert h["X-Api-Key"] == wp.REDACTED
    assert h["X-Signature"] == wp.REDACTED            # 签名类自定义鉴权头也脱敏（扩展名单）
    assert h["Content-Type"] == "application/json"    # 非敏感头保留
    assert h["X-Token"] == "{{tok}}"                  # 模板值放行
    assert {(r["node_id"], r["field"]) for r in red} == {
        ("h", "Authorization"), ("h", "X-Api-Key"), ("h", "X-Signature")}


def test_redact_secrets_url_and_body():
    g = {"nodes": [
        {"id": "u", "type": "http_fetch", "config": {
            "url": "https://user:pass@api.x.com/v1?api_key=SECRET&q={{q}}&page=1",
            "body": '{"token": "BODYSECRET", "q": "{{q}}", "n": 3}'}},
        {"id": "t", "type": "http_fetch", "config": {"url": "https://api.x.com/{{q}}"}},
    ], "edges": []}
    red = wp.redact_secrets(g)
    cfg = g["nodes"][0]["config"]
    assert "pass" not in cfg["url"] and wp.REDACTED in cfg["url"]   # userinfo 打码
    assert "api_key=SECRET" not in cfg["url"] and "page=1" in cfg["url"]  # 敏感查询参数打码、普通参数保留
    assert "{{q}}" in cfg["url"]                                    # 模板查询参数保留
    body = json.loads(cfg["body"])
    assert body["token"] == wp.REDACTED and body["q"] == "{{q}}" and body["n"] == 3
    assert g["nodes"][1]["config"]["url"] == "https://api.x.com/{{q}}"   # 纯模板 url 放行
    fields = {r["field"] for r in red}
    assert "url.userinfo" in fields and "url.api_key" in fields and "body.token" in fields


# ---------------- Task 2: 导出 ----------------

async def _seed_workflow(session_factory):
    """建 1 用户 + 1 模型(带 key) + 1 提示词(2 版) + 1 数据集(2 行) + 1 引用它们的工作流。
    返回 (uid, wf_id)。"""
    from app.crypto import encrypt
    from app.models import (Dataset, ModelConfig, Prompt, User,
                            Workflow)
    async with session_factory() as s:
        u = User(username="exp"); s.add(u); await s.flush()
        m = ModelConfig(user_id=u.id, name="m1", model_name="deepseek", base_url="http://x",
                        api_key_enc=encrypt("SECRET-KEY"), default_params_json='{"temperature": 0}')
        p = Prompt(user_id=u.id, name="p1", description="d"); s.add_all([m, p]); await s.flush()
        s.add(PromptVersion(prompt_id=p.id, version=1, body="旧", variables_json="[]"))
        s.add(PromptVersion(prompt_id=p.id, version=2, body="新正文", variables_json='["q"]'))
        d = Dataset(user_id=u.id, name="ds1", row_count=2, columns_json='["q"]'); s.add(d); await s.flush()
        s.add(DatasetRow(dataset_id=d.id, idx=0, data_json='{"q": "007"}'))
        s.add(DatasetRow(dataset_id=d.id, idx=1, data_json='{"q": "你好"}'))
        graph = {"nodes": [
            {"id": "in", "type": "input", "config": {"dataset_ids": [d.id]}},
            {"id": "g", "type": "llm_synth",
             "config": {"model_config_id": m.id, "system_prompt_ref": p.id}},
            {"id": "h", "type": "http_fetch",
             "config": {"headers": {"Authorization": "Bearer sk-x", "Accept": "*/*"}}},
        ], "edges": [{"source": "in", "target": "g", "kind": "normal"}]}
        wf = Workflow(user_id=u.id, name="链路A", graph_json=json.dumps(graph, ensure_ascii=False))
        s.add(wf); await s.commit()
        return u.id, wf.id


async def _read_dataset_rows(session, dataset):
    return [json.loads(line) async for line in iter_jsonl_lines(session, dataset, settings.data_dir)]


async def test_export_package_self_contained_no_key_redacted(session_factory, tmp_path):
    _uid, wf_id = await _seed_workflow(session_factory)
    dest = tmp_path / "out.gfpkg"
    async with session_factory() as s:
        wf = await s.get(Workflow, wf_id)
        await wp.export_package(s, wf, str(dest))
    with zipfile.ZipFile(dest) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        ds_lines = zf.read("datasets/%d.jsonl" % manifest["datasets"][0]["id"]).decode().splitlines()
    assert manifest["kind"] == wp.PACKAGE_KIND and manifest["schema_version"] == 1
    # 模型不含 key
    assert manifest["models"][0]["name"] == "m1"
    assert "api_key" not in manifest["models"][0] and "api_key_enc" not in manifest["models"][0]
    # 提示词取最新版正文
    assert manifest["prompts"][0]["body"] == "新正文" and manifest["prompts"][0]["variables"] == ["q"]
    # http 头脱敏
    httpn = next(n for n in manifest["workflow"]["graph"]["nodes"] if n["id"] == "h")
    assert httpn["config"]["headers"]["Authorization"] == wp.REDACTED
    assert httpn["config"]["headers"]["Accept"] == "*/*"
    assert manifest["redactions"] == [{"node_id": "h", "field": "Authorization"}]
    # 数据集行类型保真（"007" 仍是字符串）
    assert [json.loads(line) for line in ds_lines] == [{"q": "007"}, {"q": "你好"}]
    # 库内 graph 未被脱敏污染（redact 只改导出副本）
    async with session_factory() as s:
        wf2 = await s.get(Workflow, wf_id)
        h_in_db = next(n for n in json.loads(wf2.graph_json)["nodes"] if n["id"] == "h")
        assert h_in_db["config"]["headers"]["Authorization"] == "Bearer sk-x"


# ---------------- Task 3: 导入硬化 + manifest 校验 ----------------

def _pkg_bytes(manifest, extra_files=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        for name, data in (extra_files or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def _good_manifest():
    return {"kind": wp.PACKAGE_KIND, "schema_version": 1,
            "workflow": {"name": "w", "graph": {"nodes": [], "edges": []}},
            "models": [], "prompts": [], "datasets": [], "redactions": []}


def test_open_safe_zip_rejects_traversal(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", "x")
    path = tmp_path / "z.gfpkg"; path.write_bytes(buf.getvalue())
    with pytest.raises(wp.PackageError):
        wp._open_safe_zip(str(path))


def test_open_safe_zip_rejects_non_zip(tmp_path):
    path = tmp_path / "z.gfpkg"; path.write_bytes(b"not a zip")
    with pytest.raises(wp.PackageError):
        wp._open_safe_zip(str(path))


def test_parse_manifest_rejects_bad(tmp_path):
    def mp(b):
        path = tmp_path / "z.gfpkg"; path.write_bytes(b)
        with wp._open_safe_zip(str(path)) as zf:
            return wp._parse_manifest(zf)
    with pytest.raises(wp.PackageError):   # 非本系统包
        mp(_pkg_bytes({**_good_manifest(), "kind": "other"}))
    with pytest.raises(wp.PackageError):   # 版本过新
        mp(_pkg_bytes({**_good_manifest(), "schema_version": 999}))
    with pytest.raises(wp.PackageError):   # 结构非法
        mp(_pkg_bytes({**_good_manifest(), "workflow": "nope"}))
    with pytest.raises(wp.PackageError):   # 资源目录非数组
        mp(_pkg_bytes({**_good_manifest(), "models": {}}))
    assert mp(_pkg_bytes(_good_manifest()))["kind"] == wp.PACKAGE_KIND   # 合法放行


def test_parse_manifest_rejects_corrupt_json(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", "{ not json")
    path = tmp_path / "z.gfpkg"; path.write_bytes(buf.getvalue())
    with wp._open_safe_zip(str(path)) as zf:
        with pytest.raises(wp.PackageError):
            wp._parse_manifest(zf)


# ---------------- Task 4: 导入 ----------------

async def test_import_roundtrip_cross_tenant_reuse_and_create(session_factory, tmp_path):
    from sqlalchemy import select
    from app.crypto import decrypt, encrypt
    # 导出账号 A 的链路
    _uid_a, wf_id = await _seed_workflow(session_factory)
    dest = tmp_path / "out.gfpkg"
    async with session_factory() as s:
        await wp.export_package(s, await s.get(Workflow, wf_id), str(dest))
    # 账号 B 先有一个同名模型 "m1"（带 key）→ 导入应复用它；提示词/数据集 B 没有 → 新建
    async with session_factory() as s:
        b = User(username="importer"); s.add(b); await s.flush()
        s.add(ModelConfig(user_id=b.id, name="m1", model_name="b-model", base_url="http://b",
                          api_key_enc=encrypt("B-KEY"), default_params_json="{}"))
        await s.commit(); uid_b = b.id
    async with session_factory() as s:
        wf_out, report = await wp.import_package(s, str(dest), uid_b)
    async with session_factory() as s:
        new = await s.get(Workflow, wf_out["id"])
        assert new.user_id == uid_b and new.name == "链路A(导入)"
        g = json.loads(new.graph_json)
        gen = next(n for n in g["nodes"] if n["id"] == "g")
        bm = (await s.execute(select(ModelConfig).where(
            ModelConfig.user_id == uid_b, ModelConfig.name == "m1"))).scalars().first()
        assert gen["config"]["model_config_id"] == bm.id        # 复用 B 既有同名（非包内 A 的 id）
        assert decrypt(bm.api_key_enc) == "B-KEY"               # 复用模型保留自己的 key，未被覆盖
        pid = gen["config"]["system_prompt_ref"]
        np = await s.get(Prompt, pid); assert np.user_id == uid_b   # 提示词新建并重连
        inn = next(n for n in g["nodes"] if n["id"] == "in")
        did = inn["config"]["dataset_ids"][0]
        nd = await s.get(Dataset, did); assert nd.user_id == uid_b and nd.row_count == 2
        assert await _read_dataset_rows(s, nd) == [{"q": "007"}, {"q": "你好"}]   # 行保真
    assert report["models_reused"] and report["datasets_created"] and report["prompts_created"]
    assert report["secrets_need_refill"] == [{"node_id": "h", "field": "Authorization"}]


async def test_import_ignores_embedded_user_id(session_factory, tmp_path):
    """包里就算夹带 user_id 也无视：一切落到导入者账号。"""
    from sqlalchemy import select
    async with session_factory() as s:
        u = User(username="victim"); s.add(u); await s.flush(); uid = u.id
    manifest = {**_good_manifest(),
                "workflow": {"name": "x", "graph": {"nodes": [], "edges": []}},
                "models": [{"id": 1, "name": "evil", "user_id": 999}]}
    path = tmp_path / "z.gfpkg"; path.write_bytes(_pkg_bytes(manifest))
    async with session_factory() as s:
        wf_out, _report = await wp.import_package(s, str(path), uid)
    async with session_factory() as s:
        wf = await s.get(Workflow, wf_out["id"]); assert wf.user_id == uid
        m = (await s.execute(select(ModelConfig).where(ModelConfig.name == "evil"))).scalars().first()
        assert m.user_id == uid          # 落到导入者，非包内 user_id 999
        assert m.api_key_enc == ""       # 新建模型空 key（api_key_set=bool(enc)=False，不误报有 key）


async def test_import_atomic_rollback_on_bad_graph(session_factory, tmp_path):
    """图有环（validate 失败）但带数据集 → 整体回滚、不留孤儿数据集。"""
    from sqlalchemy import select
    async with session_factory() as s:
        u = User(username="atom"); s.add(u); await s.flush(); uid = u.id
    manifest = {"kind": wp.PACKAGE_KIND, "schema_version": 1,
                "workflow": {"name": "环", "graph": {"nodes": [
                    {"id": "a", "type": "auto_process", "config": {}},
                    {"id": "b", "type": "auto_process", "config": {}}],
                    "edges": [{"source": "a", "target": "b", "kind": "normal"},
                              {"source": "b", "target": "a", "kind": "normal"}]}},
                "models": [], "prompts": [],
                "datasets": [{"id": 9, "name": "孤儿集", "columns": ["q"], "row_count": 1,
                              "file": "datasets/9.jsonl"}],
                "redactions": []}
    path = tmp_path / "z.gfpkg"
    path.write_bytes(_pkg_bytes(manifest, {"datasets/9.jsonl": '{"q": "1"}\n'}))
    async with session_factory() as s:
        with pytest.raises(wp.PackageError):
            await wp.import_package(s, str(path), uid)
        await s.rollback()
    async with session_factory() as s:
        left = (await s.execute(select(Dataset).where(Dataset.user_id == uid))).scalars().all()
        assert left == []      # 回滚后无孤儿数据集


async def test_import_missing_ref_degrades_to_draft(session_factory, tmp_path):
    """包内 graph 引用了 manifest 资源目录里没有的模型 → 重连失败置空 + draft_unresolved 点名。"""
    async with session_factory() as s:
        u = User(username="draft"); s.add(u); await s.flush(); uid = u.id
    manifest = {**_good_manifest(),
                "workflow": {"name": "缺引用", "graph": {"nodes": [
                    {"id": "g", "type": "llm_synth", "config": {"model_config_id": 42}}], "edges": []}}}
    path = tmp_path / "z.gfpkg"; path.write_bytes(_pkg_bytes(manifest))
    async with session_factory() as s:
        wf_out, report = await wp.import_package(s, str(path), uid)
        g = json.loads((await s.get(Workflow, wf_out["id"])).graph_json)
    assert g["nodes"][0]["config"]["model_config_id"] is None
    assert report["draft_unresolved"] == [{"node_id": "g", "kind": "模型", "old_id": 42}]


async def test_import_workflow_name_suffix_increments(session_factory, tmp_path):
    async with session_factory() as s:
        u = User(username="dup"); s.add(u); await s.flush(); uid = u.id
        s.add(Workflow(user_id=uid, name="w(导入)"))
        await s.commit()
    path = tmp_path / "z.gfpkg"; path.write_bytes(_pkg_bytes(_good_manifest()))
    async with session_factory() as s:
        wf_out, _ = await wp.import_package(s, str(path), uid)
    assert wf_out["name"] == "w(导入 2)"   # "w(导入)" 已占用 → 递增


# ---------------- Task 5: REST 端点 ----------------

async def test_export_import_endpoints_roundtrip(auth_client):
    up = await auth_client.post("/api/datasets/upload",
        files={"files": ("d.jsonl", b'{"q": "007"}\n{"q": "x"}\n', "application/x-ndjson")})
    did = (await wait_ready(auth_client, up.json()[0]["id"]))["id"]
    wf = (await auth_client.post("/api/workflows", json={"name": "导链"})).json()
    graph = {"nodes": [{"id": "in", "type": "input", "config": {"dataset_ids": [did]}}], "edges": []}
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})
    r = await auth_client.get(f"/api/workflows/{wf['id']}/export")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    pkg = r.content
    await auth_client.post("/api/auth/login", json={"username": "importer2"})
    r2 = await auth_client.post("/api/workflows/import",
        files={"file": ("x.gfpkg", pkg, "application/zip")})
    assert r2.status_code == 200
    body = r2.json()
    assert body["workflow"]["name"] == "导链(导入)"
    assert body["report"]["datasets_created"]
    got = (await auth_client.get(f"/api/workflows/{body['workflow']['id']}")).json()
    new_did = got["graph"]["nodes"][0]["config"]["dataset_ids"][0]
    rows = (await auth_client.get(f"/api/datasets/{new_did}/rows")).json()
    assert rows["rows"][0] == {"q": "007"}


async def test_import_endpoint_rejects_garbage(auth_client):
    r = await auth_client.post("/api/workflows/import",
        files={"file": ("x.gfpkg", b"not a zip", "application/zip")})
    assert r.status_code == 422


async def test_export_foreign_workflow_404(auth_client):
    wf = (await auth_client.post("/api/workflows", json={"name": "私有"})).json()
    await auth_client.post("/api/auth/login", json={"username": "intruder"})
    assert (await auth_client.get(f"/api/workflows/{wf['id']}/export")).status_code == 404


# ---------------- Task 6: CLI ----------------

def test_cli_workflow_register_has_export_import():
    import argparse
    from app.cli.commands import workflow as wfcmd
    parser = argparse.ArgumentParser()
    wfcmd.register(parser.add_subparsers(dest="cmd"))
    assert parser.parse_args(["wf", "export", "x"]).func is wfcmd.cmd_wf_export
    assert parser.parse_args(["wf", "import", "f.gfpkg"]).func is wfcmd.cmd_wf_import
    with pytest.raises(SystemExit):
        parser.parse_args(["wf", "dump"])


# ---------------- Task 8: 对抗 review 修复回归 ----------------

async def test_export_redacts_model_default_params_secret(session_factory, tmp_path):
    """模型 default_params 里夹带的凭据（extra_body 内鉴权头）导出时也脱敏。"""
    async with session_factory() as s:
        u = User(username="dp"); s.add(u); await s.flush()
        m = ModelConfig(user_id=u.id, name="mdp", base_url="http://x", api_key_enc="enc",
                        default_params_json=json.dumps({"temperature": 0,
                            "extra_body": {"headers": {"Authorization": "Bearer SECRET"}}}))
        s.add(m); await s.flush()
        graph = {"nodes": [{"id": "g", "type": "llm_synth", "config": {"model_config_id": m.id}}], "edges": []}
        wf = Workflow(user_id=u.id, name="w", graph_json=json.dumps(graph)); s.add(wf); await s.commit()
        wf_id = wf.id
    dest = tmp_path / "o.gfpkg"
    async with session_factory() as s:
        await wp.export_package(s, await s.get(Workflow, wf_id), str(dest))
    with zipfile.ZipFile(dest) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    dp = manifest["models"][0]["default_params"]
    assert dp["temperature"] == 0
    assert dp["extra_body"]["headers"]["Authorization"] == wp.REDACTED


def _write_pkg_with_dataset_bytes(tmp_path, raw_jsonl: bytes):
    manifest = {**_good_manifest(),
                "datasets": [{"id": 1, "name": "d", "columns": ["q"], "row_count": 1, "file": "datasets/1.jsonl"}]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        zf.writestr("datasets/1.jsonl", raw_jsonl)
    path = tmp_path / "z.gfpkg"; path.write_bytes(buf.getvalue())
    return path


async def test_import_non_utf8_dataset_is_422_not_500(session_factory, tmp_path):
    path = _write_pkg_with_dataset_bytes(tmp_path, '{"q": "甲"}\n'.encode("gbk"))   # GBK，非 UTF-8
    async with session_factory() as s:
        u = User(username="enc1"); s.add(u); await s.flush(); uid = u.id
    async with session_factory() as s:
        with pytest.raises(wp.PackageError):
            await wp.import_package(s, str(path), uid)


async def test_import_bom_dataset_ok(session_factory, tmp_path):
    path = _write_pkg_with_dataset_bytes(tmp_path, '﻿{"q": "甲"}\n'.encode("utf-8"))  # 带 BOM
    async with session_factory() as s:
        u = User(username="enc2"); s.add(u); await s.flush(); uid = u.id
    async with session_factory() as s:
        await wp.import_package(s, str(path), uid)
    async with session_factory() as s:
        from sqlalchemy import select
        d = (await s.execute(select(Dataset).where(Dataset.user_id == uid))).scalars().first()
        rows = await _read_dataset_rows(s, d)
    assert rows == [{"q": "甲"}]   # BOM 被容错剥除


async def test_import_dirty_graph_shape_is_422(session_factory, tmp_path):
    async with session_factory() as s:
        u = User(username="shape"); s.add(u); await s.flush(); uid = u.id
    for bad_graph in ({"nodes": 123, "edges": []},
                      {"nodes": ["x"], "edges": []},
                      {"nodes": [], "edges": [123]}):
        manifest = {**_good_manifest(), "workflow": {"name": "w", "graph": bad_graph}}
        path = tmp_path / "z.gfpkg"; path.write_bytes(_pkg_bytes(manifest))
        async with session_factory() as s:
            with pytest.raises(wp.PackageError):
                await wp.import_package(s, str(path), uid)
            await s.rollback()


def test_parse_manifest_schema_version_strict(tmp_path):
    def mp(ver):
        path = tmp_path / "z.gfpkg"
        path.write_bytes(_pkg_bytes({**_good_manifest(), "schema_version": ver}))
        with wp._open_safe_zip(str(path)) as zf:
            return wp._parse_manifest(zf)
    for bad in (True, 1.0, "1", [1]):
        with pytest.raises(wp.PackageError):
            mp(bad)
    assert mp(1)["schema_version"] == 1


def test_open_safe_zip_rejects_unsupported_compression(tmp_path, monkeypatch):
    # stdlib 写不出非法压缩方法的 zip，故构造合法 zip 后改写中央目录里 ZipInfo.compress_type 校验逻辑路径：
    # 直接验证 _open_safe_zip 对声明非常规 compress_type 的条目报 PackageError。
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", "{}")
    path = tmp_path / "z.gfpkg"; path.write_bytes(buf.getvalue())
    real_infolist = zipfile.ZipFile.infolist

    def fake_infolist(self):
        infos = real_infolist(self)
        for i in infos:
            i.compress_type = 6      # imploded：_ALLOWED_COMPRESS 之外
        return infos
    monkeypatch.setattr(zipfile.ZipFile, "infolist", fake_infolist)
    with pytest.raises(wp.PackageError):
        wp._open_safe_zip(str(path))


async def test_export_draft_graph_endpoint_422(auth_client):
    wf = (await auth_client.post("/api/workflows", json={"name": "草稿"})).json()
    bad = {"nodes": [{"id": "x"}], "edges": []}   # 节点缺 type → parse_graph 抛 GraphError
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": bad})
    assert (await auth_client.get(f"/api/workflows/{wf['id']}/export")).status_code == 422


async def test_export_draft_graph_shape_endpoint_422(auth_client):
    """草稿图结构畸形（nodes 非 list）→ parse_graph 抛 TypeError，导出端须 422 不 500（对齐 columns）。"""
    wf = (await auth_client.post("/api/workflows", json={"name": "脏图"})).json()
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": {"nodes": "x", "edges": []}})
    assert (await auth_client.get(f"/api/workflows/{wf['id']}/export")).status_code == 422


# ---------------- Task 8: 第二轮复审修复 ----------------

def test_redact_secrets_list_nested_and_nonstring():
    g = {"nodes": [
        {"id": "a", "type": "http_fetch", "config": {
            "body": '[{"api_key": "sk-TOP-ARRAY"}, {"prompt": "hi"}]'}},          # 顶层数组 body
        {"id": "b", "type": "http_fetch", "config": {
            "body": '{"items": [{"authorization": "Bearer sk-NESTED"}]}'}},        # list 内 dict
        {"id": "c", "type": "http_fetch", "config": {
            "headers": {"x-api-key": 9876543210, "authorization": ["Bearer", "sk-LIST"]}}},  # 非字符串头值
    ], "edges": []}
    red = wp.redact_secrets(g)
    assert json.loads(g["nodes"][0]["config"]["body"])[0]["api_key"] == wp.REDACTED
    assert json.loads(g["nodes"][1]["config"]["body"])["items"][0]["authorization"] == wp.REDACTED
    assert g["nodes"][2]["config"]["headers"]["x-api-key"] == wp.REDACTED        # int 头值打码
    assert g["nodes"][2]["config"]["headers"]["authorization"] == wp.REDACTED    # list 头值打码
    assert len(red) == 4


def test_redact_secrets_url_fragment():
    g = {"nodes": [{"id": "f", "type": "http_fetch",
                    "config": {"url": "https://api.x.com/c#api_key=sk-FRAG&v=1"}}], "edges": []}
    red = wp.redact_secrets(g)
    url = g["nodes"][0]["config"]["url"]
    assert "sk-FRAG" not in url and wp.REDACTED in url and "v=1" in url
    assert any(r["field"] == "fragment.api_key" for r in red)


async def test_export_redacts_list_nested_default_params(session_factory, tmp_path):
    async with session_factory() as s:
        u = User(username="dpl"); s.add(u); await s.flush()
        m = ModelConfig(user_id=u.id, name="ml", base_url="http://x", api_key_enc="enc",
                        default_params_json=json.dumps({"extra_body": {"providers": [
                            {"api_key": "sk-DP-LIST"}]}}))
        s.add(m); await s.flush()
        graph = {"nodes": [{"id": "g", "type": "llm_synth", "config": {"model_config_id": m.id}}], "edges": []}
        wf = Workflow(user_id=u.id, name="w", graph_json=json.dumps(graph)); s.add(wf); await s.commit()
        wf_id = wf.id
    dest = tmp_path / "o.gfpkg"
    async with session_factory() as s:
        await wp.export_package(s, await s.get(Workflow, wf_id), str(dest))
    with zipfile.ZipFile(dest) as zf:
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["models"][0]["default_params"]["extra_body"]["providers"][0]["api_key"] == wp.REDACTED


async def test_import_resource_id_non_int_is_422(session_factory, tmp_path):
    async with session_factory() as s:
        u = User(username="badid"); s.add(u); await s.flush(); uid = u.id
    for bad in ([], {}, "5", 5.0, None):
        manifest = {**_good_manifest(), "models": [{"id": bad, "name": "x"}]}
        path = tmp_path / "z.gfpkg"; path.write_bytes(_pkg_bytes(manifest))
        async with session_factory() as s:
            with pytest.raises(wp.PackageError):
                await wp.import_package(s, str(path), uid)
            await s.rollback()


def test_redact_secrets_url_semicolon_separator():
    g = {"nodes": [{"id": "s", "type": "http_fetch",
                    "config": {"url": "https://api.x.com/c?q=hi;token=SECRET;page=2"}}], "edges": []}
    red = wp.redact_secrets(g)
    url = g["nodes"][0]["config"]["url"]
    assert "token=SECRET" not in url and wp.REDACTED in url
    assert "q=hi" in url and "page=2" in url          # 分号分隔的非敏感参数保留（不丢）
    assert any(r["field"] == "url.token" for r in red)


def test_redact_secrets_deep_body_is_packageerror():
    deep = "[" * 100 + "1" + "]" * 100               # 100 层：json 可解析但超脱敏深度上限
    g = {"nodes": [{"id": "d", "type": "http_fetch", "config": {"body": deep}}], "edges": []}
    with pytest.raises(wp.PackageError):
        wp.redact_secrets(g)


def test_redact_secrets_deep_value_under_sensitive_key_no_500():
    """敏感键的值本身是超深 dict（不被递归下探、被整体打码）：_is_secret_value 的 json.dumps
    不得 RecursionError 逃逸——应整体打码。"""
    deep = {}; cur = deep
    for _ in range(2500):
        nxt = {}; cur["x"] = nxt; cur = nxt
    g = {"nodes": [{"id": "h", "type": "http_fetch",
                    "config": {"headers": {"authorization": deep}}}], "edges": []}
    red = wp.redact_secrets(g)        # 不得抛 RecursionError
    assert g["nodes"][0]["config"]["headers"]["authorization"] == wp.REDACTED
    assert red == [{"node_id": "h", "field": "authorization"}]


def test_redact_http_params_and_endpoint():
    from app.services.workflow_package import redact_secrets
    graph = {"nodes": [{"id": "f", "type": "http_fetch", "config": {
        "endpoint": "http://api?token=SECRET",
        "params": {"api_key": "KKK", "q": "hi", "tpl": "{{x}}"},
    }}], "edges": []}
    reds = redact_secrets(graph)
    cfg = graph["nodes"][0]["config"]
    assert cfg["params"]["api_key"] == "***REDACTED***"      # 敏感键值打码
    assert cfg["params"]["q"] == "hi"                         # 非敏感保留
    assert cfg["params"]["tpl"] == "{{x}}"                    # 模板值放行
    assert "token=***REDACTED***" in cfg["endpoint"]          # endpoint 查询串脱敏
    fields = {r["field"] for r in reds}
    assert "params.api_key" in fields


def test_parse_manifest_deep_json_is_packageerror(tmp_path):
    deep = "[" * 3000 + "1" + "]" * 3000          # 3000 层数组：json.loads 抛 RecursionError
    raw = ('{"kind":"%s","schema_version":1,'
           '"workflow":{"name":"w","graph":{"nodes":[],"edges":[]}},'
           '"models":[],"prompts":[],"datasets":[],"redactions":[],"junk":%s}'
           % (wp.PACKAGE_KIND, deep))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", raw)
    path = tmp_path / "z.gfpkg"; path.write_bytes(buf.getvalue())
    with wp._open_safe_zip(str(path)) as zf:
        with pytest.raises(wp.PackageError):
            wp._parse_manifest(zf)


async def test_export_deep_default_params_is_packageerror(session_factory, tmp_path):
    deep = {}; cur = deep
    for _ in range(100):
        nxt = {}; cur["x"] = nxt; cur = nxt
    async with session_factory() as s:
        u = User(username="deepdp"); s.add(u); await s.flush()
        m = ModelConfig(user_id=u.id, name="m", base_url="http://x", api_key_enc="e",
                        default_params_json=json.dumps(deep))
        s.add(m); await s.flush()
        graph = {"nodes": [{"id": "g", "type": "llm_synth", "config": {"model_config_id": m.id}}], "edges": []}
        wf = Workflow(user_id=u.id, name="w", graph_json=json.dumps(graph)); s.add(wf); await s.commit()
        wf_id = wf.id
    async with session_factory() as s:
        with pytest.raises(wp.PackageError):
            await wp.export_package(s, await s.get(Workflow, wf_id), str(tmp_path / "o.gfpkg"))


async def test_import_same_name_distinct_resources_not_folded(session_factory, tmp_path):
    """包内两个同名不同 id 的数据集（行不同）在全新账号导入 → 各自新建、行不丢、引用各指各的。"""
    manifest = {**_good_manifest(),
                "workflow": {"name": "w", "graph": {"nodes": [
                    {"id": "in1", "type": "input", "config": {"dataset_ids": [11]}},
                    {"id": "in2", "type": "input", "config": {"dataset_ids": [22]}}], "edges": []}},
                "datasets": [
                    {"id": 11, "name": "data", "columns": ["q"], "row_count": 1, "file": "datasets/11.jsonl"},
                    {"id": 22, "name": "data", "columns": ["q"], "row_count": 1, "file": "datasets/22.jsonl"}]}
    path = tmp_path / "z.gfpkg"
    path.write_bytes(_pkg_bytes(manifest, {"datasets/11.jsonl": '{"q": "a"}\n',
                                           "datasets/22.jsonl": '{"q": "b"}\n'}))
    async with session_factory() as s:
        u = User(username="fold"); s.add(u); await s.flush(); uid = u.id
    async with session_factory() as s:
        wf_out, report = await wp.import_package(s, str(path), uid)
    assert len(report["datasets_created"]) == 2 and not report["datasets_reused"]
    async with session_factory() as s:
        g = json.loads((await s.get(Workflow, wf_out["id"])).graph_json)
        d1 = next(n for n in g["nodes"] if n["id"] == "in1")["config"]["dataset_ids"][0]
        d2 = next(n for n in g["nodes"] if n["id"] == "in2")["config"]["dataset_ids"][0]
        assert d1 != d2          # 两节点指向不同数据集（未折叠）
        rows = {}
        for did in (d1, d2):
            rows[did] = await _read_dataset_rows(s, await s.get(Dataset, did))
        assert sorted(v[0]["q"] for v in rows.values()) == ["a", "b"]   # 两份数据都在，无吞

