"""目标模式标记协议：Agent 回合末尾的 CONTINUE/DONE 标记解析与剥离。"""
import re

GOAL_MARKER_RE = re.compile(r"<!--\s*REDLOTUS_GOAL\s*:\s*(CONTINUE|DONE)\s*-->", re.IGNORECASE)


def parse_goal(text: str) -> tuple[str | None, str]:
    """返回 (信号, 剥离标记后的文本)。信号为 "CONTINUE"/"DONE"，无标记为 None。"""
    matches = GOAL_MARKER_RE.findall(text)
    cleaned = GOAL_MARKER_RE.sub("", text).strip()
    return (matches[-1].upper() if matches else None), cleaned
