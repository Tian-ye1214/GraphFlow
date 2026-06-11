import type { Edge, Node } from '@xyflow/react'
import type { GraphEdge, GraphNode, WorkflowGraph } from '../api/types'

export const NODE_LABELS: Record<GraphNode['type'], string> = {
  input: '输入',
  llm_synth: 'LLM 合成',
  auto_process: '自动处理',
  output: '输出',
}

export function toFlow(graph: WorkflowGraph): { nodes: Node[]; edges: Edge[] } {
  return {
    nodes: graph.nodes.map((n) => ({
      id: n.id, type: n.type, position: n.position, data: { config: n.config },
    })),
    edges: graph.edges.map((e, i) => ({
      id: `e${i}_${e.source}_${e.target}`, source: e.source, target: e.target,
      data: { kind: e.kind ?? 'normal' },
    })),
  }
}

export function fromFlow(nodes: Node[], edges: Edge[]): WorkflowGraph {
  return {
    nodes: nodes.map((n) => ({
      id: n.id,
      type: n.type as GraphNode['type'],
      position: { x: n.position.x, y: n.position.y },
      config: ((n.data as { config?: Record<string, any> })?.config) ?? {},
    })),
    edges: edges.map((e) => ({
      source: e.source, target: e.target,
      kind: (((e.data as { kind?: GraphEdge['kind'] })?.kind) ?? 'normal') as GraphEdge['kind'],
    })),
  }
}
