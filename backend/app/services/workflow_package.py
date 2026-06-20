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
