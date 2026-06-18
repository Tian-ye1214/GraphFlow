import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, Collapse, Drawer, FloatButton, Input, Popconfirm, Select, Space, Spin, Switch, Tag, message } from 'antd'
import ReactMarkdown from 'react-markdown'
import { api } from '../api/client'
import { useEvents } from '../api/events'
import type {
  AgentMessageOut, AgentSessionDetail, AgentSessionSummary, AgentToolContent, ModelConfig,
} from '../api/types'
import { extractConfirmDeletes, stripGoalMarkers } from './parse'

export const AGENT_ROLES = ['coordinator', 'manager', 'worker', 'compactor'] as const
const THINKING_EFFORT_OPTIONS = ['low', 'medium', 'high', 'xhigh', 'max'] as const

function withThinkingParamDefaults(params?: Record<string, any>) {
  return {
    ...(params ?? {}),
    thinking_enabled: params?.thinking_enabled ?? true,
    reasoning_effort: params?.reasoning_effort ?? 'high',
  }
}

export function buildSessionPayload({
  advanced,
  modelSel,
  roleSel,
  sharedParams,
  roleParams,
}: {
  advanced: boolean
  modelSel?: number
  roleSel: Record<string, number | undefined>
  sharedParams: Record<string, any>
  roleParams: Record<string, Record<string, any>>
}) {
  const useAdvanced = advanced && AGENT_ROLES.every((r) => roleSel[r])
  const modelParams = Object.fromEntries(AGENT_ROLES.map((role) => [
    role,
    withThinkingParamDefaults(useAdvanced ? roleParams[role] : sharedParams),
  ]))
  if (useAdvanced) return { models: roleSel, model_params: modelParams }
  return { model_config_id: modelSel, model_params: modelParams }
}

function ThinkingControls({ params, patchParams }: {
  params: Record<string, any>
  patchParams: (p: object) => void
}) {
  const thinking = withThinkingParamDefaults(params)
  return (
    <>
      <span style={{ fontSize: 12 }}>思考
        <Switch size="small" style={{ marginLeft: 4 }} checked={thinking.thinking_enabled}
                onChange={(v) => patchParams({ thinking_enabled: v })} /></span>
      <Select size="small" style={{ width: 90 }} value={thinking.reasoning_effort}
              disabled={!thinking.thinking_enabled}
              onChange={(v) => patchParams({ reasoning_effort: v })}
              options={THINKING_EFFORT_OPTIONS.map((e) => ({ value: e, label: e }))} />
    </>
  )
}

export default function AgentDrawer() {
  const [open, setOpen] = useState(false)
  const [sessions, setSessions] = useState<AgentSessionSummary[]>([])
  const [detail, setDetail] = useState<AgentSessionDetail | null>(null)
  const [models, setModels] = useState<ModelConfig[]>([])
  const [modelSel, setModelSel] = useState<number>()
  const [advanced, setAdvanced] = useState(false)
  const [roleSel, setRoleSel] = useState<Record<string, number | undefined>>({})
  const [sharedParams, setSharedParams] = useState<Record<string, any>>({})
  const [roleParams, setRoleParams] = useState<Record<string, Record<string, any>>>({})
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState('')
  const [liveTools, setLiveTools] = useState<AgentToolContent[]>([])
  const [goalRound, setGoalRound] = useState(0)
  const [goalMode, setGoalMode] = useState(false)
  const [goalText, setGoalText] = useState('')
  const [goalWf, setGoalWf] = useState<number>()
  const [workflows, setWorkflows] = useState<{ id: number; name: string }[]>([])
  const [metrics, setMetrics] = useState<{ round: number; metric: number | null; run_id: number }[]>([])
  const sessionIdRef = useRef<number | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)

  const refreshDetail = useCallback(async (sid: number) => {
    setDetail(await api.get<AgentSessionDetail>(`/api/agent/sessions/${sid}`))
  }, [])

  const selectSession = useCallback(async (sid: number) => {
    sessionIdRef.current = sid
    setStreaming('')
    setLiveTools([])
    setGoalRound(0)
    setMetrics([])
    await refreshDetail(sid)
  }, [refreshDetail])

  const deleteSession = async (sid: number) => {
    try {
      await api.del(`/api/agent/sessions/${sid}`)
      const rest = sessions.filter((x) => x.id !== sid)
      setSessions(rest)
      if (sessionIdRef.current === sid) {
        sessionIdRef.current = null
        if (rest.length) await selectSession(rest[0].id)
        else setDetail(null)
      }
    } catch (e) { message.error((e as Error).message) }
  }
  const deleteAllSessions = async () => {
    try {
      await api.del('/api/agent/sessions')
      setSessions([]); sessionIdRef.current = null; setDetail(null)
      message.success('已清空会话')
    } catch (e) { message.error((e as Error).message) }
  }

  useEffect(() => {
    if (!open) return
    void api.get<AgentSessionSummary[]>('/api/agent/sessions').then((list) => {
      setSessions(list)
      if (list.length && sessionIdRef.current === null) void selectSession(list[0].id)
    })
    void api.get<ModelConfig[]>('/api/models').then(setModels)
    void api.get<{ id: number; name: string }[]>('/api/workflows').then(setWorkflows)
  }, [open, selectSession])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [detail?.messages.length, streaming, liveTools.length])

  useEvents((e) => {
    if (e.entity !== 'agent' || e.id !== sessionIdRef.current) return
    if (e.kind === 'delta') setStreaming((s) => s + String(e.data ?? ''))
    else if (e.kind === 'tool_start') {
      const d = e.data as AgentToolContent
      setLiveTools((ts) => [...ts, { ...d, status: 'running' }])
    } else if (e.kind === 'tool_end') {
      const d = e.data as AgentToolContent
      setLiveTools((ts) => {
        const i = ts.findIndex((t) => t.status === 'running' && t.tool === d.tool && t.agent_role === d.agent_role)
        if (i < 0) return [...ts, d]
        const next = ts.slice()
        next[i] = d
        return next
      })
    } else if (e.kind === 'message') {
      setStreaming('')
      setLiveTools([])
      void refreshDetail(e.id)
    } else if (e.kind === 'goal_round') setGoalRound(Number(e.data) || 0)
    else if (e.kind === 'goal_metric') setMetrics((m) => [...m, e.data as { round: number; metric: number | null; run_id: number }])
    else if (e.kind === 'turn_done') {
      setStreaming('')
      setLiveTools([])
      setGoalRound(0)
      setMetrics([])
      void refreshDetail(e.id)
    }
  })

  const newSession = async () => {
    const useAdvanced = advanced && AGENT_ROLES.every((r) => roleSel[r])
    if (!useAdvanced && !modelSel) {
      message.warning('先选择模型配置')
      return
    }
    const body = buildSessionPayload({ advanced, modelSel, roleSel, sharedParams, roleParams })
    try {
      const s = await api.post<AgentSessionSummary>('/api/agent/sessions', body)
      setSessions((list) => [s, ...list])
      await selectSession(s.id)
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const send = async (text: string) => {
    const sid = sessionIdRef.current
    if (!sid || !text.trim()) return
    try {
      await api.post(`/api/agent/sessions/${sid}/messages`, { text })
      setInput('')
      await refreshDetail(sid)
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  const stop = async () => {
    if (sessionIdRef.current) await api.post(`/api/agent/sessions/${sessionIdRef.current}/stop`)
  }

  const startGoal = async () => {
    const sid = sessionIdRef.current
    if (!sid || !goalText.trim() || !goalWf) { message.warning('选择工作流并填写目标'); return }
    try {
      setMetrics([])
      await api.post(`/api/agent/sessions/${sid}/goal`, { workflow_id: goalWf, goal_text: goalText })
      setGoalText('')
      await refreshDetail(sid)
    } catch (e) { message.error((e as Error).message) }
  }

  const running = detail?.status === 'running'

  const renderToolEntry = (t: AgentToolContent, key: string | number) => (
    <Collapse
      key={key}
      size="small"
      style={{ marginBottom: 4 }}
      items={[{
        key: '1',
        label: (
          <span style={{ fontSize: 12 }}>
            ⚙ {t.args_brief || t.tool} {t.status === 'ok' ? '✓' : t.status === 'error' ? '✗' : '…'}
            <Tag style={{ marginLeft: 8 }}>{t.agent_role}</Tag>
          </span>
        ),
        children: <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, margin: 0 }}>{t.output_brief || '(运行中)'}</pre>,
      }]}
    />
  )

  const renderMessage = (m: AgentMessageOut) => {
    if (m.role === 'tool') return renderToolEntry(m.content as AgentToolContent, m.id)
    const raw = m.content.text ?? ''
    if (m.role === 'user') {
      return (
        <div key={m.id} style={{ textAlign: 'right', margin: '8px 0' }}>
          <span style={{ background: '#e6f4ff', borderRadius: 8, padding: '6px 10px', display: 'inline-block', whiteSpace: 'pre-wrap' }}>{raw}</span>
        </div>
      )
    }
    const { text, commands } = extractConfirmDeletes(stripGoalMarkers(raw))
    return (
      <div key={m.id} style={{ margin: '8px 0' }}>
        <ReactMarkdown>{text}</ReactMarkdown>
        {commands.map((cmd) => (
          <Button key={cmd} danger size="small" style={{ marginRight: 8 }} disabled={running}
                  onClick={() => void send(`确认：${cmd}`)}>
            确认删除：{cmd}
          </Button>
        ))}
      </div>
    )
  }

  return (
    <>
      <FloatButton type="primary" style={{ right: 24, bottom: 24 }}
                   icon={<span>❦</span>} onClick={() => setOpen(true)} />
      <Drawer open={open} onClose={() => setOpen(false)} placement="bottom" height="45vh" mask={false}
              title={
                <Space>
                  <Select size="small" style={{ width: 160 }} placeholder="选择会话"
                          value={detail?.id} onChange={(v) => void selectSession(v)}
                          options={sessions.map((s) => ({ value: s.id, label: s.title || `会话 ${s.id}` }))} />
                  <Button size="small" onClick={() => void newSession()}>新建</Button>
                  <Select size="small" style={{ width: 120 }} placeholder="模型"
                          value={modelSel} onChange={setModelSel}
                          options={models.map((m) => ({ value: m.id, label: m.name }))} />
                  <Button size="small" type="text" onClick={() => setAdvanced(!advanced)}>高级</Button>
                  <Popconfirm title="删除当前会话？" disabled={!detail}
                              onConfirm={() => detail && void deleteSession(detail.id)}>
                    <Button size="small" danger disabled={!detail}>删除会话</Button>
                  </Popconfirm>
                  <Popconfirm title="清空全部会话？" onConfirm={() => void deleteAllSessions()}>
                    <Button size="small" danger>清空全部</Button>
                  </Popconfirm>
                  <span style={{ fontSize: 12 }}>目标模式
                    <Switch size="small" style={{ marginLeft: 4 }} checked={goalMode}
                            onChange={setGoalMode} /></span>
                </Space>
              }>
        {advanced && (
          <Space style={{ marginBottom: 8 }} wrap>
            {AGENT_ROLES.map((r) => (
              <Select key={r} size="small" style={{ width: 130 }} placeholder={r}
                      value={roleSel[r]} onChange={(v) => setRoleSel({ ...roleSel, [r]: v })}
                      options={models.map((m) => ({ value: m.id, label: `${r}: ${m.name}` }))} />
            ))}
          </Space>
        )}
        <Space style={{ marginBottom: 8 }} wrap>
          {advanced
            ? AGENT_ROLES.map((r) => (
              <Space key={r}>
                <Tag>{r}</Tag>
                <ThinkingControls params={roleParams[r] ?? {}}
                                  patchParams={(p) => setRoleParams({ ...roleParams, [r]: { ...(roleParams[r] ?? {}), ...p } })} />
              </Space>
            ))
            : <ThinkingControls params={sharedParams}
                                patchParams={(p) => setSharedParams({ ...sharedParams, ...p })} />}
        </Space>
        {goalMode && detail && !running && (
          <Space style={{ marginBottom: 8 }} wrap>
            <Select size="small" style={{ width: 160 }} placeholder="目标工作流"
                    value={goalWf} onChange={setGoalWf}
                    options={workflows.map((w) => ({ value: w.id, label: w.name }))} />
            <Input size="small" style={{ width: 280 }}
                   placeholder="一句话目标，如：把首轮质检通过率提到 90%"
                   value={goalText} onChange={(e) => setGoalText(e.target.value)} />
            <Button size="small" type="primary" onClick={() => void startGoal()}>启动目标</Button>
          </Space>
        )}
        <div style={{ height: 'calc(100% - 120px)', overflowY: 'auto' }}>
          {detail?.messages.map(renderMessage)}
          {liveTools.map((t, i) => renderToolEntry(t, `live-${i}`))}
          {streaming && <ReactMarkdown>{stripGoalMarkers(streaming)}</ReactMarkdown>}
          {running && !streaming && <Spin size="small" style={{ display: 'block', margin: 8 }} />}
          <div ref={bottomRef} />
        </div>
        <div style={{ position: 'absolute', bottom: 12, left: 16, right: 16 }}>
          {running && (
            <Space style={{ marginBottom: 6 }}>
              <Tag color="processing">红莲正在工作…{goalRound > 0 && `目标进行中 · 第 ${goalRound} 轮`}</Tag>
              {goalRound > 0 && <Button size="small" danger onClick={() => void stop()}>停止</Button>}
            </Space>
          )}
        {metrics.length > 0 && (
          <div style={{ fontSize: 12, color: '#555', marginBottom: 6 }}>
            {metrics.map((m) => (
              <span key={m.round} style={{ marginRight: 12 }}>
                第{m.round}轮: {m.metric === null ? '—' : `${(m.metric * 100).toFixed(1)}%`}（#{m.run_id}）
              </span>
            ))}
          </div>
        )}
          <Space.Compact style={{ width: '100%' }}>
            <Input.TextArea autoSize={{ minRows: 1, maxRows: 4 }} value={input} disabled={running || !detail}
                            onChange={(e) => setInput(e.target.value)}
                            onPressEnter={(e) => {
                              if (!e.shiftKey) {
                                e.preventDefault()
                                void send(input)
                              }
                            }}
                            placeholder={detail ? '让红莲帮你搭链路、配模型、跑数据…' : '先新建会话'} />
            <Button type="primary" disabled={running || !detail} onClick={() => void send(input)}>发送</Button>
          </Space.Compact>
        </div>
      </Drawer>
    </>
  )
}
