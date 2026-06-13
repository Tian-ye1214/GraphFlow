import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert, Button, Drawer, Space, message } from 'antd'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Background, Controls, ReactFlow, ReactFlowProvider, addEdge,
  useEdgesState, useNodesState, type Connection, type Edge, type Node,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { api } from '../api/client'
import type { Workflow } from '../api/types'
import NodeConfigForm from '../canvas/forms/NodeConfigForm'
import { nodeTypes } from '../canvas/nodeTypes'
import { NODE_LABELS, fromFlow, toFlow } from '../canvas/serialize'
import { useEvents } from '../api/events'
import { graphFingerprint } from '../canvas/fingerprint'

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
    (c: Connection) => setEdges((eds) => addEdge({ ...c, data: { kind: 'normal' } }, eds)),
    [setEdges],
  )

  const addNode = (type: keyof typeof NODE_LABELS) =>
    setNodes((ns) => [...ns, {
      id: nextId(type, ns), type,
      position: { x: 80 + ns.length * 50, y: 80 + ns.length * 40 },
      data: { config: {} },
    }])

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
    setNodes((ns) => ns.map((n) => (n.id === selectedId ? { ...n, data: { config } } : n)))

  return (
    <div style={{ height: 'calc(100vh - 48px)' }}>
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
      <Drawer
        title={selected ? `${NODE_LABELS[selected.type as keyof typeof NODE_LABELS]}（${selected.id}）` : ''}
        open={!!selected} onClose={() => setSelectedId(null)} width={440} mask={false}
      >
        {selected && (
          <NodeConfigForm
            type={selected.type!}
            config={(selected.data as { config: Record<string, any> }).config}
            onChange={updateConfig}
            workflowId={Number(id)}
            nodeId={selected.id}
          />
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
