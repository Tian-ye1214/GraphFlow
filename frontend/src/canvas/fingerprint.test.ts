import { expect, test } from 'vitest'
import { graphFingerprint } from './fingerprint'
import type { WorkflowGraph } from '../api/types'

const graph: WorkflowGraph = {
  nodes: [{ id: 'in', type: 'input', position: { x: 1, y: 2 }, config: { dataset_ids: [1] } }],
  edges: [{ source: 'in', target: 'out', kind: 'normal' }],
}

test('同一图指纹一致（与对象键序无关）', () => {
  const reordered = {
    edges: [{ kind: 'normal', target: 'out', source: 'in' }],
    nodes: [{ config: { dataset_ids: [1] }, position: { x: 1, y: 2 }, type: 'input', id: 'in' }],
  } as WorkflowGraph
  expect(graphFingerprint(reordered)).toBe(graphFingerprint(graph))
})

test('位置变化改变指纹', () => {
  const moved = JSON.parse(JSON.stringify(graph)) as WorkflowGraph
  moved.nodes[0].position.x = 99
  expect(graphFingerprint(moved)).not.toBe(graphFingerprint(graph))
})
