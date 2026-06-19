"""智能处理操作的代码生成：临时单 Agent（零工具、请求级生命周期）+ 仅采上游列名（不跑数、不预览）。
节点助手为多轮：前端每轮带该节点 history，后端无状态跑一轮。"""
import json

from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from app.agent.factory import create_agent
from app.agent.prompts import load_prompt
from app.engine.columns import propagate_columns, resolve_dataset_cols
from app.engine.graph import parse_graph
from app.models import Workflow

INSTRUCTIONS = load_prompt("codegen_system.md")


def strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _user_prompt(instruction: str, columns: list[str]) -> str:
    cols = "、".join(columns) if columns else "（未知，按指令中提到的列名处理）"
    return load_prompt("codegen_user.md").format(instruction=instruction, columns=cols)


async def generate_code(model, instruction: str, columns: list[str], current_code: str = "",
                        preview_tools: list | None = None, params: dict | None = None) -> dict:
    """按指令+上游列名生成 {code, output_columns}；不执行、不预览。
    传入 current_code 时要求模型在其基础上增量修改、保留已有处理。"""
    agent = create_agent(model, preview_tools or [], INSTRUCTIONS, params=params)
    prompt = _user_prompt(instruction, columns)
    if current_code.strip():
        prompt += ("\n\n现有代码（请在此基础上增量修改，保留已有处理逻辑，"
                   "不要丢失之前的转换）：\n" + current_code)
    try:
        result = await agent.run(prompt)
    except UnexpectedModelBehavior as e:   # 空/不可用补全(截断/限流/内容过滤)重试耗尽 → 可读 ValueError(路由→422)而非 500
        raise ValueError("模型未返回有效内容，请重试") from e
    raw = str(result.output or "")
    try:   # 模型偶发返回非 JSON（散文/空/澄清语）；codegen 必须产出代码，转可读 ValueError（路由→422）而非 500
        data = json.loads(strip_code_fences(raw))
    except json.JSONDecodeError as e:
        raise ValueError(f"模型未返回有效的代码 JSON，请重试或调整指令：{raw[:200]}") from e
    if not isinstance(data, dict):   # 合法 JSON 但非对象(数组/标量)：data.get 会 AttributeError→500
        raise ValueError(f"模型未返回有效的代码 JSON（应为对象），请重试：{raw[:200]}")
    return {"code": data.get("code", ""), "output_columns": data.get("output_columns", [])}


NODE_ASSIST_INSTRUCTIONS = {
    "llm_synth": load_prompt("node_assist_llm_synth.md"),
    "qc": load_prompt("node_assist_qc.md"),
}


def _to_history(history: list[dict] | None) -> list:
    msgs = []
    for h in history or []:
        if h.get("role") == "user":
            msgs.append(ModelRequest(parts=[UserPromptPart(content=h.get("text", ""))]))
        else:
            msgs.append(ModelResponse(parts=[TextPart(content=h.get("text", ""))]))
    return msgs


async def generate_node_config(model, node_type: str, instruction: str, columns: list[str],
                               current_config: dict | None = None,
                               preview_tools: list | None = None,
                               params: dict | None = None,
                               history: list[dict] | None = None) -> dict:
    """多轮：带该节点 history 跑一轮，返回 {reply, config}。config 为 None 表示本轮只对话不产配置。
    未知 node_type 抛 KeyError。传入 current_config 时要求模型在其基础上增量修改。"""
    agent = create_agent(model, preview_tools or [], NODE_ASSIST_INSTRUCTIONS[node_type], params=params)
    prompt = _user_prompt(instruction, columns)
    if current_config:
        prompt += ("\n\n现有节点配置（请在此基础上增量修改，保留已有提示词中的处理，"
                   "不要丢失之前的需求）：\n" + json.dumps(current_config, ensure_ascii=False))
    try:
        result = await agent.run(prompt, message_history=_to_history(history))
    except UnexpectedModelBehavior:   # 空/不可用补全：多轮对话降级为本轮不产配置（config=None），不崩 500
        return {"reply": "（模型未返回有效内容，请重试）", "config": None}
    raw = str(result.output or "")
    data = None
    try:
        data = json.loads(strip_code_fences(raw))
    except json.JSONDecodeError:
        pass
    # 非 JSON 或合法 JSON 但非对象(数组/标量) → 当纯对话回复(本轮不产配置)，不 AttributeError→500
    if not isinstance(data, dict):
        return {"reply": raw.strip() or "（模型未返回有效配置，请重述需求）", "config": None}
    return {"reply": data.get("reply", ""), "config": data.get("config")}


async def gather_upstream_columns(s, workflow_id: int, node_id: str, user_id: int):
    """静态推算 node_id 的输入列（沿 llm/处理节点传播）。返回 (columns, source)。"""
    wf = await s.get(Workflow, workflow_id)
    if wf is None or wf.user_id != user_id:
        return [], "none"
    graph = parse_graph(wf.graph_json)
    if node_id not in {n.id for n in graph.nodes}:
        return [], "none"
    dataset_cols = await resolve_dataset_cols(s, graph, user_id)
    cols = propagate_columns(graph, dataset_cols)[node_id]["input"]
    return cols, ("computed" if cols else "none")
