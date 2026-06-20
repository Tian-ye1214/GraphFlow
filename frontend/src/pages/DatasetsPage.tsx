import { useEffect, useState } from 'react'
import { Button, Drawer, Popconfirm, Space, Table, Upload, message } from 'antd'
import { UploadOutlined } from '@ant-design/icons'
import { api } from '../api/client'
import type { Dataset, RowsPage } from '../api/types'
import { useEvents } from '../api/events'
import { renderCell } from '../utils'

export default function DatasetsPage() {
  const [list, setList] = useState<Dataset[]>([])
  const [preview, setPreview] = useState<Dataset | null>(null)
  const [page, setPage] = useState(1)
  const [rows, setRows] = useState<RowsPage>({ total: 0, rows: [] })

  const reload = () => api.get<Dataset[]>('/api/datasets').then(setList)
  useEffect(() => {
    void reload()
  }, [])

  useEvents((e) => {
    if (e.entity === 'dataset') void reload()
  })

  useEffect(() => {
    if (preview) void api.get<RowsPage>(`/api/datasets/${preview.id}/rows?page=${page}&page_size=20`).then(setRows)
  }, [preview, page])

  const doUpload = async (files: File[]) => {
    const form = new FormData()
    files.forEach((f) => form.append('files', f))
    try {
      await api.postForm('/api/datasets/upload', form)
      message.success('上传成功')
      await reload()
    } catch (e) {
      message.error(String((e as Error).message))
    }
  }

  return (
    <>
      <Space style={{ marginBottom: 16 }}>
        <Upload
          multiple
          accept=".jsonl,.json,.csv,.xlsx,.xls"
          beforeUpload={(_, fileList) => {
            void doUpload(fileList as unknown as File[])
            return false
          }}
          showUploadList={false}
        >
          <Button type="primary" icon={<UploadOutlined />}>上传数据集（JSONL / JSON / CSV / Excel，可多选）</Button>
        </Upload>
      </Space>
      <Table
        rowKey="id"
        dataSource={list}
        columns={[
          { title: '名称', dataIndex: 'name' },
          { title: '来源', dataIndex: 'source', render: (v: string) => (v === 'run' ? '运行结果' : '上传') },
          { title: '行数', dataIndex: 'row_count' },
          { title: '列', dataIndex: 'columns', render: (cols: string[]) => cols.join(', ') },
          {
            title: '操作',
            render: (_, ds) => (
              <Space>
                <a onClick={() => { setPage(1); setPreview(ds) }}>预览</a>
                <Popconfirm title="确认删除？" onConfirm={async () => { await api.del(`/api/datasets/${ds.id}`); await reload() }}>
                  <a>删除</a>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      <Drawer title={preview?.name} open={!!preview} onClose={() => setPreview(null)} width="60%">
        <Table
          rowKey={(_, i) => String(i)}
          dataSource={rows.rows}
          columns={(preview?.columns ?? []).map((c) => ({
            title: c, dataIndex: c, ellipsis: true, render: renderCell,
          }))}
          pagination={{ current: page, pageSize: 20, total: rows.total, onChange: setPage }}
          scroll={{ x: 'max-content' }}
        />
      </Drawer>
    </>
  )
}
