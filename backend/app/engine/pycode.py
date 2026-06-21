"""在子进程中执行智能处理代码：进程隔离（死循环拖不垮事件循环）+ 超时杀进程树。"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from app.agent.subproc import run_subprocess

HARNESS = Path(__file__).resolve().parent / "pycode_harness.py"
CODE_TIMEOUT = 120

# 子进程跑用户/agent 代码：env 只给启动 python + 编码所需的最小项，绝不透传 GRAPHFLOW_*/密钥/cookie 等
# 凭证类环境变量给被执行代码（防被执行代码读 os.environ 偷密钥——dry_run 会在更少审阅的草稿上跑此路径）。
_SAFE_ENV_KEYS = ("PATH", "Path", "SYSTEMROOT", "SystemRoot", "TEMP", "TMP", "TMPDIR", "HOME",
                  "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS", "LANG", "LC_ALL", "LD_LIBRARY_PATH")


def _safe_env() -> dict:
    env = {k: os.environ[k] for k in _SAFE_ENV_KEYS if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


async def run_process_code(code: str, rows: list[dict]) -> list[dict]:
    if not code.strip():
        raise ValueError("智能处理操作未生成代码")
    with tempfile.TemporaryDirectory() as td:
        code_p, in_p, out_p = Path(td) / "code.py", Path(td) / "in.json", Path(td) / "out.json"
        code_p.write_text(code, encoding="utf-8")
        in_p.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        try:
            _out, err, rc = await run_subprocess(
                [sys.executable, str(HARNESS), str(code_p), str(in_p), str(out_p)],
                shell=False, cwd=td, env=_safe_env(),
                timeout=CODE_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise ValueError(f"智能处理代码执行超时（{CODE_TIMEOUT} 秒）")
        if rc != 0:
            raise ValueError(f"智能处理代码执行失败:\n{err[-2000:]}")
        if not out_p.exists():   # rc=0 但未产出（用户代码 sys.exit/os._exit 提前退出）：清晰报错，不裸 FileNotFoundError 泄漏临时路径
            raise ValueError("智能处理代码未产出结果（疑似提前退出，如调用了 sys.exit/os._exit，或未正常返回）")
        return json.loads(out_p.read_text(encoding="utf-8"))
