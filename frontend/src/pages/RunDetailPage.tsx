import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Button, Card, Popconfirm, Progress, Select, Space, Table, Tabs, Tag, message } from 'antd'
import { useParams } from 'react-router-dom'
import { api, triggerDownload } from '../api/client'
import { useEvents } from '../api/events'
import type { ModelLogEntry, NodeState, QcFailureEntry, RowsPage, RunDetail, RunLogEntry } from '../api/types'
import { formatRunLog } from './runLog'
import { NODE_LABELS } from '../canvas/serialize'
import { STATUS_COLORS, STATUS_LABELS } from './RunsPage'
import { renderCell } from '../utils'

const ACTIVE = ['queued', 'running']

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
  const [modelLogs, setModelLogs] = useState<ModelLogEntry[]>([])
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
    void api.get<ModelLogEntry[]>(`/api/runs/${id}/model-logs`).then(setModelLogs)
  }, [id])
  useEffect(() => { if (run) refreshRows() }, [run?.status, refreshRows])
  useEffect(() => { if (run) refreshAux() }, [run?.status, refreshAux])
  useEffect(() => {  // 运行中每 2 秒实时刷新结果数据（与日志/进度同频）
    if (!run || !ACTIVE.includes(run.status)) return
    const t = setInterval(() => { refreshRows(); refreshAux() }, 2000)
    return () => clearInterval(t)
  }, [run?.status, refreshRows, refreshAux])

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

  return (
    <>
      <Space style={{ marginBottom: 16 }} wrap>
        <h3 style={{ margin: 0 }}>运行 #{run.id}（{run.workflow_name}）</h3>
        <Tag color={STATUS_COLORS[run.status]}>{STATUS_LABELS[run.status] ?? run.status}</Tag>
        <span>Token：{(run.stats.prompt_tokens ?? 0) + (run.stats.completion_tokens ?? 0)}</span>
        {isActive && (
          <Popconfirm title="确认取消？" onConfirm={async () => { await api.post(`/api/runs/${id}/cancel`); message.success('已请求取消'); await refresh() }}>
            <Button danger size="small">取消</Button>
          </Popconfirm>
        )}
        {!isActive && hasFailed && (
          <Button size="small" type="primary"
                  onClick={async () => { await api.post(`/api/runs/${id}/rerun-failed`); message.success('失败行已重新入队'); await refresh() }}>
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
      {qcFailures.length > 0 && (
        <Card size="small" title={`质检失败样本（${qcFailures.length}）`} style={{ marginBottom: 16 }}
              extra={<Button size="small" onClick={() => window.open(`/api/runs/${id}/qc-failures.jsonl`)}>
                下载 jsonl
              </Button>}>
          <Table rowKey={(_, i) => String(i)} dataSource={qcFailures} size="small"
                 pagination={{ pageSize: 10 }}
                 columns={[
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
                key: 'preview', label: `结果预览（${rows.total} 单元）`,
                children: <Table rowKey={(_, i) => String(i)} dataSource={rows.rows} columns={previewColumns}
                                 pagination={{ current: page, pageSize: 20, total: rows.total, onChange: setPage }}
                                 scroll={{ x: 'max-content' }} size="small" />,
              },
              {
                key: 'failed', label: `失败行（${failed.total}）`,
                children: <Table rowKey="row_idx" dataSource={failed.rows}
                                 columns={[
                                   { title: '行号', dataIndex: 'row_idx', width: 80 },
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
    </>
  )
}
