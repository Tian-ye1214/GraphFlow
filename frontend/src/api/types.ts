export interface UserInfo {
  id: number; username: string; display_name: string
  is_admin: boolean; acting_as: string | null; real_username: string
}
export interface AdminUser {
  id: number; username: string; display_name: string; is_admin: boolean; created_at: string
}

export interface ModelConfig {
  id: number; name: string; model_name: string; base_url: string
  provider: 'openai' | 'azure'; api_version: string
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
  models: Record<string, number>; created_at: string; updated_at: string
}
export interface AgentSessionDetail extends AgentSessionSummary { messages: AgentMessageOut[] }

export interface CodegenOut {
  code: string
  output_columns: string[]
  columns: string[]
  sample_source: 'computed' | 'none'
}

export interface NodeAssistOut {
  config: Record<string, any>
  sample_source: 'computed' | 'none'
}

export type ColumnsMap = Record<string, { input: string[]; output: string[] }>

export interface RunLogEntry { created_at: string; node_id: string; level: string; message: string }

export interface QcMetricEntry { node_id: string; total: number; first_round_pass: number; first_round_rate: number }
export interface QcFailureEntry { node_id: string; sample: Record<string, any>; reasons: { model_config_id: number; pass: boolean; reason: string }[]; created_at: string }
