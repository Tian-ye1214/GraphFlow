import { useCallback, useEffect, useState } from 'react'
import { Button, Input, Modal, Popconfirm, Space, Table, Tag, message } from 'antd'
import { api } from '../api/client'
import type { AdminUser } from '../api/types'
import { useAuth } from '../stores/auth'

export default function AdminPage() {
  const [users, setUsers] = useState<AdminUser[]>([])
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const actAs = useAuth((s) => s.actAs)
  const reload = useCallback(() => api.get<AdminUser[]>('/api/admin/users').then(setUsers), [])
  useEffect(() => { void reload() }, [reload])

  const create = async () => {
    try {
      await api.post('/api/admin/users', { username: newName.trim() })
      setCreating(false)
      setNewName('')
      message.success('已创建')
      await reload()
    } catch (e) {
      message.error((e as Error).message)
    }
  }

  return (
    <>
      <Space style={{ marginBottom: 16 }}>
        <h3 style={{ margin: 0 }}>租户管理</h3>
        <Button type="primary" size="small" onClick={() => setCreating(true)}>新建用户</Button>
      </Space>
      <Table
        rowKey="id"
        dataSource={users}
        columns={[
          { title: 'ID', dataIndex: 'id', width: 70 },
          { title: '用户名', dataIndex: 'username' },
          { title: '显示名', dataIndex: 'display_name' },
          { title: '管理员', dataIndex: 'is_admin', render: (v: boolean) => (v ? <Tag color="gold">admin</Tag> : null) },
          { title: '创建时间', dataIndex: 'created_at' },
          {
            title: '操作', key: 'act',
            render: (_: unknown, u: AdminUser) => (
              <Space>
                <Button size="small" onClick={async () => {
                  try { await actAs(u.id); message.success(`已切换为 ${u.username}`) }
                  catch (e) { message.error((e as Error).message) }
                }}>
                  以此身份操作
                </Button>
                <Popconfirm title="删除该用户及其全部资源？" onConfirm={async () => {
                  try { await api.del(`/api/admin/users/${u.id}`); message.success('已删除'); await reload() }
                  catch (e) { message.error((e as Error).message) }
                }}>
                  <Button danger size="small">删除</Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      <Modal open={creating} title="新建用户" onCancel={() => setCreating(false)} onOk={() => void create()}>
        <Input placeholder="用户名" value={newName} onChange={(e) => setNewName(e.target.value)} />
      </Modal>
    </>
  )
}
