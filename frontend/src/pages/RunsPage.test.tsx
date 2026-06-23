import '@testing-library/jest-dom/vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import RunsPage from './RunsPage'

vi.mock('../api/events', () => ({ useEvents: () => {} }))

class ResizeObserverStub { observe() {} unobserve() {} disconnect() {} }
vi.stubGlobal('ResizeObserver', ResizeObserverStub)
vi.stubGlobal('matchMedia', (q: string) => ({
  matches: false, media: q, onchange: null,
  addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false },
}))

afterEach(() => { vi.restoreAllMocks() })

describe('RunsPage QC summary', () => {
  it('renders first-round QC pass rate and empty state', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify([
      {
        id: 2, workflow_id: 1, workflow_name: 'wf', status: 'completed', error: '',
        stats: {}, qc_summary: { total: 3, first_round_pass: 2, first_round_rate: 2 / 3 },
        created_at: 't', started_at: null, finished_at: null,
      },
      {
        id: 1, workflow_id: 1, workflow_name: 'wf', status: 'completed', error: '',
        stats: {}, qc_summary: { total: 0, first_round_pass: 0, first_round_rate: null },
        created_at: 't', started_at: null, finished_at: null,
      },
    ]), { status: 200 })))
    render(<MemoryRouter><RunsPage /></MemoryRouter>)
    expect(await screen.findByText('67%（2/3）')).toBeInTheDocument()
    expect(screen.getByText('无质检指标')).toBeInTheDocument()
  })
})
