import { describe, expect, it } from 'vitest'
import { fromFlow, toFlow, displayName, stripPrompt, copyLabel } from './serialize'
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

describe('stripPrompt', () => {
  it('删掉 4 个提示词键、保留其余键', () => {
    const out = stripPrompt({
      model_config_id: 7, output_column: 'q_en', params: { temperature: 0 },
      system_prompt: 'sys', user_prompt: 'Q:{{q}}', system_prompt_ref: 3, user_prompt_ref: 4,
    })
    expect(out).toEqual({ model_config_id: 7, output_column: 'q_en', params: { temperature: 0 } })
  })
  it('深拷贝隔离：改返回对象不影响入参', () => {
    const src = { params: { temperature: 0 }, user_prompt: 'x' }
    const out = stripPrompt(src)
    out.params.temperature = 1
    expect(src.params.temperature).toBe(0)
  })
  it('没有提示词键的 config 原样返回（值相等、对象不同引用）', () => {
    const src = { dataset_ids: [1, 2] }
    const out = stripPrompt(src)
    expect(out).toEqual({ dataset_ids: [1, 2] })
    expect(out).not.toBe(src)
  })
})

describe('copyLabel', () => {
  it('原名未带后缀 → _2', () => {
    expect(copyLabel('翻译', new Set(['翻译']))).toBe('翻译_2')
  })
  it('_2 已占用 → _3', () => {
    expect(copyLabel('翻译', new Set(['翻译', '翻译_2']))).toBe('翻译_3')
  })
  it('复制已带后缀的名：剥词干再自增（翻译_2 → 翻译_3，而非 翻译_2_2）', () => {
    expect(copyLabel('翻译_2', new Set(['翻译', '翻译_2']))).toBe('翻译_3')
  })
})
