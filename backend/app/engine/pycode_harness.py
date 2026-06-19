"""智能处理代码执行壳（子进程内运行）：argv = 代码文件 输入JSON 输出JSON。
用户代码必须定义 process(rows: list[dict]) -> list[dict]。结果写文件而非 stdout，
用户代码里的 print 不会污染结果通道。"""
import json
import sys
import traceback


def main() -> int:
    code_path, in_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    with open(code_path, encoding="utf-8") as f:
        code = f.read()
    with open(in_path, encoding="utf-8") as f:
        rows = json.load(f)
    ns: dict = {}
    try:
        exec(compile(code, "agent_code.py", "exec"), ns)
        fn = ns.get("process")
        if not callable(fn):
            raise ValueError("代码未定义 process(rows) 函数")
        out = fn(rows)
        if not isinstance(out, list) or not all(isinstance(r, dict) for r in out):
            raise ValueError("process 必须返回 list[dict]")
    except Exception:
        traceback.print_exc()
        return 1
    with open(out_path, "w", encoding="utf-8") as f:
        # allow_nan=False：用户代码返回 NaN/Infinity（非法 JSON token）时此处即失败(rc!=0)，
        # 而非写出非标准 token 落库、等读行端点 Starlette(allow_nan=False) 渲染时 500。
        json.dump(out, f, ensure_ascii=False, allow_nan=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
