import { useEffect, useState } from 'react'
import { Button, Input, Modal, Popconfirm, Space, Table } from 'antd'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type { WorkflowSummary } from '../api/types'
import { useEvents } from '../api/events'

export default function WorkflowsPage() {
  const [list, setList] = useState<WorkflowSummary[]>([])
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')

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

  return (
    <>
      <Button type="primary" onClick={() => setCreating(true)} style={{ marginBottom: 16 }}>
        新建工作流
      </Button>
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
    </>
  )
}
