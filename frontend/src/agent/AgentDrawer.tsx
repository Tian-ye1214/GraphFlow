import { useCallback, useEffect, useRef, useState } from 'react'
import { Button, Collapse, Drawer, FloatButton, Input, Select, Space, Spin, Tag, message } from 'antd'
import ReactMarkdown from 'react-markdown'
import { api } from '../api/client'
import { useEvents } from '../api/events'
import type {
  AgentMessageOut, AgentSessionDetail, AgentSessionSummary, AgentToolContent, ModelConfig,
} from '../api/types'
import { extractConfirmDeletes, stripGoalMarkers } from './parse'

const ROLES = ['coordinator', 'manager', 'worker'] as const

export default function AgentDrawer() {
  const [open, setOpen] = useState(false)
  const [sessions, setSessions] = useState<AgentSessionSummary[]>([])
  const [detail, setDetail] = useState<AgentSessionDetail | null>(null)
  const [models, setModels] = useState<ModelConfig[]>([])
  const [modelSel, setModelSel] = useState<number>()
  const [advanced, setAdvanced] = useState(false)
  const [roleSel, setRoleSel] = useState<Record<string, number | undefined>>({})
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState('')
  const [liveTools, setLiveTools] = useState<AgentToolContent[]>([])
  const [goalRound, setGoalRound] = useState(0)
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
    await refreshDetail(sid)
  }, [refreshDetail])

  useEffect(() => {
    if (!open) return
    void api.get<AgentSessionSummary[]>('/api/agent/sessions').then((list) => {
      setSessions(list)
      if (list.length && sessionIdRef.current === null) void selectSession(list[0].id)
    })
    void api.get<ModelConfig[]>('/api/models').then(setModels)
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
    else if (e.kind === 'turn_done') {
      setStreaming('')
      setLiveTools([])
      setGoalRound(0)
      void refreshDetail(e.id)
    }
  })

  const newSession = async () => {
    const useAdvanced = advanced && ROLES.every((r) => roleSel[r])
    if (!useAdvanced && !modelSel) {
      message.warning('先选择模型配置')
      return
    }
    const body = useAdvanced ? { models: roleSel } : { model_config_id: modelSel }
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
      <Drawer open={open} onClose={() => setOpen(false)} width={440} mask={false}
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
                </Space>
              }>
        {advanced && (
          <Space style={{ marginBottom: 8 }} wrap>
            {ROLES.map((r) => (
              <Select key={r} size="small" style={{ width: 130 }} placeholder={r}
                      value={roleSel[r]} onChange={(v) => setRoleSel({ ...roleSel, [r]: v })}
                      options={models.map((m) => ({ value: m.id, label: `${r}: ${m.name}` }))} />
            ))}
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
