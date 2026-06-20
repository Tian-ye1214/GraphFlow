# 链路导入导出（.gfpkg）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把一条链路（图 + 它引用的全量数据集/模型配置/提示词）打包成自包含、可识别的 `.gfpkg`（zip）文件，支持导出备份/分发与跨账号/部署安全导入还原。

**Architecture:** 新建纯服务层 `app/services/workflow_package.py`（导出收集+脱敏+流式打包；导入解压硬化+复用优先重连+原子建链），由 workflows 路由两端点、gf CLI、Web UI 三面共用。绝不出包 api_key；导入只落导入者账号、无视包内 user_id；单事务原子。

**Tech Stack:** FastAPI + SQLite(WAL)、SQLAlchemy async、Python 标准库 `zipfile`/`json`（无新依赖）；React 前端。

## Global Constraints

- **密钥红线**：模型 `api_key` 永不出包（连字段都不出现）；`http_fetch.headers` 敏感头固化值导出前替成 `"***REDACTED***"`；日志/响应/包内容皆不得含密钥。
- **租户红线**：导入一律落到导入者自己账号；引用只在导入者自有资源内解析；**绝不信任包内任何 user_id**。
- **不可信输入硬化**：上传 zip 视为不可信——条目路径净化（拒 `..`/绝对路径/盘符）、条目数上限、解压总量上限、按 cap 限读（防 lying-header 炸弹）。manifest 形状/JSON 非法一律 422，绝不 500。
- **类型保真**：数据行经 `data_json` round-trip；`json.loads(line, parse_constant=lambda _v: None)`（NaN/Infinity→None），`json.dumps(..., ensure_ascii=False)`。不得把 `"007"`→`7`、`"false"`→`bool`、吞 `"None"`。
- **原子性**：导入全程单事务，成功末尾一次 `commit()`；任何 `PackageError`/`GraphError` 在 commit 前抛出，请求层回滚，不留半截孤儿。
- **KISS**：仅 schema v1；`schema_version` 字段占位，导入 `==1` 放行、`>1` 拒绝，**不写迁移逻辑**。不对不会发生的输入加防御。
- **复用优先**：导入对模型/提示词/数据集"同名则复用（取 id 最大）、缺失才建"；故新建不撞名、不加后缀。加后缀只用于**工作流名**。
- **导出范围**：只导链路定义 + 它引用到的资源；**绝不含运行历史/结果**。
- **测试本地**：`backend/tests/` 被 gitignore，测试文件用 `git add -f`；绝不 stage `__pycache__`/`.pyc`/`.idea`/`.codegraph`。提交信息中文、不含 "claude"、不加 Co-Authored-By。
- **命令路径**：Bash 用绝对路径 `cd "E:/代码/GraphFlow/backend"`（cwd 不跨调用保持）；后端测试 `uv run pytest`。

## 待重映射引用清单（实现依据）

| 引用 | 节点类型 | config 键 | 形状 |
|---|---|---|---|
| 模型 | `llm_synth` | `model_config_id` | int |
| 模型 | `qc` | `judge_model_ids`（旧版回退 `model_config_id`） | list[int]/int |
| 数据集 | `input` | `dataset_ids` | list[int] |
| 提示词 | `llm_synth`/`qc` | `system_prompt_ref`/`user_prompt_ref` | int |
| 密钥（脱敏不重映射） | `http_fetch` | `headers` | dict |

## 文件结构

- **Create** `backend/app/services/workflow_package.py` — 导出/导入核心服务（纯逻辑 + DB 读写，三面共用）。
- **Create** `backend/tests/test_workflow_package.py` — 单元 + 集成 + 对抗测试。
- **Modify** `backend/app/routers/workflows.py` — 加 `GET /{id}/export`、`POST /import` 两端点。
- **Modify** `backend/app/cli/commands/workflow.py` — `export`/`import` 取代 `dump`/`load`。
- **Modify** 前端 `frontend/src/api/*`（调用 + 报告类型）、链路页（导出/导入按钮 + 报告弹窗）。

## 已确认的现有可复用件（实现时直接用，勿重写）

- `app.crypto.encrypt(plain) / decrypt(token)` — 建空 key 模型用 `encrypt("")`（`api_key_enc` 非空字段）。
- `app.engine.graph.parse_graph(dict|str) -> Graph` / `validate_graph(Graph)`（抛 `GraphError`）/ `Graph.nodes`（每个 `Node.id/.type/.config`）。
- `app.routers.datasets._safe_filename(name) -> str`（清非法/控制字符 + 限长 200）。
- `app.routers.workflows.get_owned_workflow(wf_id, user, session)`（非自有 404）。
- `app.models.now()`（带 tz 的 UTC `datetime`）；模型 `Dataset/DatasetRow/ModelConfig/Prompt/PromptVersion/Workflow`。
- 测试夹具（`backend/tests/conftest.py`）：`auth_client`（已登录 `tester`，再 `POST /api/auth/login {"username": X}` 可切租户）、`session_factory`、`client`。

---

### Task 1: 包常量 + 引用收集 + http 头脱敏（纯函数）

**Files:**
- Create: `backend/app/services/workflow_package.py`
- Test: `backend/tests/test_workflow_package.py`

**Interfaces:**
- Produces:
  - `PACKAGE_KIND = "graphflow.workflow.package"`、`SCHEMA_VERSION = 1`、`EXPORTER = "graphflow"`、`REDACTED = "***REDACTED***"`、`MAX_ENTRIES/MAX_MANIFEST_BYTES/MAX_TOTAL_UNCOMPRESSED`
  - `class PackageError(ValueError)`
  - `collect_refs(graph) -> tuple[set[int], set[int], set[int]]`（数据集、模型、提示词 ID 集）
  - `redact_headers(graph_dict: dict) -> list[dict]`（原地改 graph_dict，返回 `[{node_id, header}]`）

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_workflow_package.py
import json
import io
import zipfile
import pytest
import app.services.workflow_package as wp
from app.engine.graph import parse_graph


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
```

- [ ] **Step 2: 运行确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && uv run pytest tests/test_workflow_package.py -q`
Expected: FAIL（`ModuleNotFoundError` / `AttributeError`）

- [ ] **Step 3: 实现**

```python
# backend/app/services/workflow_package.py
"""链路可移植包 .gfpkg：导出（收集引用+脱敏+流式打包）与导入（解压硬化+复用优先重连+原子建链）。
三面（API/CLI/Web）共用。绝不出包 api_key；导入只落导入者账号、无视包内 user_id；单事务原子。"""
import io
import json
import re
import zipfile

from sqlalchemy import insert, select

from app.crypto import encrypt
from app.engine.graph import GraphError, parse_graph, validate_graph
from app.models import Dataset, DatasetRow, ModelConfig, Prompt, PromptVersion, Workflow, now

PACKAGE_KIND = "graphflow.workflow.package"
SCHEMA_VERSION = 1
EXPORTER = "graphflow"
REDACTED = "***REDACTED***"

# 导入侧安全闸（不可信 zip）。业务无上限；这些是防 zip 炸弹/路径穿越的安全网。
MAX_ENTRIES = 10_000
MAX_MANIFEST_BYTES = 64 * 1024 * 1024            # 64MB manifest 限读
MAX_TOTAL_UNCOMPRESSED = 4 * 1024 ** 3           # 4GB 解压总量上限

# 敏感 http 头名（大小写不敏感子串）。值含 {{ 模板的放行（逐行注入，非固化密钥）。
_SENSITIVE_HEADER = re.compile(r"authorization|cookie|token|secret|key|password|auth", re.I)


class PackageError(ValueError):
    """包格式/内容非法（导入端转 422）。"""


def _int_list(v):
    return [x for x in v if isinstance(x, int) and not isinstance(x, bool)] if isinstance(v, list) else []


def collect_refs(graph):
    """遍历节点 config 收集 (数据集ID集, 模型ID集, 提示词ID集)。脏值（非 int / bool）跳过——
    导出是尽力收集，草稿态不在此报错（跑前 runner 自有校验）。"""
    ds, models, prompts = set(), set(), set()
    for node in graph.nodes:
        cfg = node.config if isinstance(node.config, dict) else {}
        ds.update(_int_list(cfg.get("dataset_ids")))
        models.update(_int_list(cfg.get("judge_model_ids")))
        mid = cfg.get("model_config_id")
        if isinstance(mid, int) and not isinstance(mid, bool):
            models.add(mid)
        for slot in ("system_prompt_ref", "user_prompt_ref"):
            pid = cfg.get(slot)
            if isinstance(pid, int) and not isinstance(pid, bool):
                prompts.add(pid)
    return ds, models, prompts


def redact_headers(graph_dict):
    """把 http_fetch 节点 headers 里敏感头的固化值替成 REDACTED；返回 [{node_id, header}]。
    模板值（含 {{）与非敏感头放行。原地改 graph_dict。"""
    redactions = []
    for node in graph_dict.get("nodes", []):
        if not isinstance(node, dict) or node.get("type") != "http_fetch":
            continue
        headers = (node.get("config") or {}).get("headers") if isinstance(node.get("config"), dict) else None
        if not isinstance(headers, dict):
            continue
        for k in list(headers):
            v = headers[k]
            if _SENSITIVE_HEADER.search(str(k)) and isinstance(v, str) and v and "{{" not in v:
                headers[k] = REDACTED
                redactions.append({"node_id": node.get("id"), "header": k})
    return redactions
```

- [ ] **Step 4: 运行确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && uv run pytest tests/test_workflow_package.py -q`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交**

```bash
cd "E:/代码/GraphFlow"
git add backend/app/services/workflow_package.py
git add -f backend/tests/test_workflow_package.py
git commit -m "feat(package): 包常量 + 引用收集 + http 头脱敏纯函数"
```

---

### Task 2: 导出 `export_package`（流式写 zip）

**Files:**
- Modify: `backend/app/services/workflow_package.py`
- Test: `backend/tests/test_workflow_package.py`

**Interfaces:**
- Consumes: `collect_refs`、`redact_headers`、常量（Task 1）
- Produces: `async export_package(session, workflow: Workflow, dest_path) -> None`（把 .gfpkg 写到 dest_path）

- [ ] **Step 1: 写失败测试**

```python
async def _seed_workflow(session_factory):
    """建 1 用户 + 1 模型(带 key) + 1 提示词(2 版) + 1 数据集(2 行) + 1 引用它们的工作流，返回 (uid, wf)。"""
    from app.models import User, ModelConfig, Prompt, PromptVersion, Dataset, DatasetRow, Workflow
    from app.crypto import encrypt
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
        return u.id, wf


async def test_export_package_self_contained_no_key_redacted(session_factory, tmp_path):
    uid, wf = await _seed_workflow(session_factory)
    dest = tmp_path / "out.gfpkg"
    async with session_factory() as s:
        wf = await s.get(type(wf), wf.id)
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
    assert [json.loads(l) for l in ds_lines] == [{"q": "007"}, {"q": "你好"}]
```

- [ ] **Step 2: 运行确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && uv run pytest tests/test_workflow_package.py::test_export_package_self_contained_no_key_redacted -q`
Expected: FAIL（`AttributeError: export_package`）

- [ ] **Step 3: 实现**（追加到服务模块）

```python
async def export_package(session, workflow, dest_path):
    """收集 workflow 引用到的资源，写 .gfpkg（zip）到 dest_path。数据集行流式写以支持超大文件。
    只收集属于 workflow.user_id 的资源；悬空/非自有引用跳过（导入时降级草稿）。"""
    uid = workflow.user_id
    graph_dict = json.loads(workflow.graph_json)        # 新对象，redact 改它不影响库
    redactions = redact_headers(graph_dict)
    ds_ids, model_ids, prompt_ids = collect_refs(parse_graph(graph_dict))

    models = []
    for mid in sorted(model_ids):
        m = await session.get(ModelConfig, mid)
        if m is None or m.user_id != uid:
            continue
        models.append({"id": m.id, "name": m.name, "model_name": m.model_name, "base_url": m.base_url,
                       "provider": m.provider, "azure_api_mode": m.azure_api_mode,
                       "api_version": m.api_version, "default_params": json.loads(m.default_params_json)})
    prompts = []
    for pid in sorted(prompt_ids):
        p = await session.get(Prompt, pid)
        if p is None or p.user_id != uid:
            continue
        pv = (await session.execute(select(PromptVersion).where(PromptVersion.prompt_id == pid)
              .order_by(PromptVersion.version.desc()).limit(1))).scalar_one_or_none()
        prompts.append({"id": p.id, "name": p.name, "description": p.description,
                        "body": pv.body if pv else "",
                        "variables": json.loads(pv.variables_json) if pv else []})
    datasets_meta, valid_ds = [], []
    for did in sorted(ds_ids):
        d = await session.get(Dataset, did)
        if d is None or d.user_id != uid:
            continue
        datasets_meta.append({"id": d.id, "name": d.name, "original_filename": d.original_filename,
                              "columns": json.loads(d.columns_json), "row_count": d.row_count,
                              "file": f"datasets/{d.id}.jsonl"})
        valid_ds.append(d.id)

    manifest = {"kind": PACKAGE_KIND, "schema_version": SCHEMA_VERSION, "exporter": EXPORTER,
                "exported_at": now().isoformat(),
                "source": {"workflow_id": workflow.id, "workflow_name": workflow.name},
                "workflow": {"name": workflow.name, "graph": graph_dict},
                "models": models, "prompts": prompts, "datasets": datasets_meta,
                "redactions": redactions}
    with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for did in valid_ds:
            with zf.open(f"datasets/{did}.jsonl", "w") as fh:
                result = await session.stream(select(DatasetRow.data_json).where(
                    DatasetRow.dataset_id == did).order_by(DatasetRow.idx))
                async for (data_json,) in result:
                    fh.write((data_json + "\n").encode("utf-8"))
```

- [ ] **Step 4: 运行确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && uv run pytest tests/test_workflow_package.py -q`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交**

```bash
cd "E:/代码/GraphFlow"
git add backend/app/services/workflow_package.py
git add -f backend/tests/test_workflow_package.py
git commit -m "feat(package): export_package 流式打包（不含 key/头脱敏/行保真）"
```

---

### Task 3: 导入解压硬化 + manifest 校验

**Files:**
- Modify: `backend/app/services/workflow_package.py`
- Test: `backend/tests/test_workflow_package.py`

**Interfaces:**
- Produces:
  - `_read_entry(zf, name, cap) -> bytes`（按 cap 限读，超/缺抛 PackageError）
  - `_unsafe_name(name) -> bool`
  - `_open_safe_zip(zip_path) -> zipfile.ZipFile`（路径净化 + 条目数 + 解压总量；非 zip/违规抛 PackageError）
  - `_parse_manifest(zf) -> dict`（kind/版本/结构校验，违规抛 PackageError）

- [ ] **Step 1: 写失败测试**

```python
def _pkg_bytes(manifest, extra_files=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        for name, data in (extra_files or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def _good_manifest():
    return {"kind": wp.PACKAGE_KIND, "schema_version": 1, "workflow": {"name": "w", "graph": {"nodes": [], "edges": []}},
            "models": [], "prompts": [], "datasets": [], "redactions": []}


def test_open_safe_zip_rejects_traversal(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", "x")
    path = tmp_path / "z.gfpkg"; path.write_bytes(buf.getvalue())
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
    assert mp(_pkg_bytes(_good_manifest()))["kind"] == wp.PACKAGE_KIND   # 合法放行


def test_open_safe_zip_rejects_non_zip(tmp_path):
    path = tmp_path / "z.gfpkg"; path.write_bytes(b"not a zip")
    with pytest.raises(wp.PackageError):
        wp._open_safe_zip(str(path))
```

- [ ] **Step 2: 运行确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && uv run pytest tests/test_workflow_package.py -q -k "safe_zip or parse_manifest"`
Expected: FAIL

- [ ] **Step 3: 实现**（追加到服务模块）

```python
def _unsafe_name(name):
    n = str(name).replace("\\", "/")
    return n.startswith("/") or ":" in n or ".." in n.split("/")


def _open_safe_zip(zip_path):
    """打开不可信 zip：非法路径 / 条目超数 / 解压总量超限 → PackageError。调用方用 with 关闭。"""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise PackageError(f"不是合法的 zip 文件: {e}")
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        zf.close(); raise PackageError("包内条目过多")
    total = 0
    for info in infos:
        if _unsafe_name(info.filename):
            zf.close(); raise PackageError(f"非法条目路径: {info.filename}")
        total += info.file_size
    if total > MAX_TOTAL_UNCOMPRESSED:
        zf.close(); raise PackageError("包解压后体积超限")
    return zf


def _read_entry(zf, name, cap):
    """按 cap+1 限读单条目（防 lying-header 炸弹）；缺/超抛 PackageError。"""
    try:
        with zf.open(name) as fh:
            data = fh.read(cap + 1)
    except KeyError:
        raise PackageError(f"包内缺少 {name}")
    if len(data) > cap:
        raise PackageError(f"{name} 过大")
    return data


def _parse_manifest(zf):
    try:
        m = json.loads(_read_entry(zf, "manifest.json", MAX_MANIFEST_BYTES))
    except ValueError as e:
        raise PackageError(f"manifest.json 不是合法 JSON: {e}")
    if not isinstance(m, dict) or m.get("kind") != PACKAGE_KIND:
        raise PackageError("不是 GraphFlow 链路包")
    ver = m.get("schema_version")
    if ver != SCHEMA_VERSION:
        if isinstance(ver, int) and not isinstance(ver, bool) and ver > SCHEMA_VERSION:
            raise PackageError(f"包版本过新（v{ver}），请升级 GraphFlow")
        raise PackageError(f"不支持的包版本: {ver!r}")
    wf = m.get("workflow")
    if (not isinstance(wf, dict) or not isinstance(wf.get("graph"), dict)
            or not isinstance(wf.get("name"), str)):
        raise PackageError("manifest.workflow 结构非法")
    for key in ("models", "prompts", "datasets", "redactions"):
        if not isinstance(m.get(key, []), list):
            raise PackageError(f"manifest.{key} 必须是数组")
    return m
```

- [ ] **Step 4: 运行确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && uv run pytest tests/test_workflow_package.py -q`
Expected: PASS（7 passed）

- [ ] **Step 5: 提交**

```bash
cd "E:/代码/GraphFlow"
git add backend/app/services/workflow_package.py
git add -f backend/tests/test_workflow_package.py
git commit -m "feat(package): 导入解压硬化 + manifest 校验"
```

---

### Task 4: 导入 `import_package`（复用优先重连 + 流式建数据集 + 重写 + 原子建链）

**Files:**
- Modify: `backend/app/services/workflow_package.py`
- Test: `backend/tests/test_workflow_package.py`

**Interfaces:**
- Consumes: Task 1-3 全部
- Produces: `async import_package(session, zip_path, user_id) -> tuple[dict, dict]`，返回 `({"id","name"}, report)`；report 见下结构

- [ ] **Step 1: 写失败测试**

```python
async def test_import_roundtrip_cross_tenant_reuse_and_create(session_factory, tmp_path):
    # 导出账号 A 的链路
    uid_a, wf = await _seed_workflow(session_factory)
    dest = tmp_path / "out.gfpkg"
    async with session_factory() as s:
        await wp.export_package(s, await s.get(type(wf), wf.id), str(dest))
    # 账号 B 先有一个同名模型 "m1"（带 key）→ 导入应复用它；提示词/数据集 B 没有 → 新建
    from app.models import User, ModelConfig, Workflow, Dataset, DatasetRow, Prompt
    from app.crypto import encrypt, decrypt
    async with session_factory() as s:
        b = User(username="importer"); s.add(b); await s.flush()
        s.add(ModelConfig(user_id=b.id, name="m1", model_name="b-model", base_url="http://b",
                          api_key_enc=encrypt("B-KEY"), default_params_json="{}"))
        await s.commit(); uid_b = b.id
    async with session_factory() as s:
        wf_out, report = await wp.import_package(s, str(dest), uid_b)
    # 新建工作流落在 B 账号
    async with session_factory() as s:
        new = await s.get(Workflow, wf_out["id"])
        assert new.user_id == uid_b
        g = json.loads(new.graph_json)
        gen = next(n for n in g["nodes"] if n["id"] == "g")
        # 模型复用 B 既有同名（id 指向 B 的 m1，非包内 A 的 id）
        bm = (await s.execute(select(ModelConfig).where(ModelConfig.user_id == uid_b, ModelConfig.name == "m1"))).scalars().first()
        assert gen["config"]["model_config_id"] == bm.id
        assert decrypt(bm.api_key_enc) == "B-KEY"      # 复用的模型保留自己的 key，未被覆盖
        # 提示词新建并重连
        pid = gen["config"]["system_prompt_ref"]
        np = await s.get(Prompt, pid); assert np.user_id == uid_b
        # 数据集新建并重连，行保真
        inn = next(n for n in g["nodes"] if n["id"] == "in")
        did = inn["config"]["dataset_ids"][0]
        nd = await s.get(Dataset, did); assert nd.user_id == uid_b and nd.row_count == 2
        rows = (await s.execute(select(DatasetRow.data_json).where(DatasetRow.dataset_id == did).order_by(DatasetRow.idx))).all()
        assert [json.loads(r[0]) for r in rows] == [{"q": "007"}, {"q": "你好"}]
    assert report["models_reused"] and report["datasets_created"] and report["prompts_created"]
    assert report["headers_need_refill"] == [{"node_id": "h", "header": "Authorization"}]


async def test_import_atomic_rollback_on_bad_graph(session_factory, tmp_path):
    # 构造一个 manifest：图有环（validate 失败），但带一个数据集 → 必须整体回滚、不留孤儿数据集
    from app.models import User, Dataset
    async with session_factory() as s:
        u = User(username="atom"); s.add(u); await s.flush(); uid = u.id
    manifest = {"kind": wp.PACKAGE_KIND, "schema_version": 1,
                "workflow": {"name": "环", "graph": {"nodes": [
                    {"id": "a", "type": "auto_process", "config": {}},
                    {"id": "b", "type": "auto_process", "config": {}}],
                    "edges": [{"source": "a", "target": "b", "kind": "normal"},
                              {"source": "b", "target": "a", "kind": "normal"}]}},
                "models": [], "prompts": [],
                "datasets": [{"id": 9, "name": "孤儿集", "columns": ["q"], "row_count": 1, "file": "datasets/9.jsonl"}],
                "redactions": []}
    path = tmp_path / "z.gfpkg"; path.write_bytes(_pkg_bytes(manifest, {"datasets/9.jsonl": '{"q": "1"}\n'}))
    async with session_factory() as s:
        with pytest.raises(wp.PackageError):
            await wp.import_package(s, str(path), uid)
        await s.rollback()
    async with session_factory() as s:
        left = (await s.execute(select(Dataset).where(Dataset.user_id == uid))).scalars().all()
        assert left == []      # 回滚后无孤儿数据集
```

- [ ] **Step 2: 运行确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && uv run pytest tests/test_workflow_package.py -q -k import`
Expected: FAIL（`AttributeError: import_package`）

- [ ] **Step 3: 实现**（追加到服务模块）

```python
def _loads_row(line):
    """解析数据行：NaN/Infinity→None（防渲染 500）；深嵌套 RecursionError→PackageError。"""
    try:
        return json.loads(line, parse_constant=lambda _v: None)
    except RecursionError as e:
        raise PackageError(f"数据行嵌套过深: {e}")
    except ValueError as e:
        raise PackageError(f"数据行不是合法 JSON: {e}")


async def _reuse_by_name(session, model_cls, user_id, name):
    return (await session.execute(select(model_cls).where(
        model_cls.user_id == user_id, model_cls.name == name
    ).order_by(model_cls.id.desc()))).scalars().first()


async def _create_dataset_streaming(session, zf, dmeta, user_id):
    """从 zip 内 jsonl 流式建数据集（批量插入，bound 内存）；行类型保真。返回新 id。"""
    ds = Dataset(user_id=user_id, name=dmeta.get("name", ""), source="upload",
                 original_filename=dmeta.get("original_filename", ""),
                 columns_json=json.dumps(dmeta.get("columns") or [], ensure_ascii=False), row_count=0)
    session.add(ds); await session.flush()
    fname = dmeta.get("file")
    count, read_bytes, batch = 0, 0, []
    if isinstance(fname, str) and fname:
        if _unsafe_name(fname):
            raise PackageError(f"非法数据集路径: {fname}")
        try:
            handle = zf.open(fname)
        except KeyError:
            raise PackageError(f"包内缺少数据集文件 {fname}")
        with handle:
            for raw in io.TextIOWrapper(handle, encoding="utf-8"):
                read_bytes += len(raw)
                if read_bytes > MAX_TOTAL_UNCOMPRESSED:
                    raise PackageError("数据集解压超限")
                line = raw.strip()
                if not line:
                    continue
                obj = _loads_row(line)
                if not isinstance(obj, dict):
                    raise PackageError(f"数据集 {fname} 第 {count + 1} 行不是 JSON 对象")
                batch.append({"dataset_id": ds.id, "idx": count,
                              "data_json": json.dumps(obj, ensure_ascii=False)})
                count += 1
                if len(batch) >= 1000:
                    await session.execute(insert(DatasetRow), batch); batch = []
        if batch:
            await session.execute(insert(DatasetRow), batch)
    ds.row_count = count
    return ds.id


def _remap(old, mapping, node_id, kind, report):
    if not isinstance(old, int) or isinstance(old, bool):
        return old                                   # 脏值原样留（草稿态），不在导入处报错
    new = mapping.get(old)
    if new is None:
        report["draft_unresolved"].append({"node_id": node_id, "kind": kind, "old_id": old})
    return new                                        # 缺失→None（降级草稿）


def _rewrite_refs(graph_dict, model_map, prompt_map, ds_map, report):
    for node in graph_dict.get("nodes", []):
        cfg = node.get("config")
        if not isinstance(cfg, dict):
            continue
        nid = node.get("id")
        if isinstance(cfg.get("dataset_ids"), list):
            cfg["dataset_ids"] = [x for x in (_remap(x, ds_map, nid, "数据集", report)
                                              for x in cfg["dataset_ids"]) if x is not None]
        if isinstance(cfg.get("model_config_id"), int) and not isinstance(cfg.get("model_config_id"), bool):
            cfg["model_config_id"] = _remap(cfg["model_config_id"], model_map, nid, "模型", report)
        if isinstance(cfg.get("judge_model_ids"), list):
            cfg["judge_model_ids"] = [x for x in (_remap(x, model_map, nid, "模型", report)
                                                  for x in cfg["judge_model_ids"]) if x is not None]
        for slot in ("system_prompt_ref", "user_prompt_ref"):
            if isinstance(cfg.get(slot), int) and not isinstance(cfg.get(slot), bool):
                cfg[slot] = _remap(cfg[slot], prompt_map, nid, "提示词", report)


async def _unique_wf_name(session, user_id, base):
    existing = set((await session.execute(
        select(Workflow.name).where(Workflow.user_id == user_id))).scalars().all())
    cand = f"{base}(导入)"
    i = 2
    while cand in existing:
        cand = f"{base}(导入 {i})"; i += 1
    return cand


async def import_package(session, zip_path, user_id):
    """导入 .gfpkg：硬化解压 → 校验 → 复用优先重连 → 重写 graph → validate → 建工作流。
    单事务：成功末尾一次 commit；失败前不 commit（异常上抛由请求层回滚）。"""
    report = {"models_reused": [], "models_created": [], "models_need_key": [],
              "prompts_reused": [], "prompts_created": [],
              "datasets_reused": [], "datasets_created": [],
              "headers_need_refill": [], "draft_unresolved": []}
    with _open_safe_zip(zip_path) as zf:
        m = _parse_manifest(zf)
        model_map, prompt_map, ds_map = {}, {}, {}

        for item in m["models"]:
            if not isinstance(item, dict):
                raise PackageError("models 项必须是对象")
            name = str(item.get("name", ""))
            ex = await _reuse_by_name(session, ModelConfig, user_id, name)
            if ex is not None:
                model_map[item.get("id")] = ex.id
                report["models_reused"].append({"name": name, "id": ex.id})
            else:
                mc = ModelConfig(user_id=user_id, name=name, model_name=str(item.get("model_name", "")),
                                 base_url=str(item.get("base_url", "")), provider=str(item.get("provider", "openai")),
                                 azure_api_mode=str(item.get("azure_api_mode", "legacy")),
                                 api_version=str(item.get("api_version", "")), api_key_enc=encrypt(""),
                                 default_params_json=json.dumps(item.get("default_params") or {}, ensure_ascii=False))
                session.add(mc); await session.flush()
                model_map[item.get("id")] = mc.id
                report["models_created"].append({"name": name, "id": mc.id})
                report["models_need_key"].append({"name": name, "id": mc.id})

        for item in m["prompts"]:
            if not isinstance(item, dict):
                raise PackageError("prompts 项必须是对象")
            name = str(item.get("name", ""))
            ex = await _reuse_by_name(session, Prompt, user_id, name)
            if ex is not None:
                prompt_map[item.get("id")] = ex.id
                report["prompts_reused"].append({"name": name, "id": ex.id})
            else:
                pr = Prompt(user_id=user_id, name=name, description=str(item.get("description", "")))
                session.add(pr); await session.flush()
                session.add(PromptVersion(prompt_id=pr.id, version=1, body=str(item.get("body", "")),
                                          variables_json=json.dumps(item.get("variables") or [], ensure_ascii=False)))
                prompt_map[item.get("id")] = pr.id
                report["prompts_created"].append({"name": name, "id": pr.id})

        for item in m["datasets"]:
            if not isinstance(item, dict):
                raise PackageError("datasets 项必须是对象")
            name = str(item.get("name", ""))
            ex = await _reuse_by_name(session, Dataset, user_id, name)
            if ex is not None:
                ds_map[item.get("id")] = ex.id
                report["datasets_reused"].append({"name": name, "id": ex.id})
            else:
                new_id = await _create_dataset_streaming(session, zf, item, user_id)
                ds_map[item.get("id")] = new_id
                report["datasets_created"].append({"name": name, "id": new_id})

        graph_dict = m["workflow"]["graph"]
        _rewrite_refs(graph_dict, model_map, prompt_map, ds_map, report)
        try:
            validate_graph(parse_graph(graph_dict))
        except GraphError as e:
            raise PackageError(f"链路图非法: {e}")
        name = await _unique_wf_name(session, user_id, m["workflow"]["name"])
        wf = Workflow(user_id=user_id, name=name, graph_json=json.dumps(graph_dict, ensure_ascii=False))
        session.add(wf); await session.flush()
        for r in m["redactions"]:
            if isinstance(r, dict):
                report["headers_need_refill"].append({"node_id": r.get("node_id"), "header": r.get("header")})
        wf_id, wf_name = wf.id, wf.name
        await session.commit()
    return {"id": wf_id, "name": wf_name}, report
```

- [ ] **Step 4: 运行确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && uv run pytest tests/test_workflow_package.py -q`
Expected: PASS（全绿）

- [ ] **Step 5: 提交**

```bash
cd "E:/代码/GraphFlow"
git add backend/app/services/workflow_package.py
git add -f backend/tests/test_workflow_package.py
git commit -m "feat(package): import_package 复用优先重连 + 流式建集 + 原子建链"
```

---

### Task 5: REST 端点 `GET /{id}/export`、`POST /import`

**Files:**
- Modify: `backend/app/routers/workflows.py`
- Test: `backend/tests/test_workflow_package.py`

**Interfaces:**
- Consumes: `export_package`、`import_package`、`PackageError`（服务层）；`get_owned_workflow`、`_safe_filename`
- Produces: 两个路由端点

- [ ] **Step 1: 写失败测试**

```python
async def test_export_import_endpoints_roundtrip(auth_client, tmp_path):
    # tester 建数据集 + 工作流
    up = await auth_client.post("/api/datasets/upload",
        files={"files": ("d.jsonl", b'{"q": "007"}\n{"q": "x"}\n', "application/x-ndjson")})
    did = up.json()[0]["id"]
    wf = (await auth_client.post("/api/workflows", json={"name": "导链"})).json()
    graph = {"nodes": [{"id": "in", "type": "input", "config": {"dataset_ids": [did]}}], "edges": []}
    await auth_client.put(f"/api/workflows/{wf['id']}", json={"graph": graph})
    # 导出
    r = await auth_client.get(f"/api/workflows/{wf['id']}/export")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    pkg = r.content
    # 切到另一个账号导入
    await auth_client.post("/api/auth/login", json={"username": "importer2"})
    r2 = await auth_client.post("/api/workflows/import",
        files={"file": ("x.gfpkg", pkg, "application/zip")})
    assert r2.status_code == 200
    body = r2.json()
    assert body["workflow"]["name"] == "导链(导入)"
    assert body["report"]["datasets_created"]
    # 新账号能看到这条导入的链路且数据集行保真
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
```

- [ ] **Step 2: 运行确认失败**

Run: `cd "E:/代码/GraphFlow/backend" && uv run pytest tests/test_workflow_package.py -q -k endpoint`
Expected: FAIL（404/405，端点未定义）

- [ ] **Step 3: 实现**（在 `backend/app/routers/workflows.py` 顶部加导入，文件尾加两端点）

顶部导入区追加：
```python
import os
import tempfile
from urllib.parse import quote

from fastapi import UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.routers.datasets import _safe_filename
from app.services.workflow_package import export_package, import_package, PackageError
```

文件尾追加：
```python
@router.get("/{wf_id}/export")
async def export_workflow(wf_id: int, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    wf = await get_owned_workflow(wf_id, user, session)
    fd, tmp = tempfile.mkstemp(suffix=".gfpkg", dir=settings.data_dir)
    os.close(fd)
    try:
        await export_package(session, wf, tmp)
    except Exception:
        os.unlink(tmp)
        raise
    safe = _safe_filename(wf.name)
    ascii_name = (safe.encode("ascii", "ignore").decode().strip() or "workflow") + ".gfpkg"
    disp = (f"attachment; filename=\"{ascii_name}\"; "
            f"filename*=UTF-8''{quote(safe + '.gfpkg')}")
    return FileResponse(tmp, media_type="application/zip",
                        headers={"Content-Disposition": disp},
                        background=BackgroundTask(os.unlink, tmp))


@router.post("/import")
async def import_workflow(file: UploadFile, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    fd, tmp = tempfile.mkstemp(suffix=".gfpkg", dir=settings.data_dir)
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                out.write(chunk)
        try:
            wf_out, report = await import_package(session, tmp, user.id)
        except (PackageError, GraphError) as e:
            raise HTTPException(status_code=422, detail=str(e))
    finally:
        os.unlink(tmp)
    publish(user.id, "workflow", wf_out["id"])
    return {"workflow": wf_out, "report": report}
```

> 注：`from app.routers.datasets import _safe_filename` 不形成环（datasets 不导入 workflows）。若实测有环，则把 `_safe_filename` 下沉到 `app/services/file_parse.py` 复用，两路由各自导入服务层。

- [ ] **Step 4: 运行确认通过**

Run: `cd "E:/代码/GraphFlow/backend" && uv run pytest tests/test_workflow_package.py -q`
Expected: PASS（全绿）

- [ ] **Step 5: 提交**

```bash
cd "E:/代码/GraphFlow"
git add backend/app/routers/workflows.py
git add -f backend/tests/test_workflow_package.py
git commit -m "feat(package): workflows export/import 两端点"
```

---

### Task 6: gf CLI `wf export` / `wf import`（取代 `dump`/`load`）

**Files:**
- Modify: `backend/app/cli/commands/workflow.py`
- Test: `backend/tests/test_workflow_package.py`（轻量：直接调命令函数，mock Cli 太重则跳；至少覆盖 register 不报错与 argparse 形状）

**Interfaces:**
- Consumes: 服务端两端点
- Produces: `cmd_wf_export(args)`、`cmd_wf_import(args)`；register 中 `export`/`import` 子命令；移除 `dump`/`load`

- [ ] **Step 1: 实现**（替换 `cmd_wf_dump`/`cmd_wf_load` 两函数）

```python
def cmd_wf_export(args):
    cli = Cli()
    wf_id = cli.resolve("workflows", args.ref) if args.ref else cli.current_wf()
    wf = cli.req("GET", f"/api/workflows/{wf_id}")
    r = cli.check(cli.http.get(f"/api/workflows/{wf_id}/export"))
    out = Path(args.output) if args.output else Path(f"{wf['name']}.gfpkg")
    out.write_bytes(r.content)
    print(f"已导出链路「{wf['name']}」到 {out}")


def cmd_wf_import(args):
    cli = Cli()
    path = Path(args.file)
    if not path.is_file():
        die(f"文件不存在: {args.file}")
    with open(path, "rb") as f:
        d = cli.req("POST", "/api/workflows/import",
                    files={"file": (path.name, f, "application/zip")})
    rep, w = d["report"], d["workflow"]
    print(f"已导入为链路「{w['name']}」(#{w['id']})")
    if rep["models_reused"] or rep["datasets_reused"] or rep["prompts_reused"]:
        print("  复用: " + "、".join(
            [f"模型 {x['name']}" for x in rep["models_reused"]]
            + [f"提示词 {x['name']}" for x in rep["prompts_reused"]]
            + [f"数据集 {x['name']}" for x in rep["datasets_reused"]]))
    if rep["models_need_key"]:
        print("  待回填密钥的模型: " + "、".join(x["name"] for x in rep["models_need_key"]))
    if rep["headers_need_refill"]:
        print("  待回填的 http 头: " + "、".join(
            f"{x['node_id']}.{x['header']}" for x in rep["headers_need_refill"]))
    if rep["draft_unresolved"]:
        print("  ⚠ 有引用无法解析，已降级草稿: " + "、".join(
            f"{x['node_id']}({x['kind']})" for x in rep["draft_unresolved"]))
```

`register(sub)` 中删掉 dump/load 两行，替为：
```python
    s = wf.add_parser("export", help="导出链路为 .gfpkg 包")
    s.add_argument("ref", nargs="?"); s.add_argument("-o", "--output"); s.set_defaults(func=cmd_wf_export)
    s = wf.add_parser("import", help="从 .gfpkg 包导入链路")
    s.add_argument("file"); s.set_defaults(func=cmd_wf_import)
```
并把模块顶部 docstring 的 `dump|load` 改为 `export|import`。

- [ ] **Step 2: 写/跑形状测试**

```python
def test_cli_workflow_register_has_export_import():
    import argparse
    from app.cli.commands import workflow as wfcmd
    parser = argparse.ArgumentParser()
    wfcmd.register(parser.add_subparsers(dest="cmd"))
    # export/import 可解析，dump/load 已移除
    assert parser.parse_args(["wf", "export", "x"]).func is wfcmd.cmd_wf_export
    assert parser.parse_args(["wf", "import", "f.gfpkg"]).func is wfcmd.cmd_wf_import
    with pytest.raises(SystemExit):
        parser.parse_args(["wf", "dump"])
```

Run: `cd "E:/代码/GraphFlow/backend" && uv run pytest tests/test_workflow_package.py -q -k cli`
Expected: PASS

- [ ] **Step 3: 提交**

```bash
cd "E:/代码/GraphFlow"
git add backend/app/cli/commands/workflow.py
git add -f backend/tests/test_workflow_package.py
git commit -m "feat(package): gf wf export/import 取代 dump/load"
```

---

### Task 7: 前端导出/导入按钮 + 报告

**Files:**
- Modify: 前端 API 层（`frontend/src/api/`，与现有 workflows 调用同文件）、链路页组件、`frontend/src/api/types.ts`
- 实现前先 `Read` 现有 workflows API 调用与链路页，沿用既有按钮/弹窗样式与请求封装。

**Interfaces:**
- Produces：`exportWorkflow(id)`（触发下载）、`importWorkflow(file)`（上传，返回报告）、`ImportReport` 类型；链路页两个按钮 + 导入后报告弹窗

- [ ] **Step 1: 类型**（`frontend/src/api/types.ts` 追加）

```typescript
export interface ImportReport {
  models_reused: { name: string; id: number }[]
  models_created: { name: string; id: number }[]
  models_need_key: { name: string; id: number }[]
  prompts_reused: { name: string; id: number }[]
  prompts_created: { name: string; id: number }[]
  datasets_reused: { name: string; id: number }[]
  datasets_created: { name: string; id: number }[]
  headers_need_refill: { node_id: string; header: string }[]
  draft_unresolved: { node_id: string; kind: string; old_id: number }[]
}
export interface ImportResult { workflow: { id: number; name: string }; report: ImportReport }
```

- [ ] **Step 2: API 调用**（沿用现有 axios/fetch 封装；导出走 blob 下载，导入走 multipart）

```typescript
// 导出：取 blob，按响应头文件名触发下载
export async function exportWorkflow(id: number): Promise<void> {
  const res = await api.get(`/workflows/${id}/export`, { responseType: 'blob' })
  const dispo = res.headers['content-disposition'] || ''
  const m = /filename\*=UTF-8''([^;]+)/.exec(dispo)
  const name = m ? decodeURIComponent(m[1]) : 'workflow.gfpkg'
  const url = URL.createObjectURL(res.data)
  const a = document.createElement('a'); a.href = url; a.download = name; a.click()
  URL.revokeObjectURL(url)
}

export async function importWorkflow(file: File): Promise<ImportResult> {
  const fd = new FormData(); fd.append('file', file)
  return (await api.post('/workflows/import', fd)).data
}
```
> 以上 `api` 实例与 baseURL 须对齐现有封装（实现时按仓库实际写法调整：若用原生 fetch，则相应改写）。

- [ ] **Step 3: UI**：链路列表/编辑页加「导出」按钮（调 `exportWorkflow`）与「导入」按钮（隐藏 `<input type=file accept=".gfpkg,.zip">`，选完调 `importWorkflow`，成功后刷新列表/跳转新链路并弹出报告：复用/新建/待回填 http 头/待回填模型 key/降级草稿项）。沿用现有弹窗组件。

- [ ] **Step 4: 前端校验**

Run: `cd "E:/代码/GraphFlow/frontend" && npx tsc -b && npx vitest run`
Expected: tsc 0 错；vitest 全绿（既有 + 视情况新增 1 个 importWorkflow 单测）

- [ ] **Step 5: 提交**

```bash
cd "E:/代码/GraphFlow"
git add frontend/src
git commit -m "feat(package): 前端链路导出/导入按钮 + 导入报告"
```

---

### Task 8: 全量验证 + 活体测试 + 记忆

**Files:** 无源码改动（验证为主）

- [ ] **Step 1: 后端全量**

Run: `cd "E:/代码/GraphFlow/backend" && uv run pytest -q`
Expected: 全绿（基线 500 + 本批新增；无 1 失败）

- [ ] **Step 2: 前端全量**

Run: `cd "E:/代码/GraphFlow/frontend" && npx tsc -b && npx vitest run`
Expected: tsc 0 错；vitest 全绿

- [ ] **Step 3: 对抗式 review**（dispatch 3-5 个只读 subagent，分别从安全/租户/类型保真/原子性/边界视角审 `workflow_package.py` 与两端点；确认无密钥泄露、无跨租户、无 500 逃逸、回滚干净）。发现的 Critical/Important 修掉再回归。

- [ ] **Step 4: 活体**（真实后端 + 真实 DeepSeek；建即删 `smoke_` 前缀）

```
1. 真实导出一条 input+llm 链路 → .gfpkg。
2. 切 smoke 用户导入 → 复用既有 zrs 同名模型(自带 key) → 跑通真实翻译。
3. 删除所有 smoke 资源；只读 DB 复验无孤儿、zrs 真实数据零损失、基线还原。
```
活体脚本临时落 `backend/.live_pkg.py`，跑完 `rm -f`。

- [ ] **Step 5: 记忆 + 收尾**：写本批 project 记忆（单点位置：`workflow_package.py` 是导入导出唯一切口；新增引用类型只改 `collect_refs`/`_rewrite_refs` 两处），更新 `MEMORY.md` 索引。确认工作树仅余 `.codegraph/`、`.idea/`。

- [ ] **Step 6: 完成开发分支**：用 superpowers:finishing-a-development-branch（验证测试 → 选项菜单 → 用户确认后合并 master 并删分支）。
```
