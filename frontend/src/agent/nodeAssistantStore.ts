import { useSyncExternalStore } from 'react'
import { api } from '../api/client'
import type { NodeAssistReply } from '../api/types'

// 应用级（模块级）store：按 `${workflowId}:${nodeId}` 隔离每个节点助手的会话与草稿。
// 不随节点抽屉/换页卸载 → 切节点/应用内导航不丢；F5 丢（既定取舍）。
// send 的 promise 由 store 持有 → 组件卸载后仍回填该 key（后台续）。

export interface AssistMsg { role: 'user' | 'assistant'; text: string; config?: Record<string, any> }
export interface NodeAssistState {
  messages: AssistMsg[]
  draft: string
  pending: boolean
  modelConfigId?: number
}

const EMPTY: NodeAssistState = { messages: [], draft: '', pending: false }
const states = new Map<string, NodeAssistState>()
const listeners = new Set<() => void>()

function emit() { listeners.forEach((l) => l()) }

function storage(): Storage | null {
  try {
    return globalThis.localStorage ?? null
  } catch {
    return null
  }
}

function restore(key: string): NodeAssistState | null {
  const raw = storage()?.getItem(key)
  if (!raw) return null
  try {
    const parsed = JSON.parse(raw) as { draft?: unknown; modelConfigId?: unknown }
    return {
      messages: [],
      draft: typeof parsed.draft === 'string' ? parsed.draft : '',
      pending: false,
      modelConfigId: typeof parsed.modelConfigId === 'number' ? parsed.modelConfigId : undefined,
    }
  } catch {
    return null
  }
}

function persist(key: string, next: NodeAssistState) {
  const s = storage()
  if (!s) return
  const hasDraft = next.draft.length > 0
  const hasModel = typeof next.modelConfigId === 'number'
  if (!hasDraft && !hasModel) {
    s.removeItem(key)
    return
  }
  s.setItem(key, JSON.stringify({ draft: next.draft, modelConfigId: next.modelConfigId }))
}

function get(key: string): NodeAssistState {
  const cached = states.get(key)
  if (cached) return cached
  const restored = restore(key)
  if (restored) {
    states.set(key, restored)
    return restored
  }
  return EMPTY
}

function set(key: string, next: NodeAssistState) {
  states.set(key, next)
  persist(key, next)
  emit()
}

export function useNodeAssist(key: string): NodeAssistState {
  return useSyncExternalStore(
    (l) => { listeners.add(l); return () => { listeners.delete(l) } },
    () => get(key),
  )
}

export function setDraft(key: string, draft: string) { set(key, { ...get(key), draft }) }
export function setModelConfigId(key: string, modelConfigId: number | undefined) {
  set(key, { ...get(key), modelConfigId })
}

export async function sendAssist(key: string, payload: {
  workflow_id: number; node_id: string; node_type: string; model_config_id: number
  current_config: Record<string, any>; params: Record<string, any>
}) {
  const cur = get(key)
  const text = cur.draft.trim()
  if (!text || cur.pending) return
  const history = cur.messages.map((m) => ({ role: m.role, text: m.text }))
  set(key, { ...cur, messages: [...cur.messages, { role: 'user', text }], draft: '', pending: true })
  try {
    const r = await api.post<NodeAssistReply>('/api/agent/node-assist', { ...payload, instruction: text, history })
    const c = get(key)
    set(key, { ...c, pending: false,
      messages: [...c.messages, { role: 'assistant', text: r.reply, config: r.config ?? undefined }] })
  } catch (e) {
    const c = get(key)
    set(key, { ...c, pending: false,
      messages: [...c.messages, { role: 'assistant', text: '出错：' + (e as Error).message }] })
  }
}
