import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', () => ({ api: { post: vi.fn() } }))
import { api } from '../api/client'
import {
  setDraft, newConversation, switchConversation, sendAssist, activeConversation,
  cancelAssist, getState as readState,
} from './nodeAssistantStore'

beforeEach(() => { localStorage.clear(); vi.clearAllMocks() })

describe('nodeAssistantStore 多会话', () => {
  const KEY = 'graphflow.nodeAssistant.v1:1:llm_synth:n1'

  it('newConversation 新开空会话、旧会话仍在且可切回', async () => {
    ;(api.post as any).mockResolvedValue({ reply: 'ok', config: null })
    setDraft(KEY, '第一句')
    await sendAssist(KEY, { workflow_id: 1, node_id: 'n1', node_type: 'llm_synth', model_config_id: 9, current_config: {}, params: {} })
    const s1 = readState(KEY)
    const firstConv = s1.activeId
    expect(activeConversation(s1).messages.length).toBe(2)  // user + assistant
    newConversation(KEY)
    const s2 = readState(KEY)
    expect(s2.activeId).not.toBe(firstConv)
    expect(activeConversation(s2).messages.length).toBe(0)  // 新会话空
    expect(s2.conversations.length).toBe(2)
    switchConversation(KEY, firstConv)
    expect(activeConversation(readState(KEY)).messages.length).toBe(2)  // 切回旧会话消息还在
  })

  it('消息持久化到 localStorage（跨实例还原）', async () => {
    ;(api.post as any).mockResolvedValue({ reply: 'ok', config: null })
    setDraft(KEY, 'hi')
    await sendAssist(KEY, { workflow_id: 1, node_id: 'n1', node_type: 'llm_synth', model_config_id: 9, current_config: {}, params: {} })
    const raw = localStorage.getItem(KEY)!
    expect(JSON.parse(raw).conversations[0].messages.length).toBe(2)
  })

  it('损坏的旧格式 localStorage 降级为单空会话', () => {
    localStorage.setItem(KEY + ':legacy', JSON.stringify({ draft: 'x', modelConfigId: 3 }))
    const s = readState(KEY + ':legacy')
    expect(s.conversations.length).toBe(1)
    expect(s.draft).toBe('x')
    expect(s.modelConfigId).toBe(3)
  })

  it('sendAssist 带 call_id 与 AbortSignal', async () => {
    ;(api.post as any).mockResolvedValue({ reply: 'ok', config: null })
    setDraft(KEY, 'hi')
    await sendAssist(KEY, { workflow_id: 1, node_id: 'n1', node_type: 'llm_synth', model_config_id: 9, current_config: {}, params: {} })
    const [url, body, signal] = (api.post as any).mock.calls[0]
    expect(url).toBe('/api/agent/node-assist')
    expect(typeof body.call_id).toBe('string')
    expect(body.call_id.length).toBeGreaterThan(0)
    expect(signal).toBeInstanceOf(AbortSignal)
  })

  it('cancelAssist 调 stop 端点 + 中止在途请求，落「（已打断）」气泡', async () => {
    let captured: AbortSignal | undefined
    ;(api.post as any).mockImplementationOnce((_u: string, _b: any, signal: AbortSignal) => {
      captured = signal
      return new Promise((_resolve, reject) => {
        signal.addEventListener('abort', () => {
          const e = new Error('aborted'); (e as any).name = 'AbortError'; reject(e)
        })
      })
    })
    setDraft(KEY, 'hi')
    const p = sendAssist(KEY, { workflow_id: 1, node_id: 'n1', node_type: 'llm_synth', model_config_id: 9, current_config: {}, params: {} })
    await Promise.resolve()
    expect(readState(KEY).pending).toBe(true)
    ;(api.post as any).mockResolvedValueOnce({ ok: true })   // stop 端点
    cancelAssist(KEY)
    await p
    const stopCall = (api.post as any).mock.calls.find((c: any[]) => c[0] === '/api/agent/node-assist/stop')
    expect(stopCall).toBeTruthy()
    expect(stopCall[1].call_id.length).toBeGreaterThan(0)
    expect(captured?.aborted).toBe(true)
    const s = readState(KEY)
    expect(s.pending).toBe(false)
    const msgs = activeConversation(s).messages
    expect(msgs[msgs.length - 1]).toMatchObject({ role: 'assistant', text: '（已打断）' })
    expect(msgs[msgs.length - 1].error).toBeUndefined()
  })
})
