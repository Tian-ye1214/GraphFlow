import { describe, expect, it } from 'vitest'
import { fmtDuration, extractTplVars, renderCell } from './utils'

describe('fmtDuration', () => {
  it('start 缺省返回占位符', () => {
    expect(fmtDuration(null, null)).toBe('—')
  })
  it('秒级时长', () => {
    expect(fmtDuration('2024-01-01T00:00:00Z', '2024-01-01T00:00:45Z')).toBe('45秒')
  })
  it('分秒时长', () => {
    expect(fmtDuration('2024-01-01T00:00:00Z', '2024-01-01T00:02:05Z')).toBe('2分5秒')
  })
  it('end 缺省按进行中（>=0，不报错）', () => {
    const s = new Date(Date.now() - 3000).toISOString()
    expect(fmtDuration(s, null)).toMatch(/秒$/)
  })
})

describe('renderCell', () => {
  it('对象转 JSON 串', () => {
    expect(renderCell({ x: 1 })).toBe('{"x":1}')
  })
  it('null/undefined 转空串', () => {
    expect(renderCell(null)).toBe('')
  })
})

describe('extractTplVars', () => {
  it('按首次出现去重提取 {{变量}}', () => {
    expect(extractTplVars('{{a}} {{b}} {{a}}')).toEqual(['a', 'b'])
  })
})
