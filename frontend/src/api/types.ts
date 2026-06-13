export interface UserInfo { id: number; username: string; display_name: string }

export interface ModelConfig {
  id: number; name: string; model_name: string; base_url: string
  api_key_set: boolean; default_params: Record<string, unknown>
}

export interface Dataset {
  id: number; name: string; source: string; original_filename: string
  row_count: number; columns: string[]; created_at: string
}

export interface WorkflowSummary { id: number; name: string; updated_at: string }

export interface GraphNode {
  id: string; type: 'input' | 'llm_synth' | 'auto_process' | 'output' | 'qc'
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
  preview_rows: Record<string, unknown>[] | null
  sample_source: 'last_run' | 'dataset' | 'none'
  error: string | null
}

export interface NodeAssistOut {
  config: Record<string, any>
  sample_source: 'last_run' | 'dataset' | 'none'
}
