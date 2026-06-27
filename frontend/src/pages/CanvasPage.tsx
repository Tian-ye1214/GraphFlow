import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Drawer, Input, Space, message } from 'antd'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Background, Controls, ReactFlow, ReactFlowProvider, addEdge,
  useEdgesState, useNodesState, useReactFlow, type Connection, type Edge, type Node,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { api } from '../api/client'
import type { Workflow } from '../api/types'
import NodeConfigForm from '../canvas/forms/NodeConfigForm'
import { nodeTypes } from '../canvas/nodeTypes'
import { NODE_LABELS, RESCAN_EDGE, fromFlow, toFlow, displayName, stripPrompt, copyLabel } from '../canvas/serialize'
import { useEvents } from '../api/events'
import { graphFingerprint } from '../canvas/fingerprint'
import { nodeDropPosition } from '../canvas/layout'

function nextId(type: string, existing: Node[]): string {
  for (let i = 1; ; i++) {
    const id = `${type}_${i}`
    if (!existing.some((n) => n.id === id)) return id
  }
}

function Canvas() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [wf, setWf] = useState<Workflow | null>(null)
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const selected = nodes.find((n) => n.id === selectedId) ?? null
  const [cliChanged, setCliChanged] = useState(false)
  const baseline = useRef('')
  const rf = useReactFlow()
  const flowWrap = useRef<HTMLDivElement>(null)
  const clip = useRef<{ type: string; config: Record<string, any>; label?: string; position: { x: number; y: number } } | null>(null)
  const pasteSeq = useRef(0)

  const load = useCallback(async () => {
    const w = await api.get<Workflow>(`/api/workflows/${id}`)
    setWf(w)
    const f = toFlow(w.graph)
    setNodes(f.nodes)
    setEdges(f.edges)
    baseline.current = graphFingerprint(w.graph)
    setCliChanged(false)
  }, [id, setNodes, setEdges])

  useEffect(() => {
    void load()
  }, [load])

  useEvents((e) => {
    if (e.entity !== 'workflow' || e.id !== Number(id)) return
    if (graphFingerprint(fromFlow(nodes, edges)) === baseline.current) void load()
    else setCliChanged(true)
  })

  const onConnect = useCallback(
    (c: Connection) => {
      const rescan = c.sourceHandle === 'rescan'
      setEdges((eds) => addEdge(
        { ...c, data: { kind: rescan ? 'rescan' : 'normal' }, ...(rescan ? RESCAN_EDGE : {}) }, eds))
    },
    [setEdges],
  )

  // 节点/连线变动后防抖自动保存：指纹与 baseline 不同才真正 PUT（初次 load 设了 baseline，故不触发）
  useEffect(() => {
    const t = setTimeout(() => {
      const graph = fromFlow(nodes, edges)
      if (graphFingerprint(graph) === baseline.current) return
      baseline.current = graphFingerprint(graph)
      void api.put(`/api/workflows/${id}`, { graph })
    }, 800)
    return () => clearTimeout(t)
  }, [nodes, edges, id])

  // Ctrl/⌘+C 复制选中节点、Ctrl/⌘+V 粘贴副本（去提示词、显示名自增、错位放置）。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return
      const t = e.target as HTMLElement | null
      if (t?.closest('input, textarea, [contenteditable="true"]')) return   // 放行输入控件内的文本复制
      const key = e.key.toLowerCase()
      if (key === 'c' && selected) {
        clip.current = {
          type: selected.type!,
          config: (selected.data as { config?: Record<string, any> }).config ?? {},
          label: (selected.data as { label?: string }).label,
          position: selected.position,
        }
        pasteSeq.current = 0
      } else if (key === 'v' && clip.current) {
        e.preventDefault()
        const c = clip.current
        pasteSeq.current += 1
        const off = 40 * pasteSeq.current
        const existing = new Set(nodes.map((n) => displayName((n.data as { label?: string })?.label, n.id)))
        const id = nextId(c.type, nodes)
        const label = c.label && c.label.trim() ? copyLabel(c.label, existing) : undefined
        setNodes((ns) => [...ns, {
          id, type: c.type,
          position: { x: c.position.x + off, y: c.position.y + off },
          data: { config: stripPrompt(c.config), ...(label ? { label } : {}) },
        }])
        setSelectedId(id)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [nodes, selected, setNodes])

  const addNode = (type: keyof typeof NODE_LABELS) =>
    setNodes((ns) => {
      const rect = flowWrap.current?.getBoundingClientRect()
      const center = rect
        ? rf.screenToFlowPosition({ x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 })
        : { x: 200, y: 200 }
      return [...ns, {
        id: nextId(type, ns), type,
        position: nodeDropPosition(center, ns.length),
        data: { config: {} },
      }]
    })

  const save = async () => {
    const graph = fromFlow(nodes, edges)
    baseline.current = graphFingerprint(graph)
    await api.put(`/api/workflows/${id}`, { graph })
    message.success('已保存')
  }

  const run = async () => {
    await save()
    try {
      const r = await api.post<{ id: number }>('/api/runs', { workflow_id: Number(id) })
      navigate(`/runs/${r.id}`)
    } catch (e) {
      message.error(String((e as Error).message))
    }
  }

  const updateConfig = (config: Record<string, any>) =>
    setNodes((ns) => ns.map((n) => (n.id === selectedId ? { ...n, data: { ...n.data, config } } : n)))

  const updateLabel = (label: string) =>
    setNodes((ns) => ns.map((n) => (n.id === selectedId ? { ...n, data: { ...n.data, label } } : n)))

  return (
    <div ref={flowWrap} style={{ height: 'calc(100vh - 48px)', position: 'relative' }}>
      {cliChanged && (
        <Alert
          type="info" showIcon style={{ marginBottom: 8 }}
          message="工作流已被 CLI 修改"
          action={<Button size="small" type="primary" onClick={() => void load()}>加载最新版本</Button>}
        />
      )}
      <Space style={{ marginBottom: 8 }}>
        <b>{wf?.name}</b>
        {(Object.keys(NODE_LABELS) as (keyof typeof NODE_LABELS)[]).map((t) => (
          <Button key={t} size="small" onClick={() => addNode(t)}>+ {NODE_LABELS[t]}</Button>
        ))}
        <Button size="small" type="primary" onClick={() => void save()}>保存</Button>
        <Button size="small" type="primary" danger onClick={() => void run()}>运行</Button>
      </Space>
      <ReactFlow
        nodes={nodes} edges={edges} nodeTypes={nodeTypes}
        onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={onConnect}
        onNodeClick={(_, n) => setSelectedId(n.id)} onPaneClick={() => setSelectedId(null)}
        fitView deleteKeyCode={['Backspace', 'Delete']}
      >
        <Background />
        <Controls />
      </ReactFlow>
      {nodes.length === 0 && (
        <div style={{
          position: 'absolute', top: '46%', left: '50%', transform: 'translate(-50%, -50%)',
          textAlign: 'center', color: '#8c8c8c', pointerEvents: 'none', zIndex: 5, maxWidth: 480,
        }}>
          <div style={{ fontSize: 16, fontWeight: 600, marginBottom: 10 }}>空白画布 · 从这里开始</div>
          <div style={{ lineHeight: 2 }}>
            ① 上方点「+ 输入」，在右侧选数据集<br />
            ② 点「+ LLM 合成」，写提示词、选模型<br />
            ③ 点「+ 输出」<br />
            ④ 依次连线 输入 → 合成 → 输出，再点「运行」
          </div>
          <div style={{ marginTop: 10, fontSize: 12 }}>提示：质检节点的「回扫」边需从质检节点连出</div>
        </div>
      )}
      <Drawer
        title={selected
          ? `${NODE_LABELS[selected.type as keyof typeof NODE_LABELS]}（${displayName((selected.data as any)?.label, selected.id)}）`
          : ''}
        open={!!selected} onClose={() => setSelectedId(null)} width={440} mask={false}
      >
        {selected && (
          <>
            <div style={{ marginBottom: 12 }}>
              <div style={{ color: '#666', marginBottom: 4 }}>显示名（仅画布展示，不改节点 id <code>{selected.id}</code>）</div>
              <Input value={(selected.data as { label?: string })?.label ?? ''}
                     placeholder={selected.id}
                     onChange={(e) => updateLabel(e.target.value)} />
            </div>
            <NodeConfigForm
              type={selected.type!}
              config={(selected.data as { config: Record<string, any> }).config}
              onChange={updateConfig}
              workflowId={Number(id)}
              nodeId={selected.id}
            />
          </>
        )}
      </Drawer>
    </div>
  )
}

export default function CanvasPage() {
  return (
    <ReactFlowProvider>
      <Canvas />
    </ReactFlowProvider>
  )
}
