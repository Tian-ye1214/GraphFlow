import { useEffect, useState } from 'react'
import { Button, Form, Input, InputNumber, Modal, Popconfirm, Space, Table, message } from 'antd'
import { api } from '../api/client'
import type { ModelConfig } from '../api/types'
import { useEvents } from '../api/events'

interface FormValues {
  name: string; model_name: string; base_url: string; api_key?: string
  temperature?: number; top_p?: number; max_tokens?: number
}

export default function ModelsPage() {
  const [list, setList] = useState<ModelConfig[]>([])
  const [editing, setEditing] = useState<ModelConfig | null | 'new'>(null)
  const [form] = Form.useForm<FormValues>()

  const reload = () => api.get<ModelConfig[]>('/api/models').then(setList)
  useEffect(() => {
    void reload()
  }, [])

  useEvents((e) => {
    if (e.entity === 'model') void reload()
  })

  const openEdit = (mc: ModelConfig | 'new') => {
    setEditing(mc)
    if (mc === 'new') form.resetFields()
    else form.setFieldsValue({ ...mc, ...(mc.default_params as object) })
  }

  const save = async () => {
    const v = await form.validateFields()
    const payload = {
      name: v.name, model_name: v.model_name, base_url: v.base_url, api_key: v.api_key ?? '',
      default_params: { temperature: v.temperature, top_p: v.top_p, max_tokens: v.max_tokens },
    }
    if (editing === 'new') await api.post('/api/models', payload)
    else await api.put(`/api/models/${(editing as ModelConfig).id}`, payload)
    setEditing(null)
    await reload()
  }

  const testConn = async (id: number) => {
    const r = await api.post<{ ok: boolean; reply?: string; error?: string }>(`/api/models/${id}/test`)
    if (r.ok) message.success(`连通正常：${r.reply}`)
    else message.error(`连接失败：${r.error}`)
  }

  return (
    <>
      <Button type="primary" onClick={() => openEdit('new')} style={{ marginBottom: 16 }}>
        新增模型
      </Button>
      <Table
        rowKey="id"
        dataSource={list}
        pagination={false}
        columns={[
          { title: '名称', dataIndex: 'name' },
          { title: '模型 ID', dataIndex: 'model_name' },
          { title: 'Base URL', dataIndex: 'base_url' },
          { title: 'Key', dataIndex: 'api_key_set', render: (v: boolean) => (v ? '已配置' : '未配置') },
          {
            title: '操作',
            render: (_, mc) => (
              <Space>
                <a onClick={() => void testConn(mc.id)}>测试</a>
                <a onClick={() => openEdit(mc)}>编辑</a>
                <Popconfirm title="确认删除？" onConfirm={async () => { await api.del(`/api/models/${mc.id}`); await reload() }}>
                  <a>删除</a>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      <Modal title={editing === 'new' ? '新增模型' : '编辑模型'} open={editing !== null}
             onOk={() => void save()} onCancel={() => setEditing(null)} destroyOnClose>
        <Form form={form} layout="vertical">
          <Form.Item name="name" label="显示名称" rules={[{ required: true }]}>
            <Input placeholder="如：内网 Qwen" />
          </Form.Item>
          <Form.Item name="model_name" label="模型 ID" rules={[{ required: true }]}>
            <Input placeholder="如：qwen-max" />
          </Form.Item>
          <Form.Item name="base_url" label="Base URL" rules={[{ required: true }]}>
            <Input placeholder="如：http://10.0.0.1:8000/v1" />
          </Form.Item>
          <Form.Item name="api_key" label="API Key" extra="编辑时留空表示不修改">
            <Input.Password placeholder="sk-..." />
          </Form.Item>
          <Space>
            <Form.Item name="temperature" label="temperature">
              <InputNumber min={0} max={2} step={0.1} />
            </Form.Item>
            <Form.Item name="top_p" label="top_p">
              <InputNumber min={0} max={1} step={0.05} />
            </Form.Item>
            <Form.Item name="max_tokens" label="max_tokens">
              <InputNumber min={1} />
            </Form.Item>
          </Space>
        </Form>
      </Modal>
    </>
  )
}
