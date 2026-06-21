用户指令：{instruction}

上游可用列：{columns}

你有只读工具看真实情况，按需调用、不要让用户手动粘贴数据：`preview_current_node_input`(列+样例行)、`describe_current_node_input`(总行数/各列类型/缺失率/值分布——定 cast/dedup/filter 用)、`show_workflow_graph`(全图/上下游)、`read_node_output`(本节点上轮产出/失败行)。
返回代码前，用 `try_process_code` 把候选代码在真实输入小样本上试跑一遍，确认能跑通且产出符合预期再返回(零副作用、不落库)。
