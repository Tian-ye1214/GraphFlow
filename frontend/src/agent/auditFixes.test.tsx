import { describe, expect, it, vi, beforeEach } from 'vitest'
import type { AgentToolContent } from '../api/types'
import { findToolEndIndex } from './AgentDrawer'

// ── L18: error messages are excluded from history ───────────────────────────

// We test the store logic directly by importing the relevant exports and
// exercising the module-level state. We reset between tests by clearing the
// private `states` Map via a fresh module import each time.

// Because the store keeps module-level state we use the API directly.
// We mock the api module so no real HTTP is made.

vi.mock('../api/client', () => ({
  api: {
    post: vi.fn(),
  },
}))

describe('L18 — error messages are excluded from history on next send', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  it('history excludes assistant messages with error=true', async () => {
    // Re-import fresh module after resetModules so state is clean
    const { api } = await import('../api/client')
    const { sendAssist, setDraft } = await import('./nodeAssistantStore')

    const mockPost = api.post as ReturnType<typeof vi.fn>

    // First call: set draft then simulate a network error
    setDraft('wf1:node1', 'first message')
    mockPost.mockRejectedValueOnce(new Error('network failure'))

    await sendAssist('wf1:node1', {
      workflow_id: 1,
      node_id: 'node1',
      node_type: 'llm',
      model_config_id: 1,
      current_config: {},
      params: {},
    })

    // On the second send the history POSTed should NOT contain the error entry.
    let capturedHistory: Array<{ role: string; text: string }> = []
    mockPost.mockImplementationOnce((_url: string, body: any) => {
      capturedHistory = body.history
      return Promise.resolve({ reply: 'ok', config: null })
    })

    setDraft('wf1:node1', 'second message')

    await sendAssist('wf1:node1', {
      workflow_id: 1,
      node_id: 'node1',
      node_type: 'llm',
      model_config_id: 1,
      current_config: {},
      params: {},
    })

    // The history sent to the backend should NOT contain any error entry
    const errorEntries = capturedHistory.filter((m) => m.text.startsWith('出错：'))
    expect(errorEntries).toHaveLength(0)

    // The history SHOULD still contain the original user message
    const userEntries = capturedHistory.filter((m) => m.role === 'user')
    expect(userEntries.length).toBeGreaterThanOrEqual(1)
  })

  it('AssistMsg interface accepts error flag', async () => {
    const { } = await import('./nodeAssistantStore')
    // Type-level check: creating an AssistMsg with error: true should compile
    // (if this file compiles, the interface is correct)
    const msg: import('./nodeAssistantStore').AssistMsg = {
      role: 'assistant',
      text: '出错：something',
      error: true,
    }
    expect(msg.error).toBe(true)
    expect(msg.text).toContain('出错：')
  })
})

// ── L19: tool_end matching uses args_brief to disambiguate ─────────────────

describe('L19 — findToolEndIndex disambiguates concurrent same-tool calls by args_brief', () => {
  const makeEntry = (overrides: Partial<AgentToolContent>): AgentToolContent => ({
    tool: 'read_node_output',
    agent_role: 'worker',
    status: 'running',
    args_brief: 'nodeA',
    output_brief: '',
    ...overrides,
  })

  it('matches on args_brief: nodeA end matches the nodeA running entry, not nodeB', () => {
    const ts: AgentToolContent[] = [
      makeEntry({ args_brief: 'nodeA' }),
      makeEntry({ args_brief: 'nodeB' }),
    ]
    const endEvent = makeEntry({ args_brief: 'nodeA', status: 'ok', output_brief: 'result A' })
    const idx = findToolEndIndex(ts, endEvent)
    expect(idx).toBe(0) // first entry (nodeA), not nodeB
  })

  it('matches on args_brief: nodeB end matches the nodeB running entry', () => {
    const ts: AgentToolContent[] = [
      makeEntry({ args_brief: 'nodeA' }),
      makeEntry({ args_brief: 'nodeB' }),
    ]
    const endEvent = makeEntry({ args_brief: 'nodeB', status: 'ok', output_brief: 'result B' })
    const idx = findToolEndIndex(ts, endEvent)
    expect(idx).toBe(1) // second entry (nodeB)
  })

  it('returns -1 when no running entry matches', () => {
    const ts: AgentToolContent[] = [
      makeEntry({ args_brief: 'nodeC', status: 'ok' }), // already finished
    ]
    const endEvent = makeEntry({ args_brief: 'nodeC', status: 'ok' })
    const idx = findToolEndIndex(ts, endEvent)
    expect(idx).toBe(-1)
  })

  it('does not match across different tools', () => {
    const ts: AgentToolContent[] = [
      makeEntry({ tool: 'list_datasets', args_brief: 'nodeA' }),
    ]
    const endEvent = makeEntry({ tool: 'read_node_output', args_brief: 'nodeA', status: 'ok' })
    const idx = findToolEndIndex(ts, endEvent)
    expect(idx).toBe(-1)
  })

  it('does not match across different agent_roles', () => {
    const ts: AgentToolContent[] = [
      makeEntry({ agent_role: 'coordinator', args_brief: 'nodeA' }),
    ]
    const endEvent = makeEntry({ agent_role: 'worker', args_brief: 'nodeA', status: 'ok' })
    const idx = findToolEndIndex(ts, endEvent)
    expect(idx).toBe(-1)
  })
})
