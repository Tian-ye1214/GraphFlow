"""目标优化循环的纯函数：跳出判定与轮次提示构造（便于单测，无 I/O）。"""
import json
from dataclasses import dataclass


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
    return (f"[目标]\n{goal_text}\n\n"
            f"[上一轮运行 #{run_id} 实测：首轮质检通过率 = {metric_str}]\n\n"
            f"[真实质检失败样本抽样（含各判定模型理由）]\n{fail_str}\n\n"
            "请先**凝练通用经验**：从这些失败样本中归纳出可推广的规律（而不是针对单条样本打补丁），"
            "再据此用 gf 命令改进当前工作流的提示词/参数（必要时调整链路）。改完即结束本回合，"
            "系统会自动跑数并把新指标喂给你。仍需继续时回复末尾输出 "
            "`<!-- REDLOTUS_GOAL:CONTINUE -->`；若判断目标不可达请输出 `<!-- REDLOTUS_GOAL:DONE -->`。")


def first_round_prompt(goal_text: str) -> str:
    return (f"[目标]\n{goal_text}\n\n"
            "这是目标优化模式第一轮。请先用 gf 查看当前工作流结构与质检节点，"
            "凝练你对如何达成目标的初步判断，再改进提示词/参数。改完结束回合，系统会自动跑数。"
            "回复末尾输出 `<!-- REDLOTUS_GOAL:CONTINUE -->`。")
