import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Button, Card, Collapse, Drawer, Popconfirm, Progress, Select, Space, Spin, Statistic, Table, Tabs, Tag, message } from 'antd'
import { useParams } from 'react-router-dom'
import { api, triggerDownload } from '../api/client'
import { useEvents } from '../api/events'
import type { AgentSessionSummary, ModelLogEntry, NodeState, QcFailureEntry, QcMetric, RowsPage, RunDetail, RunLogEntry, TraceDetail, TraceEvent } from '../api/types'
import { formatRunLog } from './runLog'
import { NODE_LABELS } from '../canvas/serialize'
import { STATUS_COLORS, STATUS_LABELS } from './RunsPage'
import { fmtDuration, renderCell } from '../utils'

const ACTIVE = ['queued', 'running']
type QueueReply = { ok: boolean; queued?: boolean; position?: number }

function compactJson(value: unknown) {
  return JSON.stringify(value, null, 2)
}

export default function RunDetailPage() {
  const { id } = useParams()
  const [run, setRun] = useState<RunDetail | null>(null)
  const [selectedNode, setSelectedNode] = useState<string>()
  const [page, setPage] = useState(1)
  const [rows, setRows] = useState<RowsPage>({ total: 0, rows: [] })
  const [failedPage, setFailedPage] = useState(1)
  const [failed, setFailed] = useState<RowsPage>({ total: 0, rows: [] })
  const [format, setFormat] = useState('jsonl')
  const [logs, setLogs] = useState<RunLogEntry[]>([])
  const [qcFailures, setQcFailures] = useState<QcFailureEntry[]>([])
  const [qcMetrics, setQcMetrics] = useState<QcMetric[]>([])
  const [modelLogs, setModelLogs] = useState<ModelLogEntry[]>([])
  const [trace, setTrace] = useState<TraceDetail | null>(null)
  const [traceOpen, setTraceOpen] = useState(false)
  const [traceLoading, setTraceLoading] = useState(false)
  const [diagnosing, setDiagnosing] = useState(false)
  const refreshLogs = useCallback(
    () => api.get<RunLogEntry[]>(`/api/runs/${id}/logs`).then(setLogs), [id])
  useEffect(() => { void refreshLogs() }, [refreshLogs])
  useEffect(() => {
    if (!run || !ACTIVE.includes(run.status)) return
    const t = setInterval(() => void refreshLogs(), 2000)
    return () => clearInterval(t)
  }, [run?.status, refreshLogs])
  const downloadLog = () => {
    triggerDownload(new Blob([formatRunLog(logs)], { type: 'text/plain' }), `run${id}.log`)
  }

  const refresh = useCallback(() => api.get<RunDetail>(`/api/runs/${id}`).then(setRun), [id])
  useEffect(() => {
    void refresh()
  }, [refresh])
  useEffect(() => {  // 运行中每 2 秒轮询
    if (!run || !ACTIVE.includes(run.status)) return
    const t = setInterval(() => void refresh(), 2000)
    return () => clearInterval(t)
  }, [run?.status, refresh])

  useEvents((e) => {
    if (e.entity !== 'run' || e.id !== Number(id)) return
    if (e.kind === 'progress') {
      const d = e.data as NodeState
      setRun((r) => r && ({
        ...r,
        node_states: r.node_states.some((s) => s.node_id === d.node_id)
          ? r.node_states.map((s) => (s.node_id === d.node_id ? d : s))
          : [...r.node_states, d],
      }))
    } else void refresh()
  })

  const node = selectedNode ?? run?.graph.nodes.find((n) => n.type === 'output')?.id
  const isActive = run ? ACTIVE.includes(run.status) : true

  // 运行期也可观测：后端对 running 无限制，故结果/失败行/模型对话在运行中即按需拉取并随轮询实时刷新
  const refreshRows = useCallback(() => {
    if (!node) return
    void api.get<RowsPage>(`/api/runs/${id}/rows?node_id=${node}&page=${page}&page_size=20`).then(setRows)
    void api.get<RowsPage>(`/api/runs/${id}/rows?node_id=${node}&status=failed&page=${failedPage}&page_size=20`).then(setFailed)
  }, [id, node, page, failedPage])
  const refreshAux = useCallback(() => {
    void api.get<QcFailureEntry[]>(`/api/runs/${id}/qc-failures`).then(setQcFailures)
    void api.get<QcMetric[]>(`/api/runs/${id}/qc-metrics`).then(setQcMetrics)
    void api.get<ModelLogEntry[]>(`/api/runs/${id}/model-logs`).then(setModelLogs)
  }, [id])
  useEffect(() => { if (run) refreshRows() }, [run?.status, refreshRows])
  useEffect(() => { if (run) refreshAux() }, [run?.status, refreshAux])
  useEffect(() => {  // 运行中每 2 秒实时刷新结果数据（与日志/进度同频）
    if (!run || !ACTIVE.includes(run.status)) return
    const t = setInterval(() => { refreshRows(); refreshAux() }, 2000)
    return () => clearInterval(t)
  }, [run?.status, refreshRows, refreshAux])

  const openTrace = useCallback(async (traceId?: string) => {
    if (!traceId) {
      message.info('该运行不支持行级 Trace')
      return
    }
    setTraceOpen(true)
    setTrace(null)
    setTraceLoading(true)
    try {
      setTrace(await api.get<TraceDetail>(`/api/runs/${id}/trace/${encodeURIComponent(traceId)}`))
    } catch (e) {
      message.error(e instanceof Error ? e.message : 'Trace 加载失败')
    } finally {
      setTraceLoading(false)
    }
  }, [id])

  const diagnoseRun = useCallback(async () => {
    setDiagnosing(true)
    try {
      const sessions = await api.get<AgentSessionSummary[]>('/api/agent/sessions')
      const sess = sessions.find((s) => s.status !== 'running') ?? sessions[0]
      if (!sess) {
        message.warning('请先创建或选择红莲会话')
        return
      }
      const r = await api.post<QueueReply>(`/api/agent/sessions/${sess.id}/diagnose-run`, { run_id: Number(id) })
      message.success(r.queued ? `已加入红莲诊断队列（前方 ${r.position ?? 1} 个任务）` : '已提交给红莲诊断')
    } catch (e) {
      message.error(e instanceof Error ? e.message : '提交诊断失败')
    } finally {
      setDiagnosing(false)
    }
  }, [id])

  const nodeLabel = useCallback((nid: string) => {
    const n = run?.graph.nodes.find((g) => g.id === nid)
    return n ? `${NODE_LABELS[n.type]}（${nid}）` : nid
  }, [run])

  const orderedStates = useMemo(() => {
    if (!run) return []
    const byId = Object.fromEntries(run.node_states.map((s) => [s.node_id, s]))
    return run.graph.nodes.map((n) => byId[n.id]).filter(Boolean)
  }, [run])

  if (!run) return null
  const hasFailed = run.node_states.some((s) => s.failed > 0)
  const previewColumns = Object.keys(rows.rows[0] ?? {}).map((c) => ({
    title: c, dataIndex: c, ellipsis: true, render: renderCell,
  }))
  const failureGroups = Object.entries(qcFailures.reduce<Record<string, number>>((acc, f) => {
    f.reasons
      .filter((r) => r.status.toLowerCase() !== 'pass')
      .forEach((r) => { acc[r.reason || '未说明原因'] = (acc[r.reason || '未说明原因'] ?? 0) + 1 })
    return acc
  }, {})).sort((a, b) => b[1] - a[1]).slice(0, 8)
  const traceButton = (traceId?: string) => traceId
    ? <Button size="small" onClick={() => void openTrace(traceId)}>Trace</Button>
    : <Tag>无 Trace</Tag>
  const renderTraceEvent = (ev: TraceEvent) => (
    <Space orientation="vertical" style={{ width: '100%' }}>
      <Space wrap>
        <Tag color={ev.status === 'failed' ? 'error' : ev.status === 'done' ? 'success' : 'processing'}>
          {ev.status}
        </Tag>
        {ev.node_type && <Tag>{ev.node_type}</Tag>}
        {ev.row_idx !== undefined && <span>row_idx：{ev.row_idx}</span>}
        {ev.attempt !== undefined && <span>attempt：{ev.attempt}</span>}
        {ev.qc_round !== undefined && <span>qc_round：{ev.qc_round}</span>}
        {ev.tokens && <span>tokens：{(ev.tokens.prompt_tokens ?? 0) + (ev.tokens.completion_tokens ?? 0)}</span>}
      </Space>
      {ev.error && <Alert type="error" message={ev.error} />}
      {ev.qc_reasons?.length ? (
        <div>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>QC 理由</div>
          {ev.qc_reasons.map((r, i) => <div key={i}>{r.status}：{r.reason}</div>)}
        </div>
      ) : null}
      {ev.output?.length ? <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{compactJson(ev.output)}</pre> : null}
      {ev.model_logs?.length ? (
        <Collapse size="small" items={ev.model_logs.map((log) => ({
          key: String(log.id),
          label: `${log.source} · ${log.model_name}`,
          children: <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
            {compactJson(log.request)}{'\n--- 响应 ---\n'}{log.response}
          </pre>,
        }))} />
      ) : null}
    </Space>
  )

  return (
    <>
      <Space style={{ marginBottom: 16 }} wrap>
        <h3 style={{ margin: 0 }}>运行 #{run.id}（{run.workflow_name}）</h3>
        <Tag color={STATUS_COLORS[run.status]}>{STATUS_LABELS[run.status] ?? run.status}</Tag>
        <span>时长：{fmtDuration(run.started_at, run.finished_at)}</span>
        <span>Token：{(run.stats.prompt_tokens ?? 0) + (run.stats.completion_tokens ?? 0)}</span>
        {isActive && (
          <Popconfirm title="确认取消？" onConfirm={async () => { await api.post(`/api/runs/${id}/cancel`); message.success('已请求取消'); await refresh() }}>
            <Button danger size="small">取消</Button>
          </Popconfirm>
        )}
        {hasFailed && (
          <Button size="small" type="primary"
                  onClick={async () => {
                    const r = await api.post<QueueReply>(`/api/runs/${id}/rerun-failed`)
                    message.success(r.queued ? `失败行已加入重跑队列（前方 ${r.position ?? 1} 个任务）` : '失败行已重新入队')
                    await refresh()
                  }}>
            重跑失败行
          </Button>
        )}
        {!isActive && (
          <Popconfirm title="把当前工作流恢复为此运行的版本？" onConfirm={async () => {
            await api.post(`/api/runs/${id}/restore`); message.success('已恢复工作流版本')
          }}>
            <Button size="small">恢复此版本</Button>
          </Popconfirm>
        )}
      </Space>
      {run.error && <Alert type="error" message={run.error} style={{ marginBottom: 16 }} />}
      {isActive && (() => {
        const tot = run.node_states.reduce((a, s) => a + s.total, 0)
        const dn = run.node_states.reduce((a, s) => a + s.done, 0)
        return <Progress percent={tot ? Math.round((dn / tot) * 100) : 0} status="active"
                         style={{ marginBottom: 12 }} />
      })()}
      <Space wrap style={{ marginBottom: 16 }}>
        {orderedStates.map((s) => (
          <Card key={s.node_id} size="small" style={{ width: 230 }}>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>{nodeLabel(s.node_id)}</div>
            <Progress percent={s.total ? Math.round((s.done / s.total) * 100) : 0}
                      status={s.failed > 0 ? 'exception' : s.status === 'done' ? 'success' : 'active'} />
            <div style={{ fontSize: 13 }}>{s.done}/{s.total}
              {s.failed > 0 && <span style={{ color: '#ff4d4f' }}>（失败 {s.failed}）</span>}</div>
            {s.failed > 0 && (
              <Button size="small" style={{ marginTop: 6 }}
                      onClick={async () => {
                        const r = await api.post<QueueReply>(`/api/runs/${id}/rerun-failed?node_id=${encodeURIComponent(s.node_id)}`)
                        message.success(r.queued ? `该节点失败行已加入重跑队列（前方 ${r.position ?? 1} 个任务）` : '该节点失败行已重新入队'); await refresh()
                      }}>
                重跑本节点失败行
              </Button>
            )}
          </Card>
        ))}
      </Space>
      <Card size="small" title="运行日志" style={{ marginBottom: 16 }}
            extra={<Button size="small" onClick={downloadLog} disabled={!logs.length}>下载日志</Button>}>
        <div style={{ maxHeight: 220, overflow: 'auto', fontFamily: 'monospace', fontSize: 12 }}>
          {logs.map((l, i) => (
            <div key={i} style={{ color: l.level === 'error' ? '#ff4d4f' : '#555' }}>
              [{l.created_at}] {l.level.toUpperCase()} {l.message}
            </div>
          ))}
          {!logs.length && <span style={{ color: '#999' }}>暂无日志</span>}
        </div>
      </Card>
      {qcMetrics.length > 0 && (
        <Card size="small" title="质检记分卡" style={{ marginBottom: 16 }}>
          <Space wrap size="large">
            {qcMetrics.map((m) => {
              const pct = Math.round(m.first_round_rate * 100)
              return (
                <Card key={m.node_id} size="small" style={{ width: 240 }}>
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>{nodeLabel(m.node_id)}</div>
                  <Statistic title="首轮通过率" value={pct} suffix="%" />
                  <Progress percent={pct} size="small" status={pct === 100 ? 'success' : 'normal'} />
                  <div style={{ fontSize: 13 }}>通过 {m.first_round_pass} / 共 {m.total}
                    （不通过 {m.total - m.first_round_pass}）</div>
                </Card>
              )
            })}
          </Space>
        </Card>
      )}
      {qcFailures.length > 0 && (
        <Card size="small" title={`质检失败样本（${qcFailures.length}）`} style={{ marginBottom: 16 }}
              extra={<Space>
                <Button size="small" onClick={() => void diagnoseRun()} loading={diagnosing}>
                  让红莲诊断本次失败
                </Button>
                <Button size="small" onClick={() => window.open(`/api/runs/${id}/qc-failures.jsonl`)}>
                  下载 jsonl
                </Button>
              </Space>}>
          {failureGroups.length > 0 && (
            <Space wrap style={{ marginBottom: 12 }}>
              {failureGroups.map(([reason, count]) => (
                <Tag key={reason} color="error">{reason} × {count}</Tag>
              ))}
            </Space>
          )}
          <Table rowKey={(_, i) => String(i)} dataSource={qcFailures} size="small"
                 pagination={{ pageSize: 10 }}
                 columns={[
                   { title: 'Trace', dataIndex: 'trace_id', width: 100,
                     render: (v: string | undefined) => traceButton(v) },
                   { title: '样本', dataIndex: 'sample', ellipsis: true,
                     render: (v: object) => JSON.stringify(v) },
                   { title: '各模型理由', dataIndex: 'reasons',
                     render: (rs: QcFailureEntry['reasons']) =>
                       rs.map((r) => `${r.status.toLowerCase() === 'pass' ? '✓' : '✗'} ${r.status}：${r.reason}`).join('；') },
                 ]} />
        </Card>
      )}
      {run && (
        <>
          <Space style={{ marginBottom: 8 }} wrap>
            查看节点：
            {isActive && <Tag color="processing">运行中 · 实时刷新（数据随节点完成陆续出现）</Tag>}
            <Select style={{ width: 260 }} value={node}
                    onChange={(v) => { setSelectedNode(v); setPage(1); setFailedPage(1) }}
                    options={run.graph.nodes.map((n) => ({ value: n.id, label: nodeLabel(n.id) }))} />
            <Select style={{ width: 100 }} value={format} onChange={setFormat}
                    options={['jsonl', 'csv', 'xlsx'].map((f) => ({ value: f, label: f }))} />
            <Button onClick={() => window.open(`/api/runs/${id}/export?format=${format}&node_id=${node}`)}>
              导出
            </Button>
          </Space>
          <Tabs
            items={[
              {
                key: 'preview', label: `结果预览（${rows.total} 行）`,
                children: <Table rowKey={(_, i) => String(i)} dataSource={rows.rows} columns={previewColumns}
                                 pagination={{ current: page, pageSize: 20, total: rows.total, onChange: setPage }}
                                 scroll={{ x: 'max-content' }} size="small" />,
              },
              {
                key: 'failed', label: `失败行（${failed.total}）`,
                children: <Table rowKey="row_idx" dataSource={failed.rows}
                                 columns={[
                                   { title: '行号', dataIndex: 'row_idx', width: 80 },
                                   { title: 'Trace', dataIndex: 'trace_id', width: 100,
                                     render: (v: string | undefined) => traceButton(v) },
                                   { title: '尝试次数', dataIndex: 'attempt', width: 90 },
                                   { title: '错误', dataIndex: 'error' },
                                 ]}
                                 pagination={{ current: failedPage, pageSize: 20, total: failed.total, onChange: setFailedPage }}
                                 size="small" />,
              },
              {
                key: 'modellog', label: `模型对话（${modelLogs.length}）`,
                children: <Table rowKey="id" dataSource={modelLogs} size="small"
                                 pagination={{ pageSize: 10 }}
                                 expandable={{ expandedRowRender: (r) => (
                                   <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>
{JSON.stringify(r.request, null, 2)}{'\n--- 响应 ---\n'}{r.response}</pre>
                                 ) }}
                                 columns={[
                                   { title: '来源', dataIndex: 'source' },
                                   { title: '节点', dataIndex: 'node_id' },
                                   { title: '模型', dataIndex: 'model_name' },
                                 ]} />,
              },
            ]}
          />
        </>
      )}
      <Drawer title={trace ? `Trace ${trace.trace_id}` : 'Trace'}
              width={760}
              open={traceOpen}
              onClose={() => setTraceOpen(false)}>
        {traceLoading && <Spin />}
        {!traceLoading && !trace && <Alert type="warning" message="该运行不支持行级 Trace" />}
        {!traceLoading && trace && (
          <Space orientation="vertical" style={{ width: '100%' }}>
            {trace.parent_trace_id && <Tag>parent：{trace.parent_trace_id}</Tag>}
            <Collapse
              defaultActiveKey={trace.events.map((_, i) => String(i))}
              items={trace.events.map((ev, i) => ({
                key: String(i),
                label: `${nodeLabel(ev.node_id)} · ${ev.status}`,
                children: renderTraceEvent(ev),
              }))}
            />
          </Space>
        )}
      </Drawer>
    </>
  )
}
