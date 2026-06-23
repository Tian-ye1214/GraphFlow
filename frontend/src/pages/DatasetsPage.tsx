import { useEffect, useState } from 'react'
import { Badge, Button, Drawer, Dropdown, Empty, Popconfirm, Space, Table, Tag, Tooltip, Upload, message } from 'antd'
import { DownloadOutlined, ExperimentOutlined, UploadOutlined } from '@ant-design/icons'
import { api, downloadDatasetExport } from '../api/client'
import type { Dataset, RowsPage } from '../api/types'
import { useEvents } from '../api/events'
import { renderCell } from '../utils'

const EXPORT_FORMATS = [
  { key: 'original', label: '原始格式' },
  { key: 'jsonl', label: 'JSONL' },
  { key: 'csv', label: 'CSV' },
  { key: 'xlsx', label: 'Excel (xlsx)' },
]

function StatusCell({ ds }: { ds: Dataset }) {
  if (ds.status === 'failed') {
    return (
      <Tooltip title={ds.import_error || '导入失败'}>
        <Tag color="red">失败</Tag>
      </Tooltip>
    )
  }
  if (ds.status === 'importing') {
    return <Badge status="processing" text={`导入中…（已导入 ${ds.imported_rows} 行）`} />
  }
  return <Badge status="success" text="就绪" />
}

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
      message.success('已开始导入，可在列表查看进度')
      await reload()
    } catch (e) {
      message.error(String((e as Error).message))
    }
  }

  const loadSample = async () => {
    try {
      await api.post('/api/datasets/sample')
      message.success('已加载示例数据集')
      await reload()
    } catch (e) {
      message.error(String((e as Error).message))
    }
  }

  const uploadBtn = (
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
  )

  return (
    <>
      <Space style={{ marginBottom: 16 }}>
        {uploadBtn}
        <Button icon={<ExperimentOutlined />} onClick={() => void loadSample()}>加载示例数据集</Button>
      </Space>
      <Table
        rowKey="id"
        dataSource={list}
        locale={{
          emptyText: (
            <Empty description="还没有数据集">
              <Space>
                {uploadBtn}
                <Button icon={<ExperimentOutlined />} onClick={() => void loadSample()}>加载示例数据集</Button>
              </Space>
            </Empty>
          ),
        }}
        columns={[
          { title: '名称', dataIndex: 'name' },
          { title: '来源', dataIndex: 'source', render: (v: string) => (v === 'run' ? '运行结果' : '上传') },
          { title: '状态', render: (_, ds) => <StatusCell ds={ds} /> },
          { title: '行数', dataIndex: 'row_count' },
          { title: '列', dataIndex: 'columns', render: (cols: string[]) => cols.join(', ') },
          {
            title: '操作',
            render: (_, ds) => (
              <Space>
                <a
                  onClick={ds.status === 'ready' ? () => { setPage(1); setPreview(ds) } : undefined}
                  style={ds.status === 'ready' ? undefined : { color: '#bbb', cursor: 'not-allowed' }}
                >预览</a>
                {ds.status === 'ready' ? (
                  <Dropdown
                    menu={{ items: EXPORT_FORMATS, onClick: ({ key }) => downloadDatasetExport(ds.id, key) }}
                  >
                    <a><DownloadOutlined /> 导出</a>
                  </Dropdown>
                ) : (
                  <span style={{ color: '#bbb' }}><DownloadOutlined /> 导出</span>
                )}
                <Popconfirm title="确认删除？" onConfirm={async () => { await api.del(`/api/datasets/${ds.id}`); await reload() }}>
                  <a>删除</a>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      <Drawer
        title={preview?.name}
        open={!!preview}
        onClose={() => setPreview(null)}
        width="60%"
        extra={preview && (
          <Dropdown menu={{ items: EXPORT_FORMATS, onClick: ({ key }) => downloadDatasetExport(preview.id, key) }}>
            <Button icon={<DownloadOutlined />}>导出</Button>
          </Dropdown>
        )}
      >
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
