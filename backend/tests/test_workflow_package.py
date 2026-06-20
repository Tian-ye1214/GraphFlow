import io
import json
import zipfile

import pytest

import app.services.workflow_package as wp
from app.engine.graph import parse_graph
from app.models import (Dataset, DatasetRow, ModelConfig, Prompt, PromptVersion, User, Workflow)


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


def test_redact_headers_only_sensitive_literal_values():
    g = {"nodes": [
        {"id": "h", "type": "http_fetch", "config": {"headers": {
            "Authorization": "Bearer sk-secret", "X-Api-Key": "abc",
            "Content-Type": "application/json", "X-Token": "{{tok}}"}}},
        {"id": "x", "type": "input", "config": {}},
    ], "edges": []}
    red = wp.redact_headers(g)
    h = g["nodes"][0]["config"]["headers"]
    assert h["Authorization"] == wp.REDACTED
    assert h["X-Api-Key"] == wp.REDACTED
    assert h["Content-Type"] == "application/json"   # 非敏感头保留
    assert h["X-Token"] == "{{tok}}"                 # 模板值放行
    assert {(r["node_id"], r["header"]) for r in red} == {("h", "Authorization"), ("h", "X-Api-Key")}


# ---------------- Task 2: 导出 ----------------

async def _seed_workflow(session_factory):
    """建 1 用户 + 1 模型(带 key) + 1 提示词(2 版) + 1 数据集(2 行) + 1 引用它们的工作流。
    返回 (uid, wf_id)。"""
    from app.crypto import encrypt
    from app.models import (Dataset, DatasetRow, ModelConfig, Prompt, PromptVersion, User,
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
    assert manifest["redactions"] == [{"node_id": "h", "header": "Authorization"}]
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
        rows = (await s.execute(select(DatasetRow.data_json).where(
            DatasetRow.dataset_id == did).order_by(DatasetRow.idx))).all()
        assert [json.loads(r[0]) for r in rows] == [{"q": "007"}, {"q": "你好"}]   # 行保真
    assert report["models_reused"] and report["datasets_created"] and report["prompts_created"]
    assert report["headers_need_refill"] == [{"node_id": "h", "header": "Authorization"}]


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

