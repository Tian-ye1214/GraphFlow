"""目标优化循环：跳出判定（纯函数）与轮次提示构造（从 prompts 读模板）。"""
import json
from dataclasses import dataclass

from app.agent.prompts import load_prompt


@dataclass
class Decision:
    stop: bool
    success: bool
    reason: str
    new_best: float
    new_no_improve: int


@dataclass
class GraphGuardDecision:
    ok: bool
    reason: str


def decide(*, metric, threshold, best, no_improve, no_improve_k) -> Decision:
    """根据本轮实测指标决定是否跳出。metric/threshold 可为 None。"""
    if metric is None:                           # 本轮没算出指标（如跑数失败）：计入无提升
        no_improve += 1
        if threshold is not None and no_improve >= no_improve_k:
            return Decision(True, False, f"连续 {no_improve} 轮无提升，停止（最佳 {best:.1%}）", best, no_improve)
        return Decision(False, False, "本轮无有效指标", best, no_improve)
    if threshold is not None and metric >= threshold:
        return Decision(True, True, f"✅ 目标达成：首轮质检通过率 {metric:.1%} ≥ {threshold:.1%}", max(best, metric), 0)
    if metric > best:
        return Decision(False, False, "指标提升", metric, 0)
    no_improve += 1
    if threshold is not None and no_improve >= no_improve_k:
        return Decision(True, False, f"连续 {no_improve} 轮无提升，停止（最佳 {best:.1%}）", best, no_improve)
    return Decision(False, False, "指标未提升", best, no_improve)


def build_round_prompt(goal_text: str, metric, failures: list, run_id: int) -> str:
    metric_str = "（首轮尚无指标）" if metric is None else f"{metric:.1%}"
    fail_str = json.dumps(failures, ensure_ascii=False, indent=2) if failures else "（无失败样本）"
    return load_prompt("goal_round.md").format(
        goal_text=goal_text, run_id=run_id, metric_str=metric_str, fail_str=fail_str)


def first_round_prompt(goal_text: str) -> str:
    return load_prompt("goal_first_round.md").format(goal_text=goal_text)


def _nodes_by_id(graph: dict) -> dict:
    return {
        n.get("id"): n
        for n in graph.get("nodes", [])
        if isinstance(n, dict) and isinstance(n.get("id"), str)
    }


def _input_dataset_ids(node: dict) -> list:
    cfg = node.get("config") or {}
    return list(cfg.get("dataset_ids") or [])


def _qc_config(node: dict) -> dict:
    cfg = dict(node.get("config") or {})
    return cfg


def validate_goal_graph_change(before: dict, after: dict) -> GraphGuardDecision:
    """Goal 模式护栏：优化可改合成/非 QC 链路，但不得替换输入数据或放宽 QC 标尺。"""
    before_nodes = _nodes_by_id(before)
    after_nodes = _nodes_by_id(after)
    for node_id, old in before_nodes.items():
        if old.get("type") == "input":
            new = after_nodes.get(node_id)
            if new is None or new.get("type") != "input":
                return GraphGuardDecision(False, f"输入节点 {node_id} 被删除或改型")
            if _input_dataset_ids(old) != _input_dataset_ids(new):
                return GraphGuardDecision(False, f"输入数据集在节点 {node_id} 被修改")
        if old.get("type") == "qc":
            new = after_nodes.get(node_id)
            if new is None or new.get("type") != "qc":
                return GraphGuardDecision(False, f"QC 节点 {node_id} 被删除或改型")
            if _qc_config(old) != _qc_config(new):
                return GraphGuardDecision(False, f"QC 节点 {node_id} 配置被修改")
    for node_id, new in after_nodes.items():
        # 新增 input/qc 测量节点会改写聚合首轮通过率（刷分），优化必须落在既有链路上
        if node_id not in before_nodes and new.get("type") in ("input", "qc"):
            return GraphGuardDecision(False, f"目标模式不得新增测量节点 {node_id}（{new.get('type')}）")
    return GraphGuardDecision(True, "")
