from pathlib import Path


def resolve_in(base: Path, name: str) -> Path:
    """把 name 解析到 base 目录内；绝对路径或 .. 逃逸则抛 ValueError。"""
    target = (base / name).resolve()
    if target != base.resolve() and not target.is_relative_to(base.resolve()):
        raise ValueError(f"路径越界: {name}")
    return target
