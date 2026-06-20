import '@testing-library/jest-dom/vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import RunDetailPage from './RunDetailPage'

// SSE 订阅在 jsdom 无 EventSource，置空避免副作用
vi.mock('../api/events', () => ({ useEvents: () => {} }))

class ResizeObserverStub { observe() {} unobserve() {} disconnect() {} }
vi.stubGlobal('ResizeObserver', ResizeObserverStub)
vi.stubGlobal('matchMedia', (q: string) => ({
  matches: false, media: q, onchange: null,
  addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false },
}))

const RUNNING_RUN = {
  id: 1, workflow_id: 1, workflow_name: 'wf', status: 'running', error: null,
  stats: { prompt_tokens: 0, completion_tokens: 0 },
  created_at: '2026-06-20T00:00:00+00:00', finished_at: null,
  graph: { nodes: [{ id: 'llm', type: 'llm_synth', config: {} }, { id: 'out', type: 'output', config: {} }], edges: [] },
  node_states: [{ node_id: 'llm', status: 'running', total: 3, done: 1, failed: 0 }],
}

function mockFetch() {
  const calls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    calls.push(path)
    const j = (x: unknown) => new Response(JSON.stringify(x), { status: 200 })
    if (/\/api\/runs\/1\/logs/.test(path)) return j([])
    if (/\/api\/runs\/1\/rows\?.*status=failed/.test(path)) return j({ total: 0, rows: [] })
    if (/\/api\/runs\/1\/rows/.test(path)) return j({ total: 2, rows: [{ id: 1, out: 'partial' }] })
    if (/\/api\/runs\/1\/qc-failures/.test(path)) return j([])
    if (/\/api\/runs\/1\/model-logs/.test(path)) return j([{ id: 9, source: 'synth', node_id: 'llm', model_name: 'm', request: [], response: 'r', prompt_tokens: 1, completion_tokens: 1, created_at: 't' }])
    if (/\/api\/runs\/1$/.test(path)) return j(RUNNING_RUN)
    return j({})
  }))
  return calls
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/runs/1']}>
      <Routes><Route path="/runs/:id" element={<RunDetailPage />} /></Routes>
    </MemoryRouter>,
  )
}

afterEach(() => { vi.restoreAllMocks() })

describe('RunDetailPage 运行期可观测性', () => {
  it('运行中即渲染结果/失败行/模型对话三个 Tab（不再等终态）', async () => {
    mockFetch()
    renderPage()
    expect(await screen.findByText(/结果预览/)).toBeInTheDocument()
    expect(screen.getByText(/失败行/)).toBeInTheDocument()
    expect(screen.getByText(/模型对话/)).toBeInTheDocument()
    // 运行中给出实时刷新提示
    expect(screen.getByText(/运行中 · 实时刷新/)).toBeInTheDocument()
  })

  it('运行中即拉取结果行与模型对话数据（后端对 running 无限制）', async () => {
    const calls = mockFetch()
    renderPage()
    await waitFor(() => {
      expect(calls.some((p) => /\/api\/runs\/1\/rows/.test(p))).toBe(true)
      expect(calls.some((p) => /\/api\/runs\/1\/model-logs/.test(p))).toBe(true)
    })
  })
})
