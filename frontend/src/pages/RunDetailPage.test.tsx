import '@testing-library/jest-dom/vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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

const COMPLETED_RUN = {
  ...RUNNING_RUN,
  status: 'completed',
  started_at: '2026-06-20T00:00:00+00:00',
  finished_at: '2026-06-20T00:01:00+00:00',
  node_states: [{ node_id: 'llm', status: 'done', total: 1, done: 1, failed: 0 }],
}

function mockTraceFetch() {
  const calls: { path: string; init?: RequestInit }[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    calls.push({ path, init })
    const j = (x: unknown) => new Response(JSON.stringify(x), { status: 200 })
    if (/\/api\/runs\/1\/logs/.test(path)) return j([])
    if (/\/api\/runs\/1\/rows\?.*status=failed/.test(path)) return j({ total: 0, rows: [] })
    if (/\/api\/runs\/1\/rows/.test(path)) return j({ total: 1, rows: [{ q: 'x', a: 'bad' }] })
    if (/\/api\/runs\/1\/qc-metrics/.test(path)) return j([{ node_id: 'qc', total: 1, first_round_pass: 0, first_round_rate: 0 }])
    if (/\/api\/runs\/1\/qc-failures/.test(path)) return j([{
      node_id: 'qc',
      trace_id: 'tr-1',
      sample: { q: 'x', a: 'bad' },
      reasons: [{ model_config_id: 1, status: 'failed', reason: '事实错误' }],
      created_at: 't',
    }])
    if (/\/api\/runs\/1\/model-logs/.test(path)) return j([])
    if (/\/api\/runs\/1\/trace\/tr-1/.test(path)) return j({
      trace_id: 'tr-1',
      events: [{
        node_id: 'llm',
        node_type: 'llm_synth',
        status: 'done',
        output: [{ q: 'x', a: 'bad' }],
        qc_reasons: [],
        model_logs: [{ id: 3, source: 'synth', model_name: 'm', request: [], response: 'bad', prompt_tokens: 1, completion_tokens: 1 }],
        tokens: { prompt_tokens: 1, completion_tokens: 1 },
      }],
    })
    if (/\/api\/agent\/sessions$/.test(path)) return j([{ id: 7, title: 's', status: 'idle', models: {}, model_params: {}, created_at: 't', updated_at: 't' }])
    if (/\/api\/agent\/sessions\/7\/diagnose-run/.test(path)) return j({ ok: true })
    if (/\/api\/runs\/1$/.test(path)) return j(COMPLETED_RUN)
    return j({})
  }))
  return calls
}

describe('RunDetailPage trace diagnostics', () => {
  it('opens the trace drawer from a QC failure row', async () => {
    const calls = mockTraceFetch()
    renderPage()
    const traceButtons = await screen.findAllByRole('button', { name: 'Trace' })
    fireEvent.click(traceButtons[0])
    expect(await screen.findByText('Trace tr-1')).toBeInTheDocument()
    expect(calls.some((c) => /\/api\/runs\/1\/trace\/tr-1/.test(c.path))).toBe(true)
  })

  it('submits the current run to the latest Agent session for diagnosis', async () => {
    const calls = mockTraceFetch()
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: /红莲诊断/ }))
    await waitFor(() => {
      const call = calls.find((c) => /\/api\/agent\/sessions\/7\/diagnose-run/.test(c.path))
      expect(call).toBeTruthy()
      expect(call?.init?.body).toBe(JSON.stringify({ run_id: 1 }))
    })
  })
})
