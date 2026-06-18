import { useEffect, useState } from 'react'
import { Card, Select, Space, Table, Tag } from 'antd'
import { api } from '../api/client'
import type { ModelLogEntry } from '../api/types'

const SOURCES = ['', 'synth', 'qc', 'redlotus', 'assistant', 'codegen', 'compactor']

export default function ModelLogsPage() {
  const [source, setSource] = useState('')
  const [rows, setRows] = useState<ModelLogEntry[]>([])
  useEffect(() => {
    void api.get<ModelLogEntry[]>(`/api/model-logs${source ? `?source=${source}` : ''}`).then(setRows)
  }, [source])
  return (
    <>
      <Space style={{ marginBottom: 12 }}>
        <span>来源</span>
        <Select style={{ width: 160 }} value={source} onChange={setSource}
                options={SOURCES.map((s) => ({ value: s, label: s || '全部' }))} />
      </Space>
      <Table rowKey="id" dataSource={rows} size="small" pagination={{ pageSize: 20 }}
             expandable={{ expandedRowRender: (r) => (
               <Card size="small">
                 <div style={{ fontWeight: 600 }}>请求</div>
                 <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>{JSON.stringify(r.request, null, 2)}</pre>
                 <div style={{ fontWeight: 600 }}>响应</div>
                 <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>{r.response}</pre>
               </Card>
             ) }}
             columns={[
               { title: '来源', dataIndex: 'source', render: (s: string) => <Tag>{s}</Tag> },
               { title: '节点', dataIndex: 'node_id' },
               { title: 'run', dataIndex: 'run_id' },
               { title: '模型', dataIndex: 'model_name' },
               { title: 'tokens', render: (_: unknown, r: ModelLogEntry) => r.prompt_tokens + r.completion_tokens },
               { title: '时间', dataIndex: 'created_at' },
             ]} />
    </>
  )
}
