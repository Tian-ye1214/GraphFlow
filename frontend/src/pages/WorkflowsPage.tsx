import { useEffect, useRef, useState } from 'react'
import { Button, Input, Modal, Popconfirm, Space, Table, message } from 'antd'
import { Link } from 'react-router-dom'
import { api, ApiError, downloadWorkflowPackage } from '../api/client'
import type { ImportResult, WorkflowSummary } from '../api/types'
import { useEvents } from '../api/events'

export default function WorkflowsPage() {
  const [list, setList] = useState<WorkflowSummary[]>([])
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [result, setResult] = useState<ImportResult | null>(null)
  const fileRef = useRef<HTMLInputElement>(null)

  const reload = () => api.get<WorkflowSummary[]>('/api/workflows').then(setList)
  useEffect(() => {
    void reload()
  }, [])

  useEvents((e) => {
    if (e.entity === 'workflow') void reload()
  })

  const create = async () => {
    if (!name.trim()) return
    await api.post('/api/workflows', { name: name.trim() })
    setCreating(false)
    setName('')
    await reload()
  }

  const exportWf = async (id: number, wfName: string) => {
    try {
      await downloadWorkflowPackage(id, wfName)
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '导出失败')
    }
  }

  const onPickFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = '' // 复位，同一文件可再次选择
    if (!file) return
    try {
      const fd = new FormData()
      fd.append('file', file)
      const res = await api.postForm<ImportResult>('/api/workflows/import', fd)
      setResult(res)
      await reload()
    } catch (err) {
      message.error(err instanceof ApiError ? err.message : '导入失败')
    }
  }

  return (
    <>
      <Space style={{ marginBottom: 16 }}>
        <Button type="primary" onClick={() => setCreating(true)}>新建工作流</Button>
        <Button onClick={() => fileRef.current?.click()}>导入 .gfpkg</Button>
        <input ref={fileRef} type="file" accept=".gfpkg,.zip" style={{ display: 'none' }}
               onChange={(e) => void onPickFile(e)} />
      </Space>
      <Table
        rowKey="id"
        dataSource={list}
        columns={[
          { title: '名称', dataIndex: 'name', render: (v, wf) => <Link to={`/workflows/${wf.id}/canvas`}>{v}</Link> },
          { title: '更新时间', dataIndex: 'updated_at' },
          {
            title: '操作',
            render: (_, wf) => (
              <Space>
                <Link to={`/workflows/${wf.id}/canvas`}>编辑</Link>
                <Link to={`/runs?workflow_id=${wf.id}`}>运行记录</Link>
                <a onClick={() => void exportWf(wf.id, wf.name)}>导出</a>
                <Popconfirm title="确认删除？" onConfirm={async () => { await api.del(`/api/workflows/${wf.id}`); await reload() }}>
                  <a>删除</a>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      <Modal title="新建工作流" open={creating} onOk={() => void create()} onCancel={() => setCreating(false)}>
        <Input placeholder="工作流名称" value={name} onChange={(e) => setName(e.target.value)} onPressEnter={() => void create()} />
      </Modal>
      <Modal title="导入完成" open={result !== null} footer={null} onCancel={() => setResult(null)}>
        {result && <ImportReportView result={result} />}
      </Modal>
    </>
  )
}

function ImportReportView({ result }: { result: ImportResult }) {
  const r = result.report
  const join = (xs: { name: string }[]) => xs.map((x) => x.name).join('、')
  return (
    <div>
      <p>已导入为链路「{result.workflow.name}」（#{result.workflow.id}）</p>
      {(r.models_reused.length > 0 || r.prompts_reused.length > 0 || r.datasets_reused.length > 0) && (
        <p>复用既有：{[
          r.models_reused.length ? `模型 ${join(r.models_reused)}` : '',
          r.prompts_reused.length ? `提示词 ${join(r.prompts_reused)}` : '',
          r.datasets_reused.length ? `数据集 ${join(r.datasets_reused)}` : '',
        ].filter(Boolean).join('；')}</p>
      )}
      {(r.models_created.length > 0 || r.prompts_created.length > 0 || r.datasets_created.length > 0) && (
        <p>新建：{[
          r.models_created.length ? `模型 ${join(r.models_created)}` : '',
          r.prompts_created.length ? `提示词 ${join(r.prompts_created)}` : '',
          r.datasets_created.length ? `数据集 ${join(r.datasets_created)}` : '',
        ].filter(Boolean).join('；')}</p>
      )}
      {r.models_need_key.length > 0 && (
        <p style={{ color: '#d46b08' }}>⚠ 待回填密钥的模型：{join(r.models_need_key)}（跑前请到模型配置补填）</p>
      )}
      {r.headers_need_refill.length > 0 && (
        <p style={{ color: '#d46b08' }}>⚠ 待回填的 http 头：
          {r.headers_need_refill.map((x) => `${x.node_id}.${x.header}`).join('、')}</p>
      )}
      {r.draft_unresolved.length > 0 && (
        <p style={{ color: '#cf1322' }}>⚠ 有引用无法解析、已降级草稿：
          {r.draft_unresolved.map((x) => `${x.node_id}(${x.kind})`).join('、')}</p>
      )}
    </div>
  )
}
