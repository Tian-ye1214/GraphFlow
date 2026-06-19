import { describe, expect, it } from 'vitest'
import { extractVars, buildPromptPayload } from './PromptsPage'

describe('PromptsPage helpers', () => {
  it('extracts unique sorted variables', () => {
    expect(extractVars('你好 {{name}} 与 {{name}} 和 {{age}}')).toEqual(['age', 'name'])
    expect(extractVars('无占位')).toEqual([])
  })

  it('builds payload trimming name', () => {
    expect(buildPromptPayload({ name: '  P  ', description: 'd', body: 'x' }))
      .toEqual({ name: 'P', description: 'd', body: 'x' })
  })
})
