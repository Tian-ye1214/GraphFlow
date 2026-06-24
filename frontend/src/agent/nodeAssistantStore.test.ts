import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', () => ({ api: { post: vi.fn() } }))
import { api } from '../api/client'
import {
  setDraft, newConversation, switchConversation, sendAssist, activeConversation,
  getState as readState,
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
})
