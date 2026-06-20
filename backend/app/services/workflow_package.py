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
