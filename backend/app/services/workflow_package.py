"""链路可移植包 .gfpkg：导出（收集引用+脱敏+流式打包）与导入（解压硬化+复用优先重连+原子建链）。
三面（API/CLI/Web）共用。绝不出包 api_key；导入只落导入者账号、无视包内 user_id；单事务原子。"""
import io
import json
import re
import urllib.parse as urlparse
import zipfile
import zlib

from sqlalchemy import insert, select

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
# 仅放行常规压缩方法；imploded/AES 等读取期会抛 NotImplementedError → 提前拒成 422。
_ALLOWED_COMPRESS = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
# 读包阶段一切非 PackageError 的底层异常（损坏流/截断/编码/不支持压缩）统一归一为 PackageError → 422。
_READ_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, OSError, NotImplementedError, UnicodeDecodeError)

# 敏感名（大小写不敏感子串）：http 头名 / url 查询参数名 / body 键名 / 模型 default_params 键名
# 命中即视为凭据。值含 {{ 模板的放行（逐行注入，非固化密钥）。
_SENSITIVE = re.compile(
    r"authorization|cookie|token|secret|key|password|auth|sign|hmac|credential|bearer", re.I)
# 递归脱敏深度上限：远超任何真实配置嵌套，超出即判脏（防 RecursionError 在导出端逃逸成 500）。
_MAX_REDACT_DEPTH = 64


def _is_secret_value(v):
    """敏感键下的值是否需打码：非 None/非空，且字符串化后不含 {{ 模板。
    覆盖任意类型（str/int/float/list/dict）——runner 会对非字符串头/体值 str() 后真实发出，
    故非字符串也可能是活凭据，不能只盯 str。"""
    if v is None or v == "" or v == [] or v == {}:
        return False
    if isinstance(v, str):
        return "{{" not in v
    try:
        s = json.dumps(v, ensure_ascii=False, default=str)
    except RecursionError:
        return True            # 敏感键下深不可测的值：宁可整体打码，绝不冒泄漏/500 风险
    return "{{" not in s


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


def _redact_recursive(obj, node_id, prefix, redactions, depth=0):
    """递归 dict / list：敏感键名的值（任意类型）整体打码并登记；非敏感的容器继续下探。原地改。
    必须遍历 list——否则 list 内 dict（如 extra_body.providers[].api_key）里的密钥永不脱敏。
    超深嵌套抛 PackageError（导出端转 422），不让 RecursionError 逃逸成 500。"""
    if depth > _MAX_REDACT_DEPTH:
        raise PackageError("配置嵌套过深，无法安全脱敏")
    if isinstance(obj, dict):
        for k in list(obj):
            v = obj[k]
            if _SENSITIVE.search(str(k)) and _is_secret_value(v):
                obj[k] = REDACTED
                redactions.append({"node_id": node_id, "field": f"{prefix}.{k}"})
            elif isinstance(v, (dict, list)):
                _redact_recursive(v, node_id, f"{prefix}.{k}", redactions, depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, (dict, list)):
                _redact_recursive(v, node_id, f"{prefix}[{i}]", redactions, depth + 1)


def _redact_url(url, node_id, redactions):
    """剔除 url 的 userinfo(user:pass@)、对敏感查询参数值打码。返回新 url。
    逐组件判模板（含 {{ 的凭据/值放行），且按字符串就地改 query——不经 urlencode，避免把 {{}} 模板百分号转义。"""
    if not isinstance(url, str) or not url:
        return url
    try:
        parts = urlparse.urlsplit(url)
    except ValueError:
        return url
    netloc, query, fragment, changed = parts.netloc, parts.query, parts.fragment, False
    if "@" in netloc:
        creds, host = netloc.rsplit("@", 1)
        if "{{" not in creds:                       # 模板凭据放行
            netloc = f"{REDACTED}@{host}"
            changed = True
            redactions.append({"node_id": node_id, "field": "url.userinfo"})

    def _redact_kv(s, where):                        # query / fragment：& 与 ; 都是合法分隔符
        nonlocal changed
        tokens = re.split(r"([&;])", s)              # 偶数位=参数对，奇数位=分隔符（原样保留，不改 & / ;）
        hit = False
        for i in range(0, len(tokens), 2):
            k, sep, v = tokens[i].partition("=")
            if sep and v and "{{" not in v and _SENSITIVE.search(urlparse.unquote(k)):
                tokens[i] = f"{k}={REDACTED}"; hit = True
                redactions.append({"node_id": node_id, "field": f"{where}.{urlparse.unquote(k)}"})
        if hit:
            changed = True
            return "".join(tokens)
        return s
    if query:
        query = _redact_kv(query, "url")
    if fragment:
        fragment = _redact_kv(fragment, "fragment")
    return (urlparse.urlunsplit(parts._replace(netloc=netloc, query=query, fragment=fragment))
            if changed else url)


def redact_secrets(graph_dict):
    """脱敏 http_fetch 节点的 headers / url / body 里的固化凭据，返回 [{node_id, field}]。
    模板值（含 {{）与非敏感项放行。原地改 graph_dict。"""
    redactions = []
    for node in graph_dict.get("nodes", []):
        if not isinstance(node, dict) or node.get("type") != "http_fetch":
            continue
        cfg = node.get("config")
        if not isinstance(cfg, dict):
            continue
        nid = node.get("id")
        headers = cfg.get("headers")
        if isinstance(headers, dict):
            for k in list(headers):
                if _SENSITIVE.search(str(k)) and _is_secret_value(headers[k]):
                    headers[k] = REDACTED        # 非字符串(int/list/dict)敏感头也整体打码
                    redactions.append({"node_id": nid, "field": str(k)})
        if isinstance(cfg.get("url"), str):
            cfg["url"] = _redact_url(cfg["url"], nid, redactions)
        body = cfg.get("body")
        if isinstance(body, str) and body:           # body 整体可含模板，逐键判模板而非整串
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            except RecursionError:                   # 超深 body：拒绝导出而非 500
                raise PackageError("http body 嵌套过深，无法安全脱敏")
            if isinstance(parsed, (dict, list)):     # 顶层数组的 body 也要脱敏
                before = len(redactions)
                _redact_recursive(parsed, nid, "body", redactions)
                if len(redactions) > before:
                    cfg["body"] = json.dumps(parsed, ensure_ascii=False)
    return redactions


async def export_package(session, workflow, dest_path):
    """收集 workflow 引用到的资源，写 .gfpkg（zip）到 dest_path。数据集行流式写以支持超大文件。
    只收集属于 workflow.user_id 的资源；悬空/非自有引用跳过（导入时降级草稿）。"""
    uid = workflow.user_id
    graph_dict = json.loads(workflow.graph_json)        # 新对象，redact 改它不影响库
    redactions = redact_secrets(graph_dict)
    ds_ids, model_ids, prompt_ids = collect_refs(parse_graph(graph_dict))

    models = []
    for mid in sorted(model_ids):
        m = await session.get(ModelConfig, mid)
        if m is None or m.user_id != uid:
            continue
        # default_params 是无 schema 任意字典，可能夹带凭据（如 extra_body 内代理鉴权）→ 同样脱敏
        try:
            dp = json.loads(m.default_params_json)
        except RecursionError:                       # 超深 default_params：拒绝导出而非 500
            raise PackageError("模型参数嵌套过深，无法安全脱敏")
        _redact_recursive(dp, None, f"model[{m.name}].default_params", redactions)
        models.append({"id": m.id, "name": m.name, "model_name": m.model_name, "base_url": m.base_url,
                       "provider": m.provider, "azure_api_mode": m.azure_api_mode,
                       "api_version": m.api_version, "default_params": dp})
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
        if info.compress_type not in _ALLOWED_COMPRESS:
            zf.close()
            raise PackageError(f"不支持的压缩方法: {info.compress_type}")
        total += info.file_size
    if total > MAX_TOTAL_UNCOMPRESSED:
        zf.close()
        raise PackageError("包解压后体积超限")
    return zf


def _read_entry(zf, name, cap):
    """按 cap+1 限读单条目（防 lying-header 炸弹）；缺/超/损坏流一律 PackageError。"""
    try:
        with zf.open(name) as fh:
            data = fh.read(cap + 1)
    except KeyError:
        raise PackageError(f"包内缺少 {name}")
    except _READ_ERRORS as e:                 # 损坏/截断/CRC错/不支持压缩 → 422 不 500
        raise PackageError(f"读取 {name} 失败: {e}")
    if len(data) > cap:
        raise PackageError(f"{name} 过大")
    return data


def _parse_manifest(zf):
    try:
        m = json.loads(_read_entry(zf, "manifest.json", MAX_MANIFEST_BYTES))
    except ValueError as e:
        raise PackageError(f"manifest.json 不是合法 JSON: {e}")
    except RecursionError:
        raise PackageError("manifest 嵌套过深")
    if not isinstance(m, dict) or m.get("kind") != PACKAGE_KIND:
        raise PackageError("不是 GraphFlow 链路包")
    ver = m.get("schema_version")
    if not isinstance(ver, int) or isinstance(ver, bool):     # 严格 int：拒 True/1.0/"1" 等等值陷阱
        raise PackageError(f"不支持的包版本: {ver!r}")
    if ver != SCHEMA_VERSION:
        if ver > SCHEMA_VERSION:
            raise PackageError(f"包版本过新（v{ver}），请升级 GraphFlow")
        raise PackageError(f"不支持的包版本: {ver!r}")
    wf = m.get("workflow")
    if (not isinstance(wf, dict) or not isinstance(wf.get("graph"), dict)
            or not isinstance(wf.get("name"), str)):
        raise PackageError("manifest.workflow 结构非法")
    # 图 nodes/edges 须为 dict 列表：否则 _rewrite_refs 在 parse_graph(有 GraphError 兜底)之前
    # 就会 TypeError/AttributeError 逃逸成 500（不可信包，必须 422）。
    graph = wf["graph"]
    nodes, edges = graph.get("nodes", []), graph.get("edges", [])
    if not isinstance(nodes, list) or not all(isinstance(n, dict) for n in nodes):
        raise PackageError("manifest 图 nodes 结构非法")
    if not isinstance(edges, list) or not all(isinstance(e, dict) for e in edges):
        raise PackageError("manifest 图 edges 结构非法")
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


async def _name_snapshot(session, model_cls, user_id):
    """导入前快照：导入者已有资源 name → id（同名取 id 最大）。复用判定基于此快照，
    不受本次导入新建（flush 可见）的行影响——否则包内两个同名不同资源会折叠成一个、丢数据。"""
    rows = (await session.execute(
        select(model_cls.id, model_cls.name).where(model_cls.user_id == user_id))).all()
    snap = {}
    for rid, name in rows:
        if name not in snap or rid > snap[name]:
            snap[name] = rid
    return snap


def _require_id(item, kind):
    """资源项 id 必须是严格 int（导出恒用 DB int id）。非 int/缺失/list/dict/bool → 422，
    既挡不可哈希作键 500，也避免字符串/浮点 id 与图中 int 引用静默失配。"""
    rid = item.get("id")
    if not isinstance(rid, int) or isinstance(rid, bool):
        raise PackageError(f"{kind} 项 id 非法: {rid!r}")
    return rid


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
        except _READ_ERRORS as e:
            raise PackageError(f"读取数据集 {fname} 失败: {e}")
        try:
            # utf-8-sig 容 BOM；非 UTF-8/损坏流在迭代时抛 _READ_ERRORS → 422（不可信包不得 500）
            with handle:
                for raw in io.TextIOWrapper(handle, encoding="utf-8-sig"):
                    read_bytes += len(raw.encode("utf-8"))     # 按字节计，多字节内容不绕过上限
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
        except _READ_ERRORS as e:
            raise PackageError(f"读取数据集 {fname} 失败: {e}")
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
              "secrets_need_refill": [], "draft_unresolved": []}
    with _open_safe_zip(zip_path) as zf:
        m = _parse_manifest(zf)
        model_map, prompt_map, ds_map = {}, {}, {}
        # 导入前快照 + 已认领集：每个既有名最多被一个包内资源复用，其余同名项各自新建，
        # 杜绝「同名不同资源折叠成一个 / 数据集行被吞」。
        m_snap = await _name_snapshot(session, ModelConfig, user_id)
        p_snap = await _name_snapshot(session, Prompt, user_id)
        d_snap = await _name_snapshot(session, Dataset, user_id)
        m_claimed, p_claimed, d_claimed = set(), set(), set()

        for item in m["models"]:
            if not isinstance(item, dict):
                raise PackageError("models 项必须是对象")
            rid = _require_id(item, "models")
            if rid in model_map:           # 重复 old_id：只解析一次
                continue
            name = str(item.get("name", ""))
            if name in m_snap and name not in m_claimed:
                m_claimed.add(name)
                model_map[rid] = m_snap[name]
                report["models_reused"].append({"name": name, "id": m_snap[name]})
            else:
                mc = ModelConfig(user_id=user_id, name=name, model_name=str(item.get("model_name", "")),
                                 base_url=str(item.get("base_url", "")),
                                 provider=str(item.get("provider", "openai")),
                                 azure_api_mode=str(item.get("azure_api_mode", "legacy")),
                                 api_version=str(item.get("api_version", "")), api_key_enc="",
                                 default_params_json=json.dumps(item.get("default_params") or {},
                                                                ensure_ascii=False))
                session.add(mc)
                await session.flush()
                model_map[rid] = mc.id
                report["models_created"].append({"name": name, "id": mc.id})
                report["models_need_key"].append({"name": name, "id": mc.id})

        for item in m["prompts"]:
            if not isinstance(item, dict):
                raise PackageError("prompts 项必须是对象")
            rid = _require_id(item, "prompts")
            if rid in prompt_map:
                continue
            name = str(item.get("name", ""))
            if name in p_snap and name not in p_claimed:
                p_claimed.add(name)
                prompt_map[rid] = p_snap[name]
                report["prompts_reused"].append({"name": name, "id": p_snap[name]})
            else:
                pr = Prompt(user_id=user_id, name=name, description=str(item.get("description", "")))
                session.add(pr)
                await session.flush()
                session.add(PromptVersion(prompt_id=pr.id, version=1, body=str(item.get("body", "")),
                                          variables_json=json.dumps(item.get("variables") or [],
                                                                    ensure_ascii=False)))
                prompt_map[rid] = pr.id
                report["prompts_created"].append({"name": name, "id": pr.id})

        for item in m["datasets"]:
            if not isinstance(item, dict):
                raise PackageError("datasets 项必须是对象")
            rid = _require_id(item, "datasets")
            if rid in ds_map:
                continue
            name = str(item.get("name", ""))
            if name in d_snap and name not in d_claimed:
                d_claimed.add(name)
                ds_map[rid] = d_snap[name]
                report["datasets_reused"].append({"name": name, "id": d_snap[name]})
            else:
                new_id = await _create_dataset_streaming(session, zf, item, user_id)
                ds_map[rid] = new_id
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
                report["secrets_need_refill"].append({"node_id": r.get("node_id"), "field": r.get("field")})
        wf_id, wf_name = wf.id, wf.name
        await session.commit()
    return {"id": wf_id, "name": wf_name}, report
