import '@testing-library/jest-dom/vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import NodeConfigForm from './NodeConfigForm'

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal('ResizeObserver', ResizeObserverStub)

afterEach(() => {
  vi.restoreAllMocks()
})

function mockColumns(columns: Record<string, { input: string[]; output: string[] }>) {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.includes('/api/workflows/1/columns')) {
      return new Response(JSON.stringify(columns), { status: 200 })
    }
    if (path.includes('/api/models')) {
      return new Response(JSON.stringify([]), { status: 200 })
    }
    return new Response(JSON.stringify({ detail: 'unexpected' }), { status: 404 })
  }))
}

describe('NodeConfigForm QC feedback column', () => {
  it('shows configurable feedback column on qc forms', async () => {
    mockColumns({ qc: { input: ['q', 'answer'], output: ['q', 'answer', 'qc_feedback'] } })

    render(<NodeConfigForm type="qc" workflowId={1} nodeId="qc" config={{}} onChange={() => {}} />)

    expect(await screen.findByText('反馈列名')).toBeInTheDocument()
    expect(screen.getByDisplayValue('qc_feedback')).toBeInTheDocument()
  })

  it('shows qc feedback as a produced output column', async () => {
    mockColumns({ qc: { input: ['q', 'answer'], output: ['q', 'answer'] } })

    render(<NodeConfigForm type="qc" workflowId={1} nodeId="qc" config={{}} onChange={() => {}} />)

    await screen.findByText('输出列 (3) ▾')
    expect(screen.getByText('qc_feedback')).toBeInTheDocument()
  })

  it('allows downstream llm prompts to reference qc feedback', async () => {
    mockColumns({ llm: { input: ['q', 'answer', 'qc_feedback'], output: ['q', 'answer', 'qc_feedback'] } })

    render(
      <NodeConfigForm
        type="llm_synth"
        workflowId={1}
        nodeId="llm"
        config={{ user_prompt: '根据{{qc_feedback}}改写答案' }}
        onChange={() => {}}
      />,
    )

    await waitFor(() => expect(screen.queryByText(/引用了上游未产出的列/)).not.toBeInTheDocument())
    const prompt = screen.getByDisplayValue('根据{{qc_feedback}}改写答案')
    expect(within(prompt.closest('div') as HTMLElement).queryByText(/引用了上游未产出的列/)).not.toBeInTheDocument()
  })
})
