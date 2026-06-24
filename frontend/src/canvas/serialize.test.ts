import { describe, expect, it } from 'vitest'
import { fromFlow, toFlow, displayName } from './serialize'
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

describe('node label', () => {
  it('label 经 toFlow→fromFlow 往返保留', () => {
    const g = { nodes: [{ id: 'llm_synth_1', type: 'llm_synth', position: { x: 1, y: 2 }, config: {}, label: '翻译' }], edges: [] }
    const f = toFlow(g as any)
    expect((f.nodes[0].data as any).label).toBe('翻译')
    const back = fromFlow(f.nodes, f.edges)
    expect((back.nodes[0] as any).label).toBe('翻译')
  })
  it('无 label 时 fromFlow 不写 label 键（不污染指纹）', () => {
    const g = { nodes: [{ id: 'input_1', type: 'input', position: { x: 0, y: 0 }, config: {} }], edges: [] }
    const back = fromFlow(toFlow(g as any).nodes, [])
    expect('label' in back.nodes[0]).toBe(false)
  })
  it('displayName: 有 label 用 label，否则用 id', () => {
    expect(displayName('翻译', 'llm_synth_1')).toBe('翻译')
    expect(displayName('', 'llm_synth_1')).toBe('llm_synth_1')
    expect(displayName(undefined, 'input_1')).toBe('input_1')
  })
})
