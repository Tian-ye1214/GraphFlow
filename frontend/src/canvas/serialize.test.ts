import { describe, expect, it } from 'vitest'
import { fromFlow, toFlow } from './serialize'
import type { WorkflowGraph } from '../api/types'

const GRAPH: WorkflowGraph = {
  nodes: [
    { id: 'in_1', type: 'input', position: { x: 0, y: 0 }, config: { dataset_ids: [1] } },
    { id: 'gen_1', type: 'llm_synth', position: { x: 200, y: 0 }, config: { user_prompt: 'Q:{{q}}' } },
  ],
  edges: [{ source: 'in_1', target: 'gen_1', kind: 'normal' }],
}

describe('serialize', () => {
  it('graph → flow → graph 往返一致', () => {
    const f = toFlow(GRAPH)
    expect(fromFlow(f.nodes, f.edges)).toEqual(GRAPH)
  })

  it('flow 边缺少 kind 时默认 normal', () => {
    const g = fromFlow(
      [{ id: 'a', type: 'input', position: { x: 0, y: 0 }, data: { config: {} } }],
      [{ id: 'e1', source: 'a', target: 'a' }],
    )
    expect(g.edges[0].kind).toBe('normal')
  })
})
