import '@testing-library/jest-dom/vitest'
import { act, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import DatasetsPage from './DatasetsPage'

vi.mock('../api/events', () => ({ useEvents: () => {} }))

class ResizeObserverStub { observe() {} unobserve() {} disconnect() {} }
vi.stubGlobal('ResizeObserver', ResizeObserverStub)
vi.stubGlobal('matchMedia', (q: string) => ({
  matches: false, media: q, onchange: null,
  addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false },
}))

afterEach(() => { vi.restoreAllMocks() })

// ── helpers ────────────────────────────────────────────────────────────────

function makeDataset(overrides: Partial<{
  id: number; name: string; status: string; imported_rows: number; columns: string[]
}> = {}) {
  return {
    id: 1,
    name: 'ds1',
    source: 'upload',
    original_filename: 'ds1.csv',
    row_count: 10,
    columns: ['col_a', 'col_b'],
    created_at: '2026-01-01T00:00:00Z',
    status: 'ready',
    imported_rows: 0,
    import_error: '',
    total_rows_including_header: 11,
    ...overrides,
  }
}

function renderPage() {
  return render(<MemoryRouter><DatasetsPage /></MemoryRouter>)
}

// ── L17: stale preview fetch guard ─────────────────────────────────────────

describe('L17 — stale preview fetch guard', () => {
  it('ignores a slow response from a previous dataset when preview switches', async () => {
    // Two datasets, both ready.
    const ds1 = makeDataset({ id: 1, name: 'Dataset-1', columns: ['col_a'] })
    const ds2 = makeDataset({ id: 2, name: 'Dataset-2', columns: ['col_b'] })

    // We control when each rows-fetch resolves.
    let resolveDs1Rows!: (v: unknown) => void
    let resolveDs2Rows!: (v: unknown) => void
    const ds1RowsPromise = new Promise((res) => { resolveDs1Rows = res })
    const ds2RowsPromise = new Promise((res) => { resolveDs2Rows = res })

    vi.stubGlobal('fetch', vi.fn(async (path: string) => {
      const j = (x: unknown) => new Response(JSON.stringify(x), { status: 200 })
      if (/\/api\/datasets\/1\/rows/.test(path)) {
        await ds1RowsPromise
        return j({ total: 1, rows: [{ col_a: 'from-ds1' }] })
      }
      if (/\/api\/datasets\/2\/rows/.test(path)) {
        await ds2RowsPromise
        return j({ total: 1, rows: [{ col_b: 'from-ds2' }] })
      }
      // initial list fetch
      return j([ds1, ds2])
    }))

    renderPage()

    // Wait for the list to render
    expect(await screen.findByText('Dataset-1')).toBeInTheDocument()

    // Click preview on ds1 — starts a slow fetch for ds1 rows
    const previewLinks = screen.getAllByText('预览')
    act(() => { previewLinks[0].click() })

    // Before ds1 resolves, click preview on ds2
    await act(async () => {
      previewLinks[1].click()
    })

    // Now resolve ds2 first (the "current" dataset)
    await act(async () => { resolveDs2Rows(null) })

    // ds2 rows should now appear in the drawer table
    expect(await screen.findByText('from-ds2')).toBeInTheDocument()

    // Now resolve ds1 (the "stale" dataset) — its rows must NOT overwrite ds2
    await act(async () => { resolveDs1Rows(null) })

    // ds1 data must not appear — the stale response was discarded
    expect(screen.queryByText('from-ds1')).not.toBeInTheDocument()
    // ds2 data still visible
    expect(screen.getByText('from-ds2')).toBeInTheDocument()
  })
})

// ── L16: importing polling ──────────────────────────────────────────────────

describe('L16 — importing status polling', () => {
  beforeEach(() => { vi.useFakeTimers({ shouldAdvanceTime: false }) })
  afterEach(() => {
    // restore real timers; flush any pending timers first to avoid leaks
    vi.runAllTimers()
    vi.useRealTimers()
  })

  it('polls list while a dataset is importing and stops when all are ready', async () => {
    const importing = makeDataset({ id: 3, name: 'BigFile', status: 'importing', imported_rows: 0 })
    const ready = makeDataset({ id: 3, name: 'BigFile', status: 'ready', imported_rows: 500 })

    let callCount = 0
    vi.stubGlobal('fetch', vi.fn((_path: string) => {
      callCount++
      const j = (x: unknown) => new Response(JSON.stringify(x), { status: 200 })
      // call 1 (mount), call 2 (poll tick 1): still importing
      // call 3+ (poll tick 2): ready
      if (callCount <= 2) return Promise.resolve(j([importing]))
      return Promise.resolve(j([ready]))
    }))

    renderPage()

    // Wait for mount fetch to settle (find importing badge)
    await act(async () => { await Promise.resolve() })
    await act(async () => { await Promise.resolve() })

    expect(screen.getByText(/导入中/)).toBeInTheDocument()
    const countAfterMount = callCount

    // Advance 1500ms — triggers the setInterval callback (poll call #2 → still importing)
    await act(async () => {
      vi.advanceTimersByTime(1500)
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(callCount).toBeGreaterThan(countAfterMount)

    // Advance another 1500ms — poll call #3 → ready; interval should clear
    await act(async () => {
      vi.advanceTimersByTime(1500)
      await Promise.resolve()
      await Promise.resolve()
    })

    // After React re-renders with ready list, badge should be gone
    expect(screen.queryByText(/导入中/)).not.toBeInTheDocument()
    expect(screen.getByText('就绪')).toBeInTheDocument()

    // Confirm polling has stopped — no more fetches when we advance more time
    const countWhenReady = callCount
    await act(async () => {
      vi.advanceTimersByTime(4500)
      await Promise.resolve()
    })
    expect(callCount).toBe(countWhenReady)
  }, 15000)
})
