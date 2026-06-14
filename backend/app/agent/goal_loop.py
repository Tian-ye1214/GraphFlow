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
