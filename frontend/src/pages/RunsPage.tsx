import { useEffect, useState } from 'react'
import { Table, Tag } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../api/client'
import type { Run } from '../api/types'

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

  useEffect(() => {
    void api.get<Run[]>(`/api/runs${wfId ? `?workflow_id=${wfId}` : ''}`).then(setList)
  }, [wfId])

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
      ]}
    />
  )
}
