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
        cfg = node.get("config")
        headers = cfg.get("headers") if isinstance(cfg, dict) else None
        if not isinstance(headers, dict):
            continue
        for k in list(headers):
            v = headers[k]
            if _SENSITIVE_HEADER.search(str(k)) and isinstance(v, str) and v and "{{" not in v:
                headers[k] = REDACTED
                redactions.append({"node_id": node.get("id"), "header": k})
    return redactions


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
        zf.close()
        raise PackageError("包内条目过多")
    total = 0
    for info in infos:
        if _unsafe_name(info.filename):
            zf.close()
            raise PackageError(f"非法条目路径: {info.filename}")
        total += info.file_size
    if total > MAX_TOTAL_UNCOMPRESSED:
        zf.close()
        raise PackageError("包解压后体积超限")
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
    ds = Dataset(user_id=user_id, name=str(dmeta.get("name", "")), source="upload",
                 original_filename=str(dmeta.get("original_filename", "")),
                 columns_json=json.dumps(dmeta.get("columns") or [], ensure_ascii=False), row_count=0)
    session.add(ds)
    await session.flush()
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
                    await session.execute(insert(DatasetRow), batch)
                    batch = []
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
        mid = cfg.get("model_config_id")
        if isinstance(mid, int) and not isinstance(mid, bool):
            cfg["model_config_id"] = _remap(mid, model_map, nid, "模型", report)
        if isinstance(cfg.get("judge_model_ids"), list):
            cfg["judge_model_ids"] = [x for x in (_remap(x, model_map, nid, "模型", report)
                                                  for x in cfg["judge_model_ids"]) if x is not None]
        for slot in ("system_prompt_ref", "user_prompt_ref"):
            sid = cfg.get(slot)
            if isinstance(sid, int) and not isinstance(sid, bool):
                cfg[slot] = _remap(sid, prompt_map, nid, "提示词", report)


async def _unique_wf_name(session, user_id, base):
    existing = set((await session.execute(
        select(Workflow.name).where(Workflow.user_id == user_id))).scalars().all())
    cand = f"{base}(导入)"
    i = 2
    while cand in existing:
        cand = f"{base}(导入 {i})"
        i += 1
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
                                 base_url=str(item.get("base_url", "")),
                                 provider=str(item.get("provider", "openai")),
                                 azure_api_mode=str(item.get("azure_api_mode", "legacy")),
                                 api_version=str(item.get("api_version", "")), api_key_enc=encrypt(""),
                                 default_params_json=json.dumps(item.get("default_params") or {},
                                                                ensure_ascii=False))
                session.add(mc)
                await session.flush()
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
                session.add(pr)
                await session.flush()
                session.add(PromptVersion(prompt_id=pr.id, version=1, body=str(item.get("body", "")),
                                          variables_json=json.dumps(item.get("variables") or [],
                                                                    ensure_ascii=False)))
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
        session.add(wf)
        await session.flush()
        for r in m["redactions"]:
            if isinstance(r, dict):
                report["headers_need_refill"].append({"node_id": r.get("node_id"), "header": r.get("header")})
        wf_id, wf_name = wf.id, wf.name
        await session.commit()
    return {"id": wf_id, "name": wf_name}, report
