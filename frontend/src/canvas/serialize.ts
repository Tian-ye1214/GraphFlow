import type { Edge, Node } from '@xyflow/react'
import type { GraphEdge, GraphNode, WorkflowGraph } from '../api/types'

export const NODE_LABELS: Record<GraphNode['type'], string> = {
  input: '输入',
  llm_synth: 'LLM 合成',
  auto_process: '自动处理',
  output: '输出',
  qc: '质检',
  http_fetch: 'HTTP 取数',
}

// 回扫边的视觉样式（橙色虚线 + 流动），CanvasPage 新建边与 toFlow 加载边共用
export const RESCAN_EDGE = { animated: true, style: { stroke: '#fa8c16', strokeDasharray: '6 3' } }

export function displayName(label: string | undefined, id: string): string {
  return (label && label.trim()) ? label : id
}

export const PROMPT_KEYS = ['system_prompt', 'user_prompt', 'system_prompt_ref', 'user_prompt_ref'] as const

// 深拷贝 config 并删掉提示词字段（含库引用）。非提示词节点没这些键 → 等价于原样深拷贝。
export function stripPrompt(config: Record<string, any>): Record<string, any> {
  const c = JSON.parse(JSON.stringify(config ?? {}))
  for (const k of PROMPT_KEYS) delete c[k]
  return c
}

// 副本显示名：剥掉结尾的 _<数字> 得词干，返回最小未占用的「词干_n」(n≥2)。
export function copyLabel(base: string, existing: Set<string>): string {
  const stem = base.replace(/_\d+$/, '')
  for (let n = 2; ; n++) {
    const name = `${stem}_${n}`
    if (!existing.has(name)) return name
  }
}

export function toFlow(graph: WorkflowGraph): { nodes: Node[]; edges: Edge[] } {
  return {
    nodes: graph.nodes.map((n) => ({
      id: n.id, type: n.type, position: n.position, data: { config: n.config, label: n.label },
    })),
    edges: graph.edges.map((e, i) => ({
      id: `e${i}_${e.source}_${e.target}`, source: e.source, target: e.target,
      sourceHandle: e.kind === 'rescan' ? 'rescan' : undefined,
      data: { kind: e.kind ?? 'normal' },
      ...(e.kind === 'rescan' ? RESCAN_EDGE : {}),
    })),
  }
}

export function fromFlow(nodes: Node[], edges: Edge[]): WorkflowGraph {
  return {
    nodes: nodes.map((n) => {
      const label = (n.data as { label?: string })?.label
      return {
        id: n.id,
        type: n.type as GraphNode['type'],
        position: { x: n.position.x, y: n.position.y },
        config: ((n.data as { config?: Record<string, any> })?.config) ?? {},
        ...(label && label.trim() ? { label } : {}),
      }
    }),
    edges: edges.map((e) => ({
      source: e.source, target: e.target,
      kind: (((e.data as { kind?: GraphEdge['kind'] })?.kind) ?? 'normal') as GraphEdge['kind'],
    })),
  }
}
