export interface UserInfo {
  id: number; username: string; display_name: string
  is_admin: boolean; acting_as: string | null; real_username: string
}
export interface AdminUser {
  id: number; username: string; display_name: string; is_admin: boolean; created_at: string
}

export interface ModelConfig {
  id: number; name: string; model_name: string; base_url: string
  provider: 'openai' | 'azure'; azure_api_mode: 'legacy' | 'v1'; api_version: string
  api_key_set: boolean; default_params: Record<string, unknown>
}

export interface Dataset {
  id: number; name: string; source: string; original_filename: string
  row_count: number; columns: string[]; created_at: string
}

export interface WorkflowSummary { id: number; name: string; updated_at: string }

export interface GraphNode {
  id: string; type: 'input' | 'llm_synth' | 'auto_process' | 'output' | 'qc' | 'http_fetch'
  position: { x: number; y: number }; config: Record<string, any>
}
export interface GraphEdge { source: string; target: string; kind: 'normal' | 'rescan' }
export interface WorkflowGraph { nodes: GraphNode[]; edges: GraphEdge[] }
export interface Workflow { id: number; name: string; graph: WorkflowGraph; updated_at: string }

export interface NodeState { node_id: string; status: string; total: number; done: number; failed: number }
export interface Run {
  id: number; workflow_id: number; workflow_name: string; status: string; error: string
  stats: { prompt_tokens?: number; completion_tokens?: number }
  created_at: string; finished_at: string | null
}
export interface RunDetail extends Run { graph: WorkflowGraph; node_states: NodeState[] }
export interface RowsPage { total: number; rows: Record<string, any>[] }

export interface AgentToolContent {
  tool: string; args_brief: string; agent_role: string
  status?: 'ok' | 'error' | 'running'; output_brief?: string
}
export interface AgentMessageOut {
  id: number; role: 'user' | 'assistant' | 'tool'
  content: { text?: string } & Partial<AgentToolContent>
  created_at: string
}
export interface AgentSessionSummary {
  id: number; title: string; status: string
  models: Record<string, number>; model_params: Record<string, Record<string, unknown>>
  created_at: string; updated_at: string
}
export interface AgentSessionDetail extends AgentSessionSummary { messages: AgentMessageOut[] }

export interface CodegenOut {
  code: string
  output_columns: string[]
  columns: string[]
  sample_source: 'computed' | 'latest_run' | 'dataset' | 'none'
}

export interface NodeAssistReply {
  reply: string
  config: Record<string, any> | null
  sample_source: 'computed' | 'latest_run' | 'dataset' | 'none'
}

export interface ModelLogEntry {
  id: number; source: string; node_id: string; run_id: number | null
  workflow_id: number | null; session_id: number | null
  model_name: string; provider: string
  request: { role: string; content: string }[] | unknown
  response: string; prompt_tokens: number; completion_tokens: number; created_at: string
}

export interface ImportReport {
  models_reused: { name: string; id: number }[]
  models_created: { name: string; id: number }[]
  models_need_key: { name: string; id: number }[]
  prompts_reused: { name: string; id: number }[]
  prompts_created: { name: string; id: number }[]
  datasets_reused: { name: string; id: number }[]
  datasets_created: { name: string; id: number }[]
  secrets_need_refill: { node_id: string | null; field: string }[]
  draft_unresolved: { node_id: string; kind: string; old_id: number }[]
}
export interface ImportResult { workflow: { id: number; name: string }; report: ImportReport }

export type ColumnsMap = Record<string, { input: string[]; output: string[] }>

export interface RunLogEntry { created_at: string; node_id: string; level: string; message: string }

export interface QcFailureEntry { node_id: string; sample: Record<string, any>; reasons: { model_config_id: number; status: string; reason: string }[]; created_at: string }

export interface PromptSummary {
  id: number; name: string; description: string; latest_version: number; variables: string[]
}
export interface PromptVersionMeta { version: number; created_at: string }
export interface PromptUsage { workflow_id: number; workflow_name: string; node_id: string; slot: string }
export interface PromptDetail {
  id: number; name: string; description: string
  current: { version: number; body: string; variables: string[] }
  versions: PromptVersionMeta[]
  used_by: PromptUsage[]
}
