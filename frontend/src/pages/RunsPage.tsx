import { useCallback, useEffect, useState } from 'react'
import { Button, Popconfirm, Table, Tag, message } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import type { Run } from '../api/types'
import { useEvents } from '../api/events'

export const STATUS_COLORS: Record<string, string> = {
  queued: 'default', running: 'processing', completed: 'success',
  failed: 'error', cancelled: 'warning',
}
export const STATUS_LABELS: Record<string, string> = {
  queued: '排队中', running: '运行中', completed: '已完成', failed: '失败', cancelled: '已取消',
}

export default function RunsPage() {
  const [list, setList] = useState<Run[]>([])
  const [params] = useSearchParams()
  const wfId = params.get('workflow_id')

  const reload = useCallback(
    () => api.get<Run[]>(`/api/runs${wfId ? `?workflow_id=${wfId}` : ''}`).then(setList),
    [wfId],
  )

  useEffect(() => {
    void reload()
  }, [reload])

  useEvents((e) => {
    if (e.entity === 'run') void reload()
  })

  return (
    <Table
      rowKey="id"
      dataSource={list}
      columns={[
        { title: 'ID', dataIndex: 'id', render: (v) => <Link to={`/runs/${v}`}>#{v}</Link> },
        { title: '工作流', dataIndex: 'workflow_name' },
        { title: '状态', dataIndex: 'status', render: (s: string) => <Tag color={STATUS_COLORS[s]}>{STATUS_LABELS[s] ?? s}</Tag> },
        { title: 'Token 用量', dataIndex: 'stats', render: (s: Run['stats']) => (s.prompt_tokens ?? 0) + (s.completion_tokens ?? 0) },
        { title: '创建时间', dataIndex: 'created_at' },
        { title: '结束时间', dataIndex: 'finished_at' },
        {
          title: '操作', key: 'act',
          render: (_: unknown, r: Run) => (
            <Popconfirm title="删除该运行及其全部数据？"
                        onConfirm={async () => { await api.del(`/api/runs/${r.id}`); message.success('已删除'); await reload() }}>
              <Button danger size="small" disabled={['queued', 'running'].includes(r.status)}>删除</Button>
            </Popconfirm>
          ),
        },
      ]}
    />
  )
}
