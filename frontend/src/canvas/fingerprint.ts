import type { WorkflowGraph } from '../api/types'
import { fromFlow, toFlow } from './serialize'

// 经同一序列化路径归一后 stringify：键序、字段集合一致，可用于画布判脏
export function graphFingerprint(graph: WorkflowGraph): string {
  const f = toFlow(graph)
  return JSON.stringify(fromFlow(f.nodes, f.edges))
}
