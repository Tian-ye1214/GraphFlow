import { describe, expect, it } from 'vitest'
import { nodeDropPosition } from './layout'

describe('nodeDropPosition', () => {
  it('落在视口中心（减半宽/半高）', () => {
    expect(nodeDropPosition({ x: 500, y: 300 }, 0)).toEqual({ x: 435, y: 280 })
  })
  it('按 count 错位防重叠（每个 +24，6 循环）', () => {
    expect(nodeDropPosition({ x: 500, y: 300 }, 1)).toEqual({ x: 459, y: 304 })
    expect(nodeDropPosition({ x: 500, y: 300 }, 6)).toEqual({ x: 435, y: 280 })  // 回到 0 偏移
  })
})
