import { useEffect, useState } from 'react'
import { Button, Form, Input, InputNumber, Modal, Popconfirm, Select, Space, Table, message } from 'antd'
import { api } from '../api/client'
import type { ModelConfig } from '../api/types'
import { useEvents } from '../api/events'

type ModelProvider = 'openai' | 'azure'

export interface FormValues {
  name: string; model_name: string; base_url: string; provider?: ModelProvider
  api_version?: string; api_key?: string; temperature?: number; top_p?: number; max_tokens?: number
}

export function endpointLabelForProvider(provider?: ModelProvider) {
  return provider === 'azure' ? 'Azure Endpoint' : 'Base URL'
}

export function buildModelPayload(v: FormValues) {
  const provider = v.provider ?? 'openai'
  return {
    name: v.name, model_name: v.model_name, base_url: v.base_url,
    provider, api_version: provider === 'azure' ? (v.api_version ?? '').trim() : '',
    api_key: v.api_key ?? '',
    default_params: { temperature: v.temperature, top_p: v.top_p, max_tokens: v.max_tokens },
  }
}

export default function ModelsPage() {
  const [list, setList] = useState<ModelConfig[]>([])
  const [editing, setEditing] = useState<ModelConfig | null | 'new'>(null)
  const [form] = Form.useForm<FormValues>()
  const provider = Form.useWatch('provider', form) ?? 'openai'

  const reload = () => api.get<ModelConfig[]>('/api/models').then(setList)
  useEffect(() => {
    void reload()
  }, [])

  useEvents((e) => {
    if (e.entity === 'model') void reload()
  })

  const openEdit = (mc: ModelConfig | 'new') => {
    setEditing(mc)
    if (mc === 'new') {
      form.resetFields()
      form.setFieldsValue({ provider: 'openai' })
    } else form.setFieldsValue({
      ...mc, ...(mc.default_params as object),
      provider: mc.provider ?? 'openai', api_version: mc.api_version ?? '',
    })
  }

  const save = async () => {
    const v = await form.validateFields()
    const payload = buildModelPayload(v)
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
          { title: 'Provider', dataIndex: 'provider', render: (v: ModelProvider) => (v === 'azure' ? 'Azure' : 'OpenAI') },
          { title: 'Base URL', dataIndex: 'base_url' },
          { title: 'API Version', dataIndex: 'api_version', render: (_: string, mc) => (mc.provider === 'azure' ? mc.api_version || '-' : '-') },
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
          <Form.Item name="provider" label="Provider" rules={[{ required: true }]} initialValue="openai">
            <Select
              options={[
                { value: 'openai', label: 'OpenAI Compatible' },
                { value: 'azure', label: 'Azure OpenAI' },
              ]}
              onChange={(value: ModelProvider) => { if (value === 'openai') form.setFieldValue('api_version', '') }}
            />
          </Form.Item>
          <Form.Item name="base_url" label={endpointLabelForProvider(provider)} rules={[{ required: true }]}>
            <Input placeholder={provider === 'azure'
              ? 'https://your-resource.openai.azure.com'
              : 'http://10.0.0.1:8000/v1'} />
          </Form.Item>
          {provider === 'azure' && (
            <Form.Item name="api_version" label="API Version" rules={[{ required: true }]}>
              <Input placeholder="2024-03-01-preview" />
            </Form.Item>
          )}
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
