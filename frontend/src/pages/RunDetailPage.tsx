import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Button, Card, Popconfirm, Progress, Select, Space, Table, Tabs, Tag, message } from 'antd'
import { useParams } from 'react-router-dom'
import { api } from '../api/client'
import type { RowsPage, RunDetail, RunLogEntry } from '../api/types'
import { formatRunLog } from './runLog'
import { NODE_LABELS } from '../canvas/serialize'
import { STATUS_COLORS, STATUS_LABELS } from './RunsPage'

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
  const refreshLogs = useCallback(
    () => api.get<RunLogEntry[]>(`/api/runs/${id}/logs`).then(setLogs), [id])
  useEffect(() => { void refreshLogs() }, [refreshLogs])
  useEffect(() => {
    if (!run || !ACTIVE.includes(run.status)) return
    const t = setInterval(() => void refreshLogs(), 2000)
    return () => clearInterval(t)
  }, [run?.status, refreshLogs])
  const downloadLog = () => {
    const blob = new Blob([formatRunLog(logs)], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `run${id}.log`
    a.click()
    URL.revokeObjectURL(url)
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

  const node = selectedNode ?? run?.graph.nodes.find((n) => n.type === 'output')?.id
  const isActive = run ? ACTIVE.includes(run.status) : true

  useEffect(() => {
    if (!run || isActive || !node) return
    void api.get<RowsPage>(`/api/runs/${id}/rows?node_id=${node}&page=${page}&page_size=20`).then(setRows)
    void api.get<RowsPage>(`/api/runs/${id}/rows?node_id=${node}&status=failed&page=${failedPage}&page_size=20`).then(setFailed)
  }, [run?.status, node, page, failedPage, id, isActive])

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
    title: c, dataIndex: c, ellipsis: true,
    render: (v: unknown) => (typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v ?? '')),
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
      </Space>
      {run.error && <Alert type="error" message={run.error} style={{ marginBottom: 16 }} />}
      <Space wrap style={{ marginBottom: 16 }}>
        {orderedStates.map((s) => (
          <Card key={s.node_id} size="small" style={{ width: 230 }}>
            <div>{nodeLabel(s.node_id)}</div>
            <Progress
              percent={s.total ? Math.round((s.done / s.total) * 100) : 0}
              status={s.failed > 0 ? 'exception' : s.status === 'done' ? 'success' : 'active'}
            />
            <div>
              {s.done}/{s.total}
              {s.failed > 0 && <span style={{ color: '#ff4d4f' }}>（失败 {s.failed}）</span>}
            </div>
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
      {!isActive && (
        <>
          <Space style={{ marginBottom: 8 }}>
            查看节点：
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
            ]}
          />
        </>
      )}
    </>
  )
}
