import { useEffect, useState } from 'react'
import { Button, Input, Popconfirm, Space, message } from 'antd'
import ReactMarkdown from 'react-markdown'
import { api } from '../api/client'
import type { PromptDetail, PromptSummary } from '../api/types'
import { useEvents } from '../api/events'
import { extractTplVars } from '../utils'

export function extractVars(body: string): string[] {
  return extractTplVars(body).sort()
}

export function buildPromptPayload(v: { name: string; description: string; body: string }) {
  return { name: v.name.trim(), description: v.description ?? '', body: v.body ?? '' }
}

export default function PromptsPage() {
  const [list, setList] = useState<PromptSummary[]>([])
  const [sel, setSel] = useState<PromptDetail | null>(null)
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [body, setBody] = useState('')
  const [search, setSearch] = useState('')

  const reload = () => api.get<PromptSummary[]>('/api/prompts').then(setList)
  useEffect(() => { void reload() }, [])
  useEvents((e) => { if (e.entity === 'prompt') void reload() })

  const openDetail = async (id: number) => {
    const d = await api.get<PromptDetail>(`/api/prompts/${id}`)
    setSel(d); setName(d.name); setDesc(d.description); setBody(d.current.body)
  }
  const openNew = () => { setSel(null); setName(''); setDesc(''); setBody('') }

  const save = async () => {
    const payload = buildPromptPayload({ name, description: desc, body })
    if (!payload.name) { message.error('请填写名称'); return }
    if (sel) { await api.put(`/api/prompts/${sel.id}`, payload); await openDetail(sel.id) }
    else { const d = await api.post<PromptDetail>('/api/prompts', payload); setSel(d) }
    await reload(); message.success('已保存')
  }
  const remove = async (id: number) => { await api.del(`/api/prompts/${id}`); setSel(null); await reload() }
  const duplicate = async (id: number) => {
    const d = await api.post<PromptDetail>(`/api/prompts/${id}/duplicate`, {})
    await reload(); await openDetail(d.id); message.success('已复制为新提示词')
  }
  const rollback = async (version: number) => {
    if (!sel) return
    await api.post(`/api/prompts/${sel.id}/rollback`, { version }); await openDetail(sel.id)
    message.success(`已回滚到 v${version}`)
  }

  const shown = list.filter((p) => p.name.includes(search))
  const vars = extractVars(body)

  return (
    <div style={{ display: 'flex', gap: 16, height: 'calc(100vh - 32px)' }}>
      <div style={{ width: 260, borderRight: '1px solid #eee', paddingRight: 12, overflow: 'auto' }}>
        <Button type="primary" size="small" onClick={openNew} style={{ marginBottom: 8 }}>新建</Button>
        <Input.Search placeholder="按名称搜索" value={search} onChange={(e) => setSearch(e.target.value)}
                      style={{ marginBottom: 8 }} />
        {shown.length === 0 && <div style={{ color: '#999' }}>还没有提示词，点「新建」</div>}
        {shown.map((p) => (
          <div key={p.id} onClick={() => void openDetail(p.id)}
               style={{ padding: 8, cursor: 'pointer', borderRadius: 4,
                        background: sel?.id === p.id ? '#e6f4ff' : undefined }}>
            <div style={{ fontWeight: 600 }}>{p.name}</div>
            <div style={{ color: '#999', fontSize: 12 }}>
              {p.description || '—'}　v{p.latest_version}　{p.variables.length} 变量
            </div>
          </div>
        ))}
      </div>
      <div style={{ flex: 1, overflow: 'auto' }}>
        <Space style={{ marginBottom: 8 }} wrap>
          <Input placeholder="名称" value={name} onChange={(e) => setName(e.target.value)} style={{ width: 220 }} />
          <Input placeholder="描述" value={desc} onChange={(e) => setDesc(e.target.value)} style={{ width: 280 }} />
          <Button type="primary" onClick={() => void save()}>{sel ? '保存（新版本）' : '保存'}</Button>
          {sel && <Button onClick={() => void duplicate(sel.id)}>复制为新提示词</Button>}
          {sel && (
            <Popconfirm
              title={`确认删除？${sel.used_by.length ? `当前被 ${sel.used_by.length} 个节点引用，删后这些 run 会报错` : ''}`}
              onConfirm={() => void remove(sel.id)}>
              <Button danger>删除</Button>
            </Popconfirm>
          )}
        </Space>
        <div style={{ display: 'flex', gap: 12 }}>
          <div style={{ flex: 1 }}>
            <div style={{ color: '#666', marginBottom: 4 }}>正文（用 {'{{列名}}'} 引用数据列）</div>
            <Input.TextArea rows={18} value={body} onChange={(e) => setBody(e.target.value)} />
            <div style={{ color: '#888', fontSize: 12, marginTop: 4 }}>
              声明变量：{vars.length ? vars.map((v) => `{{${v}}}`).join('、') : '（无）'}
            </div>
          </div>
          <div style={{ flex: 1, border: '1px solid #eee', borderRadius: 4, padding: 12, overflow: 'auto' }}>
            <ReactMarkdown>{body}</ReactMarkdown>
          </div>
        </div>
        {sel && (
          <div style={{ marginTop: 12 }}>
            <div style={{ fontWeight: 600, marginBottom: 4 }}>版本历史</div>
            {sel.versions.slice().reverse().map((v) => (
              <Space key={v.version} style={{ display: 'flex', marginBottom: 4 }}>
                <span>v{v.version}</span>
                <span style={{ color: '#999' }}>{v.created_at.slice(0, 19)}</span>
                <a onClick={() => void rollback(v.version)}>回滚到此版</a>
              </Space>
            ))}
            {sel.used_by.length > 0 && (
              <div style={{ marginTop: 8, color: '#d4380d', fontSize: 12 }}>
                被引用：{sel.used_by.map((u) => `${u.workflow_name}/${u.node_id}(${u.slot})`).join('、')}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
