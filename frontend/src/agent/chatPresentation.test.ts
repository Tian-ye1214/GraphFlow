import { describe, expect, it } from 'vitest'
import { roleLabel, ROLE_BG } from './chatPresentation'

describe('chatPresentation', () => {
  it('roleLabel 映射', () => {
    expect(roleLabel('user')).toBe('你')
    expect(roleLabel('assistant')).toBe('助手')
    expect(roleLabel('tool')).toBe('工具')
  })
  it('ROLE_BG 蓝绿', () => {
    expect(ROLE_BG.user).toBe('#e6f4ff')
    expect(ROLE_BG.assistant).toBe('#f6ffed')
  })
})
