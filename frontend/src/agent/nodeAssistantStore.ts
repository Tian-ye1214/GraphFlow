import { useSyncExternalStore } from 'react'
import { api } from '../api/client'
import type { NodeAssistReply } from '../api/types'

export interface AssistMsg { role: 'user' | 'assistant'; text: string; config?: Record<string, any>; error?: true }
export interface Conversation { id: string; title: string; messages: AssistMsg[] }
export interface NodeAssistState {
  conversations: Conversation[]
  activeId: string
  draft: string
  pending: boolean
  modelConfigId?: number
}

const states = new Map<string, NodeAssistState>()
const lastRaw = new Map<string, string | null>()  // last localStorage value seen by getState
const listeners = new Set<() => void>()
function emit() { listeners.forEach((l) => l()) }

function newId(): string {
  try { return crypto.randomUUID() } catch { return 'c' + (states.size + Math.floor(performance.now())) }
}
function emptyConv(): Conversation { return { id: newId(), title: '', messages: [] } }
function freshState(): NodeAssistState {
  const c = emptyConv()
  return { conversations: [c], activeId: c.id, draft: '', pending: false }
}

export function activeConversation(s: NodeAssistState): Conversation {
  return s.conversations.find((c) => c.id === s.activeId) ?? s.conversations[0]
}

function storage(): Storage | null {
  try { return globalThis.localStorage ?? null } catch { return null }
}
function validConv(c: any): c is Conversation {
  return c && typeof c.id === 'string' && Array.isArray(c.messages)
}
function restore(raw: string): NodeAssistState | null {
  try {
    const p = JSON.parse(raw)
    const draft = typeof p.draft === 'string' ? p.draft : ''
    const modelConfigId = typeof p.modelConfigId === 'number' ? p.modelConfigId : undefined
    const convs = Array.isArray(p.conversations) ? p.conversations.filter(validConv) : []
    if (!convs.length) return { ...freshState(), draft, modelConfigId }   // 旧格式/空 → 降级
    const activeId = convs.some((c: Conversation) => c.id === p.activeId) ? p.activeId : convs[0].id
    return { conversations: convs, activeId, draft, pending: false, modelConfigId }
  } catch { return null }
}
function persist(key: string, next: NodeAssistState) {
  const s = storage()
  if (!s) return
  const raw = JSON.stringify({
    conversations: next.conversations, activeId: next.activeId,
    draft: next.draft, modelConfigId: next.modelConfigId,
  })
  s.setItem(key, raw)
  lastRaw.set(key, raw)  // keep in sync so getState sees no change
}

export function getState(key: string): NodeAssistState {
  const currentRaw = storage()?.getItem(key) ?? null
  const cached = states.get(key)
  if (cached && lastRaw.get(key) === currentRaw) return cached  // stable reference
  // localStorage changed externally (clear/setItem outside store) — rebuild
  const init = (currentRaw ? restore(currentRaw) : null) ?? freshState()
  states.set(key, init)
  lastRaw.set(key, currentRaw)
  return init
}

function set(key: string, next: NodeAssistState) {
  states.set(key, next)
  persist(key, next)
  emit()
}

export function useNodeAssist(key: string): NodeAssistState {
  return useSyncExternalStore(
    (l) => { listeners.add(l); return () => { listeners.delete(l) } },
    () => getState(key),
  )
}

export function setDraft(key: string, draft: string) { set(key, { ...getState(key), draft }) }
export function setModelConfigId(key: string, modelConfigId: number | undefined) {
  set(key, { ...getState(key), modelConfigId })
}
export function newConversation(key: string) {
  const cur = getState(key)
  const conv = emptyConv()
  set(key, { ...cur, conversations: [conv, ...cur.conversations], activeId: conv.id, draft: '' })
}
export function switchConversation(key: string, id: string) {
  const cur = getState(key)
  if (cur.conversations.some((c) => c.id === id)) set(key, { ...cur, activeId: id })
}

function replaceConv(s: NodeAssistState, conv: Conversation): Conversation[] {
  return s.conversations.map((c) => (c.id === conv.id ? conv : c))
}

const controllers = new Map<string, AbortController>()
const callIds = new Map<string, string>()

export async function sendAssist(key: string, payload: {
  workflow_id: number; node_id: string; node_type: string; model_config_id: number
  current_config: Record<string, any>; params: Record<string, any>
}) {
  const cur = getState(key)
  const text = cur.draft.trim()
  if (!text || cur.pending) return
  const active = activeConversation(cur)
  const history = active.messages.filter((m) => !m.error).map((m) => ({ role: m.role, text: m.text }))
  const withUser: Conversation = {
    ...active,
    title: active.title || text.slice(0, 20),
    messages: [...active.messages, { role: 'user', text }],
  }
  set(key, { ...cur, draft: '', pending: true, conversations: replaceConv(cur, withUser) })
  const callId = newId()
  const ctrl = new AbortController()
  controllers.set(key, ctrl)
  callIds.set(key, callId)
  try {
    const r = await api.post<NodeAssistReply>('/api/agent/node-assist',
      { ...payload, instruction: text, history, call_id: callId }, ctrl.signal)
    const c = getState(key)
    const a = c.conversations.find((x) => x.id === active.id)
    if (!a) { set(key, { ...c, pending: false }); return }
    set(key, { ...c, pending: false, conversations: replaceConv(c,
      { ...a, messages: [...a.messages, { role: 'assistant', text: r.reply, config: r.config ?? undefined }] }) })
  } catch (e) {
    const aborted = (e as Error).name === 'AbortError'
    const c = getState(key)
    const a = c.conversations.find((x) => x.id === active.id)
    if (!a) { set(key, { ...c, pending: false }); return }
    const bubble: AssistMsg = aborted
      ? { role: 'assistant', text: '（已打断）' }
      : { role: 'assistant', text: '出错：' + (e as Error).message, error: true as const }
    set(key, { ...c, pending: false, conversations: replaceConv(c, { ...a, messages: [...a.messages, bubble] }) })
  } finally {
    controllers.delete(key)
    callIds.delete(key)
  }
}

export function cancelAssist(key: string) {
  const callId = callIds.get(key)
  if (callId) void api.post('/api/agent/node-assist/stop', { call_id: callId }).catch(() => {})
  controllers.get(key)?.abort()
}
