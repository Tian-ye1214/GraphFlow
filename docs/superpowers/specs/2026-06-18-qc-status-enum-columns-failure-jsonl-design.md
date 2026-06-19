# 质检节点：枚举 status 列 + 下游可见 + 失败全量 jsonl 设计

> 批次 21。承接列血缘（批 6/18/19）、QC K-of-N（批 4/8/9）、失败保存（批 6）。

## 目标

完善质检（qc）节点三点：

1. **下游可见列**：QC 产出 status 列与 reason 列两列，下游 LLM 合成节点能像引用上游列一样 `{{qc_status}}` / `{{qc_feedback}}`。
2. **枚举 status 路由**：单模型判定返回枚举字符串 status；`status=="pass"` 进保存，其余任何值算不通过 → 回扫；通过的不回扫直接保存。聚合到整行只到二值 pass/failed。
3. **失败全量 jsonl**：把最终仍失败的样本全量导出为逐行 jsonl，每行按判定模型平铺（`model_1 / model_1_reason / …`），便于后续总结归类失败原因。

## 现状（改造起点）

- `nodes.run_qc_judge_row`：多模型 K-of-N，每模型返回 `{"pass": bool, "reason": str}`，`_truthy` 归一；返回 `(ok, reason, usage, per_model)`，`per_model[i] = {"model_config_id", "pass", "reason"}`。
- `runner._run_qc_node`：`feedback_col = cfg.get("feedback_column") or "qc_feedback"`。通过行写 `{**strip_qc_internal(row), feedback_col: row.get(feedback_col, "")}`；失败行写 `{**row, feedback_col: reason, "_qc_reason": reason, "_qc_per_model": per_model}`。有 `rescan` 边且有失败行时回扫重生→重判，最多 `max_rounds` 轮。仅持久化通过行；最终失败样本写 `QcFailure`。
- `columns._typed_output`：qc 输出 = `input ∪ [feedback_column or "qc_feedback"]`（已含一列）。
- `models.QcFailure`：`run_id, node_id, sample_json, reasons_json, created_at`。
- `routers/runs.py`：`GET /{run_id}/qc-failures` 返回 JSON 列表（sample + reasons + created_at）；前端 RunDetail「质检失败样本」卡片下载 JSON。
- 提示词 `qc_judge.md` 不存在（QC 判定提示词由用户在节点 `system_prompt`/`user_prompt` 配置）；`node_assist_qc.md` 引导助手产出 `{"pass": true|false, "reason"}` 契约。

## 全局约束

- KISS：最简实现，无防御性代码，无投机抽象。
- 租户隔离（user_id 校验）为硬红线；所有新端点必须经 `_get_owned_run` / user 过滤。
- API key / Authorization 绝不进任何日志、响应、提示词。
- 提交记录不含 "claude"、无 Co-Authored-By 尾注。
- 不 `git add` 项目设计.txt、.idea/、.codegraph/。
- 后端测试只本地，不推 origin。

## 设计

### A. 判定契约（nodes.py）

- 单模型判定契约改为 **`{"status": "<枚举字符串>", "reason": "..."}`**。
  - 通过票判定：`str(status).strip().lower() == "pass"`。
  - 空 / 缺字段样本：沿用空锚点，直接判不通过，`status="failed"`、`reason="样本内容为空"`。
  - 单模型重试耗尽：投不通过票，`status="failed"`、`reason="判定重试 N 次仍失败：…"`。
- `run_qc_judge_row` 返回 **维持 4 元组** `(ok: bool, reason: str, usage: dict, per_model: list)`：
  - `ok = (通过票数 >= pass_k)`。
  - `reason` = 各不通过模型理由用「；」拼接；全通过时为 `"通过"`。
  - `per_model[i] = {"model_config_id": int, "status": str, "reason": str}`（status 保留该模型枚举原值，供失败 jsonl 平铺）。
- 判定时不再读 `verdict["pass"]`，改读 `verdict["status"]`，缺 `status` 字段视为判定失败（重试）。通过票判定从 `_truthy(pass)` 改为 `status=="pass"`；`_truthy` 不再被引用即删除。

### B. 输出列与血缘（runner.py + columns.py）

- 新增配置项 `status_column`（默认 `"qc_status"`）；沿用 `feedback_column`（默认 `"qc_feedback"`）。
- `_run_qc_node` 的 `judge_all` 写行时**显式覆写两列**，杜绝回扫残留旧值：
  - 通过行：`{**strip_qc_internal(row), status_col: "pass", feedback_col: ""}`。
  - 失败行：`{**row, status_col: "failed", feedback_col: reason, "_qc_reason": reason, "_qc_per_model": per_model}`。
- `strip_qc_internal` 不变（只剔 `_qc_reason` / `_qc_per_model`，两列保留下游可见）。
- `columns._typed_output` 的 qc 分支改为 `input ∪ [status_column or "qc_status", feedback_column or "qc_feedback"]`。

### C. 回扫路由（runner.py，基本不动）

- 仍按「有 `rescan` 边 + 有失败行」触发，失败行带列回扫重生→重判，最多 `max_rounds` 轮。
- 失败行已带 `{{qc_status}}` / `{{qc_feedback}}`，回扫目标 LLM 节点提示词可显式引用；保留现有 `_qc_reason` 零配置自动追加（不破坏老配置）。

### D. 失败全量 jsonl（models.py + runner.py + runs.py）

- `QcFailure` 表沿用。`reasons_json` 落库存 `_qc_per_model` 原样（按判定模型顺序的 `[{"model_config_id", "status", "reason"}, …]`）；导出时只取每项的 `status` / `reason`。
- 新增端点 **`GET /api/runs/{run_id}/qc-failures.jsonl`**（`media_type="application/x-ndjson"`，逐行一个 JSON）：
  - 每行 = `{...样本字段, "model_1": s1, "model_1_reason": r1, "model_2": s2, "model_2_reason": r2, …}`，序号 1-based 按 reasons_json 顺序。
  - 经 `_get_owned_run` 租户隔离；可选 `node_id` 过滤。
- 范围 = 最终仍失败样本（沿用现状，不存每轮审计轨迹）。

### E. 前端

- `NodeConfigForm` qc 表单「高级」分组：在「反馈列名」旁加 **「状态列名」** 输入（绑定 `status_column`，占位 `qc_status`）。
- 两列随血缘自动出现在「输出列」芯片（无需额外改动，血缘已含）。
- `RunDetailPage`「质检失败样本」卡片：下载按钮改为命中 `/api/runs/{id}/qc-failures.jsonl`（`window.open` 或 fetch→blob 下载 `.jsonl`），替换原 JSON 下载。

### F. 测试（TDD）

后端：
- `run_qc_judge_row`：status 归一（"PASS"/" pass "→通过、"failed"/"factual_error"→不通过）、K-of-N 通过票统计、per_model 含 status 枚举原值、空样本判 failed。
- `_run_qc_node`：通过行两列 = pass/空、失败行两列 = failed/reason；回扫后通过行两列被覆写为 pass/空（无旧值残留）。
- `columns`：qc 输出血缘含 `qc_status` + `qc_feedback`（及改名后的自定义列名）。
- jsonl 导出：平铺 `model_1/model_1_reason/…` 顺序正确、逐行合法 JSON、租户隔离（他人 run 404）。
- 改既有 qc 相关测试：判定契约 `pass`→`status`、断言新增 status 列。

前端：
- QC 面板「状态列名」字段渲染与绑定。
- jsonl 下载按钮命中正确端点。

### G. 非目标（YAGNI）

- 不做「可配置通过状态集合」（唯一通过值 = `pass`）。
- 不做枚举值固定校验（status 自由字符串，靠提示词引导）。
- 不存每轮失败审计轨迹（只存最终失败样本）。
- 整行/聚合层不做多值枚举（只 pass/failed）；枚举细分只在失败 jsonl 的 per-model 体现。
