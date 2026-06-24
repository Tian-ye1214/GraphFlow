import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
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

// Polyfill ResizeObserver for jsdom
if (typeof window !== 'undefined' && !window.ResizeObserver) {
  window.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
}

// Mock the api client
vi.mock('../api/client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    del: vi.fn(),
  },
}))

// Mock useEvents to be a no-op
vi.mock('../api/events', () => ({
  useEvents: vi.fn(),
}))

const mockPromptList = [
  { id: 1, name: '测试提示词', description: '描述', latest_version: 2, variables: ['name'] },
]

const mockPromptDetail = {
  id: 1,
  name: '测试提示词',
  description: '描述',
  current: { version: 2, body: '你好 {{name}}', variables: ['name'] },
  versions: [
    { version: 1, created_at: '2026-01-01T00:00:00' },
    { version: 2, created_at: '2026-01-02T00:00:00' },
  ],
  used_by: [],
}

describe('PromptsPage DOM render', () => {
  beforeEach(async () => {
    const { api } = await import('../api/client')
    const mockGet = api.get as ReturnType<typeof vi.fn>
    mockGet.mockImplementation((path: string) => {
      if (path === '/api/prompts') return Promise.resolve(mockPromptList)
      if (path === '/api/prompts/1') return Promise.resolve(mockPromptDetail)
      return Promise.resolve([])
    })
  })

  it('renders detail panel with Collapse headers 版本历史 and 被引用（0）', async () => {
    const { default: PromptsPage } = await import('./PromptsPage')
    render(<PromptsPage />)

    // Wait for list to load
    await waitFor(() => screen.getByText('测试提示词'))

    // Click the prompt to open detail
    fireEvent.click(screen.getByText('测试提示词'))

    // Wait for detail to load
    await waitFor(() => screen.getByText('复制为新提示词'))

    // Assert save button exists
    expect(screen.getByText('保存（新版本）')).toBeTruthy()
    expect(screen.getByText('复制为新提示词')).toBeTruthy()

    // Assert Collapse panel label '版本历史' exists
    await waitFor(() => screen.getByText('版本历史'))
    expect(screen.getByText('版本历史')).toBeTruthy()

    // Assert Collapse panel label '被引用（0）' exists (NEW format, not in old code)
    await waitFor(() => screen.getByText('被引用（0）'))
    expect(screen.getByText('被引用（0）')).toBeTruthy()
  })
})
