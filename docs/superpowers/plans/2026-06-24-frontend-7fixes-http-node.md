# 前端 7 项修复 + HTTP 节点重构 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 7 项前端体验问题并重构 HTTP 取数节点（接口/params/body+格式/headers + 节点助手）。

**Architecture:** ①②③④⑤为纯前端改动；⑦含前后端。①节点显示名随 `graph_json` verbatim 往返、后端零改动。
节点助手会话镜像 AgentDrawer 既有会话模式。HTTP 节点保留 `url`→`endpoint` 向后兼容。

**Tech Stack:** React 19 + TypeScript + Ant Design v6 + `@xyflow/react` v12.11 + react-markdown + vitest；后端 FastAPI + httpx + pytest。

## Global Constraints

- KISS / YAGNI / DRY / TDD；复用优先；**不引入任何 dry_run/假运行**。
- 不推 origin；提交信息**不带 claude / 不加 Co-Authored-By 尾注**；用中文。
- 后端测试：`cd backend && python -m pytest <file> -v`（项目用 uv，`uv run pytest` 亦可）。基线全绿（~715）。
- 前端测试：`cd frontend && npm test`（vitest run）；类型检查：`cd frontend && npx tsc -b`。基线全绿 + tsc clean。
- 节点 `id` 不可变；`label` 仅 UI 显示，引擎/血缘/产物全程忽略。
- 应用运行期代码可用 `crypto.randomUUID()`（仅 Workflow 脚本里禁 `Date.now/Math.random`，此处不受限）。
- HTTP body 仅 `json`/`raw`/`form`（设 Content-Type），不做 multipart；方法仍 GET/POST。

---

## Task 1: 节点显示名 round-trip（types + serialize + displayName 助手）

**Files:**
- Modify: `frontend/src/api/types.ts`（`GraphNode` 加 `label?`）
- Modify: `frontend/src/canvas/serialize.ts`（toFlow/fromFlow 透传 label；导出 `displayName`）
- Test: `frontend/src/canvas/serialize.test.ts`

**Interfaces:**
- Produces: `displayName(label: string | undefined, id: string): string`；`toFlow` 节点 `data` 含 `label`；`fromFlow` 节点 dict 含 `label`（仅当非空）。

- [ ] **Step 1: 写失败测试**（追加到 `serialize.test.ts`）

```ts
import { fromFlow, toFlow, displayName } from './serialize'

describe('node label', () => {
  it('label 经 toFlow→fromFlow 往返保留', () => {
    const g = { nodes: [{ id: 'llm_synth_1', type: 'llm_synth', position: { x: 1, y: 2 }, config: {}, label: '翻译' }], edges: [] }
    const f = toFlow(g as any)
    expect((f.nodes[0].data as any).label).toBe('翻译')
    const back = fromFlow(f.nodes, f.edges)
    expect((back.nodes[0] as any).label).toBe('翻译')
  })
  it('无 label 时 fromFlow 不写 label 键（不污染指纹）', () => {
    const g = { nodes: [{ id: 'input_1', type: 'input', position: { x: 0, y: 0 }, config: {} }], edges: [] }
    const back = fromFlow(toFlow(g as any).nodes, [])
    expect('label' in back.nodes[0]).toBe(false)
  })
  it('displayName: 有 label 用 label，否则用 id', () => {
    expect(displayName('翻译', 'llm_synth_1')).toBe('翻译')
    expect(displayName('', 'llm_synth_1')).toBe('llm_synth_1')
    expect(displayName(undefined, 'input_1')).toBe('input_1')
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npm test -- serialize`
Expected: FAIL（`displayName` 未导出、label 丢失）

- [ ] **Step 3: 实现**

`frontend/src/api/types.ts` —— 给 `GraphNode` 加可选字段：
```ts
// 在 GraphNode 接口里（与 id/type/position/config 同级）加：
  label?: string
```

`frontend/src/canvas/serialize.ts` —— toFlow/fromFlow 透传 label + 导出 displayName：
```ts
export function displayName(label: string | undefined, id: string): string {
  return (label && label.trim()) ? label : id
}

export function toFlow(graph: WorkflowGraph): { nodes: Node[]; edges: Edge[] } {
  return {
    nodes: graph.nodes.map((n) => ({
      id: n.id, type: n.type, position: n.position, data: { config: n.config, label: n.label },
    })),
    edges: graph.edges.map((e, i) => ({
      id: `e${i}_${e.source}_${e.target}`, source: e.source, target: e.target,
      sourceHandle: e.kind === 'rescan' ? 'rescan' : undefined,
      data: { kind: e.kind ?? 'normal' },
      ...(e.kind === 'rescan' ? RESCAN_EDGE : {}),
    })),
  }
}

export function fromFlow(nodes: Node[], edges: Edge[]): WorkflowGraph {
  return {
    nodes: nodes.map((n) => {
      const label = (n.data as { label?: string })?.label
      return {
        id: n.id,
        type: n.type as GraphNode['type'],
        position: { x: n.position.x, y: n.position.y },
        config: ((n.data as { config?: Record<string, any> })?.config) ?? {},
        ...(label && label.trim() ? { label } : {}),
      }
    }),
    edges: edges.map((e) => ({
      source: e.source, target: e.target,
      kind: (((e.data as { kind?: GraphEdge['kind'] })?.kind) ?? 'normal') as GraphEdge['kind'],
    })),
  }
}
```

- [ ] **Step 4: 跑测试确认通过 + 既有往返测试不破**

Run: `cd frontend && npm test -- serialize`
Expected: PASS（含原有「往返一致」用例：GRAPH 无 label，fromFlow 不写 label，`toEqual(GRAPH)` 仍成立）

- [ ] **Step 5: 提交**

```bash
git add frontend/src/api/types.ts frontend/src/canvas/serialize.ts frontend/src/canvas/serialize.test.ts
git commit -m "feat(前端): 节点显示名 label 经图序列化往返 + displayName 助手"
```

---

## Task 2: 画布显示 label + 抽屉改名 UI

**Files:**
- Modify: `frontend/src/canvas/nodeTypes.tsx`（GFNode 显示 `displayName(data.label, id)`）
- Modify: `frontend/src/pages/CanvasPage.tsx`（抽屉标题 + 显示名输入框 + `updateLabel`）

**Interfaces:**
- Consumes: `displayName` (Task 1)。
- Produces: 选中节点抽屉顶部「显示名」`Input`，改动经 `setNodes` 写 `node.data.label`，触发既有 800ms 防抖自动保存。

- [ ] **Step 1: 实现 GFNode 显示 label**

`frontend/src/canvas/nodeTypes.tsx`：
```tsx
import { Handle, Position, type NodeProps } from '@xyflow/react'
import { NODE_LABELS, displayName } from './serialize'
// ...
function GFNode({ id, type, selected, data }: NodeProps) {
  const t = type as keyof typeof NODE_LABELS
  const label = (data as { label?: string })?.label
  return (
    <div style={{ /* 原样式不变 */ }}>
      {t !== 'input' && <Handle type="target" position={Position.Left} />}
      <div style={{ fontSize: 12, color: COLORS[t] }}>{NODE_LABELS[t]}</div>
      <div style={{ fontWeight: 600 }}>{displayName(label, id)}</div>
      {t !== 'output' && <Handle type="source" position={Position.Right} />}
      {t === 'qc' && (
        <Handle id="rescan" type="source" position={Position.Bottom}
                title="回扫：连回上游 LLM 节点" style={{ background: '#fa8c16' }} />
      )}
    </div>
  )
}
```

- [ ] **Step 2: 实现抽屉标题 + 改名输入**

`frontend/src/pages/CanvasPage.tsx`：
- 顶部 import：`import { NODE_LABELS, RESCAN_EDGE, fromFlow, toFlow, displayName } from '../canvas/serialize'`
- 加 `updateLabel`（在 `updateConfig` 旁）：
```tsx
const updateLabel = (label: string) =>
  setNodes((ns) => ns.map((n) => (n.id === selectedId ? { ...n, data: { ...n.data, label } } : n)))
```
- 抽屉 `title` 改为显示名 + id 副标题：
```tsx
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
```
- 顶部 import 补 `Input`：`import { Alert, Button, Drawer, Input, Space, message } from 'antd'`

- [ ] **Step 3: 类型检查 + 跑前端测试**

Run: `cd frontend && npx tsc -b && npm test`
Expected: tsc clean；测试全绿（无回归）

- [ ] **Step 4: 提交**

```bash
git add frontend/src/canvas/nodeTypes.tsx frontend/src/pages/CanvasPage.tsx
git commit -m "feat(前端): 画布节点显示 label + 抽屉显示名改名输入"
```

---

## Task 3: 提示词库重排（工具栏 + 编辑/预览分栏 + 折叠元信息 + md 固定高滚动）

**Files:**
- Modify: `frontend/src/pages/PromptsPage.tsx`（仅第 56-118 行 JSX/样式分区，数据流不动）
- Test: `frontend/src/pages/PromptsPage.test.tsx`

**Interfaces:**
- Consumes: 既有 `list/sel/save/remove/duplicate/rollback/openDetail/openNew/shown/vars`，全不改。

- [ ] **Step 1: 写失败测试**（追加到 `PromptsPage.test.tsx`，沿用该文件现有 api mock 与渲染方式）

```tsx
// 假设现有 mock 已让 /api/prompts 返回含一条提示词、openDetail 返回 versions/used_by。
// 断言新版结构落地：
it('详情区有顶部工具栏按钮、编辑+预览并存、折叠元信息', async () => {
  // ...沿用本文件渲染 PromptsPage 并打开一条详情的步骤...
  expect(await screen.findByRole('button', { name: /保存/ })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /复制为新提示词/ })).toBeInTheDocument()
  // 编辑区 textarea 与 预览容器并存
  expect(screen.getByPlaceholderText(/名称/)).toBeInTheDocument()
  // 折叠元信息：版本历史 / 被引用 作为可折叠面板标题出现
  expect(screen.getByText('版本历史')).toBeInTheDocument()
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npm test -- PromptsPage`
Expected: FAIL（旧结构无折叠面板标题「版本历史」作为独立可点项 / 断言不满足）

- [ ] **Step 3: 实现重排**（替换 `PromptsPage.tsx` 第 74-117 行右栏整段；左侧列表 56-73 不动）

```tsx
import { Button, Collapse, Input, Popconfirm, Space, message } from 'antd'   // 顶部 import 补 Collapse
// ...右栏：
      <div style={{ flex: 1, overflow: 'auto', display: 'flex', flexDirection: 'column' }}>
        {/* 1. 顶部工具栏 */}
        <Space style={{ marginBottom: 12 }} wrap>
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
        {/* 2. 编辑 | 预览（预览固定高滚动窗口） */}
        <div style={{ display: 'flex', gap: 12, marginBottom: 12 }}>
          <div style={{ flex: 1 }}>
            <div style={{ color: '#666', marginBottom: 4 }}>正文（用 {'{{列名}}'} 引用数据列）</div>
            <Input.TextArea rows={18} value={body} onChange={(e) => setBody(e.target.value)} />
          </div>
          <div style={{ flex: 1, border: '1px solid #eee', borderRadius: 4, padding: 12,
                        height: 432, overflow: 'auto' }}>
            <ReactMarkdown>{body}</ReactMarkdown>
          </div>
        </div>
        {/* 3. 折叠元信息：变量 / 版本历史 / 被引用 */}
        <Collapse defaultActiveKey={['vars']} items={[
          { key: 'vars', label: '变量', children: (
            <div style={{ color: '#888', fontSize: 12 }}>
              {vars.length ? vars.map((v) => `{{${v}}}`).join('、') : '（无）'}
            </div>
          ) },
          ...(sel ? [
            { key: 'versions', label: '版本历史', children: (
              <>
                {sel.versions.slice().reverse().map((v) => (
                  <Space key={v.version} style={{ display: 'flex', marginBottom: 4 }}>
                    <span>v{v.version}</span>
                    <span style={{ color: '#999' }}>{v.created_at.slice(0, 19)}</span>
                    <a onClick={() => void rollback(v.version)}>回滚到此版</a>
                  </Space>
                ))}
              </>
            ) },
            { key: 'usedby', label: `被引用（${sel.used_by.length}）`, children: (
              sel.used_by.length > 0
                ? <div style={{ color: '#d4380d', fontSize: 12 }}>
                    {sel.used_by.map((u) => `${u.workflow_name}/${u.node_id}(${u.slot})`).join('、')}
                  </div>
                : <div style={{ color: '#999', fontSize: 12 }}>暂无引用</div>
            ) },
          ] : []),
        ]} />
      </div>
```

- [ ] **Step 4: 跑测试确认通过 + tsc**

Run: `cd frontend && npm test -- PromptsPage && npx tsc -b`
Expected: PASS + tsc clean

- [ ] **Step 5: 提交**

```bash
git add frontend/src/pages/PromptsPage.tsx frontend/src/pages/PromptsPage.test.tsx
git commit -m "feat(前端): 提示词库重排（工具栏+编辑预览分栏+折叠元信息+预览固定高滚动）"
```

---

## Task 4: 对话角色展示助手 + 主 AgentDrawer 去左右对齐

**Files:**
- Create: `frontend/src/agent/chatPresentation.ts`（`roleLabel` / `ROLE_BG`）
- Create: `frontend/src/agent/chatPresentation.test.ts`
- Modify: `frontend/src/agent/AgentDrawer.tsx`（renderMessage 去 `textAlign:'right'`，加角色标签 + 颜色）

**Interfaces:**
- Produces: `roleLabel(role: string): string`（user→'你'、assistant→'助手'、其他→'工具'）；`ROLE_BG: Record<string,string>`（user `#e6f4ff`，assistant `#f6ffed`）。Task 6 复用。

- [ ] **Step 1: 写失败测试** `frontend/src/agent/chatPresentation.test.ts`

```ts
import { describe, expect, it } from 'vitest'
import { roleLabel, ROLE_BG } from './chatPresentation'

describe('chatPresentation', () => {
  it('roleLabel 映射', () => {
    expect(roleLabel('user')).toBe('你')
    expect(roleLabel('assistant')).toBe('助手')
    expect(roleLabel('tool')).toBe('工具')
  })
  it('ROLE_BG 蓝绿', () => {
    expect(ROLE_BG.user).toBe('#e6f4ff')
    expect(ROLE_BG.assistant).toBe('#f6ffed')
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npm test -- chatPresentation`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现 helper**

`frontend/src/agent/chatPresentation.ts`：
```ts
export function roleLabel(role: string): string {
  return role === 'user' ? '你' : role === 'assistant' ? '助手' : '工具'
}
export const ROLE_BG: Record<string, string> = { user: '#e6f4ff', assistant: '#f6ffed' }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npm test -- chatPresentation`
Expected: PASS

- [ ] **Step 5: 应用到 AgentDrawer**（`AgentDrawer.tsx` `renderMessage` 第 190-212 行）

```tsx
import { roleLabel, ROLE_BG } from './chatPresentation'   // 顶部 import
// ...
  const renderMessage = (m: AgentMessageOut) => {
    if (m.role === 'tool') return renderToolEntry(m.content as AgentToolContent, m.id)
    const raw = m.content.text ?? ''
    if (m.role === 'user') {
      return (
        <div key={m.id} style={{ margin: '8px 0' }}>
          <div style={{ fontSize: 11, color: '#999', marginBottom: 2 }}>{roleLabel('user')}</div>
          <div style={{ background: ROLE_BG.user, borderRadius: 8, padding: '6px 10px', whiteSpace: 'pre-wrap' }}>{raw}</div>
        </div>
      )
    }
    const { text, commands } = extractConfirmDeletes(stripGoalMarkers(raw))
    return (
      <div key={m.id} style={{ margin: '8px 0' }}>
        <div style={{ fontSize: 11, color: '#999', marginBottom: 2 }}>{roleLabel('assistant')}</div>
        <div style={{ background: ROLE_BG.assistant, borderRadius: 8, padding: '6px 10px' }}>
          <ReactMarkdown>{text}</ReactMarkdown>
          {commands.map((cmd) => (
            <Button key={cmd} danger size="small" style={{ marginRight: 8 }} disabled={running}
                    onClick={() => void send(`确认：${cmd}`)}>
              确认删除：{cmd}
            </Button>
          ))}
        </div>
      </div>
    )
  }
```

- [ ] **Step 6: tsc + 全量前端测试**

Run: `cd frontend && npx tsc -b && npm test`
Expected: tsc clean；全绿

- [ ] **Step 7: 提交**

```bash
git add frontend/src/agent/chatPresentation.ts frontend/src/agent/chatPresentation.test.ts frontend/src/agent/AgentDrawer.tsx
git commit -m "feat(前端): 对话角色标签助手 + 主 Agent 抽屉去左右对齐改颜色区分"
```

---

## Task 5: 节点助手 store 重构为「每节点多会话 + 消息持久化」

**Files:**
- Modify: `frontend/src/agent/nodeAssistantStore.ts`
- Create: `frontend/src/agent/nodeAssistantStore.test.ts`

**Interfaces:**
- Produces:
  - `Conversation = { id: string; title: string; messages: AssistMsg[] }`
  - `NodeAssistState = { conversations: Conversation[]; activeId: string; draft: string; pending: boolean; modelConfigId?: number }`
  - `useNodeAssist(key): NodeAssistState`、`setDraft(key,draft)`、`setModelConfigId(key,id)`
  - `newConversation(key)`（=清除上下文：新空会话置顶并 active）
  - `switchConversation(key, id)`
  - `sendAssist(key, payload)`（payload 同现状：workflow_id/node_id/node_type/model_config_id/current_config/params）
  - `activeConversation(state): Conversation`

- [ ] **Step 1: 写失败测试** `frontend/src/agent/nodeAssistantStore.test.ts`

```ts
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', () => ({ api: { post: vi.fn() } }))
import { api } from '../api/client'
import {
  setDraft, newConversation, switchConversation, sendAssist, activeConversation,
  getState as readState,
} from './nodeAssistantStore'

beforeEach(() => { localStorage.clear(); vi.clearAllMocks() })

describe('nodeAssistantStore 多会话', () => {
  const KEY = 'graphflow.nodeAssistant.v1:1:llm_synth:n1'

  it('newConversation 新开空会话、旧会话仍在且可切回', async () => {
    ;(api.post as any).mockResolvedValue({ reply: 'ok', config: null })
    setDraft(KEY, '第一句')
    await sendAssist(KEY, { workflow_id: 1, node_id: 'n1', node_type: 'llm_synth', model_config_id: 9, current_config: {}, params: {} })
    const s1 = readState(KEY)
    const firstConv = s1.activeId
    expect(activeConversation(s1).messages.length).toBe(2)  // user + assistant
    newConversation(KEY)
    const s2 = readState(KEY)
    expect(s2.activeId).not.toBe(firstConv)
    expect(activeConversation(s2).messages.length).toBe(0)  // 新会话空
    expect(s2.conversations.length).toBe(2)
    switchConversation(KEY, firstConv)
    expect(activeConversation(readState(KEY)).messages.length).toBe(2)  // 切回旧会话消息还在
  })

  it('消息持久化到 localStorage（跨实例还原）', async () => {
    ;(api.post as any).mockResolvedValue({ reply: 'ok', config: null })
    setDraft(KEY, 'hi')
    await sendAssist(KEY, { workflow_id: 1, node_id: 'n1', node_type: 'llm_synth', model_config_id: 9, current_config: {}, params: {} })
    const raw = localStorage.getItem(KEY)!
    expect(JSON.parse(raw).conversations[0].messages.length).toBe(2)
  })

  it('损坏的旧格式 localStorage 降级为单空会话', () => {
    localStorage.setItem(KEY + ':legacy', JSON.stringify({ draft: 'x', modelConfigId: 3 }))
    const s = readState(KEY + ':legacy')
    expect(s.conversations.length).toBe(1)
    expect(s.draft).toBe('x')
    expect(s.modelConfigId).toBe(3)
  })
})
```

> 注：`readState(key)` 是测试辅助——store 应同时导出一个 `getState(key)` 纯读函数（`useNodeAssist` 内部用的 `get`）供测试断言。在实现里把内部 `get` 以 `getState` 名导出，测试 `import { getState as readState }`。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npm test -- nodeAssistantStore`
Expected: FAIL（新 API 不存在）

- [ ] **Step 3: 重写 store**（整文件替换 `nodeAssistantStore.ts`）

```ts
import { useSyncExternalStore } from 'react'
import { api } from '../api/client'
import type { NodeAssistReply } from '../api/types'

export interface AssistMsg { role: 'user' | 'assistant'; text: string; config?: Record<string, any>; error?: true }
export interface Conversation { id: string; title: string; messages: AssistMsg[] }
export interface NodeAssistState {
  conversations: Conversation[]
  activeId: string
  draft: string
  pending: boolean
  modelConfigId?: number
}

const states = new Map<string, NodeAssistState>()
const listeners = new Set<() => void>()
function emit() { listeners.forEach((l) => l()) }

function newId(): string {
  try { return crypto.randomUUID() } catch { return 'c' + (states.size + Math.floor(performance.now())) }
}
function emptyConv(): Conversation { return { id: newId(), title: '', messages: [] } }
function freshState(): NodeAssistState {
  const c = emptyConv()
  return { conversations: [c], activeId: c.id, draft: '', pending: false }
}

export function activeConversation(s: NodeAssistState): Conversation {
  return s.conversations.find((c) => c.id === s.activeId) ?? s.conversations[0]
}

function storage(): Storage | null {
  try { return globalThis.localStorage ?? null } catch { return null }
}
function validConv(c: any): c is Conversation {
  return c && typeof c.id === 'string' && Array.isArray(c.messages)
}
function restore(key: string): NodeAssistState | null {
  const raw = storage()?.getItem(key)
  if (!raw) return null
  try {
    const p = JSON.parse(raw)
    const draft = typeof p.draft === 'string' ? p.draft : ''
    const modelConfigId = typeof p.modelConfigId === 'number' ? p.modelConfigId : undefined
    const convs = Array.isArray(p.conversations) ? p.conversations.filter(validConv) : []
    if (!convs.length) return { ...freshState(), draft, modelConfigId }   // 旧格式/空 → 降级
    const activeId = convs.some((c: Conversation) => c.id === p.activeId) ? p.activeId : convs[0].id
    return { conversations: convs, activeId, draft, pending: false, modelConfigId }
  } catch { return null }
}
function persist(key: string, next: NodeAssistState) {
  const s = storage()
  if (!s) return
  s.setItem(key, JSON.stringify({
    conversations: next.conversations, activeId: next.activeId,
    draft: next.draft, modelConfigId: next.modelConfigId,
  }))
}

export function getState(key: string): NodeAssistState {
  const cached = states.get(key)
  if (cached) return cached
  const init = restore(key) ?? freshState()
  states.set(key, init)
  return init
}
function set(key: string, next: NodeAssistState) {
  states.set(key, next)
  persist(key, next)
  emit()
}

export function useNodeAssist(key: string): NodeAssistState {
  return useSyncExternalStore(
    (l) => { listeners.add(l); return () => { listeners.delete(l) } },
    () => getState(key),
  )
}

export function setDraft(key: string, draft: string) { set(key, { ...getState(key), draft }) }
export function setModelConfigId(key: string, modelConfigId: number | undefined) {
  set(key, { ...getState(key), modelConfigId })
}
export function newConversation(key: string) {
  const cur = getState(key)
  const conv = emptyConv()
  set(key, { ...cur, conversations: [conv, ...cur.conversations], activeId: conv.id, draft: '' })
}
export function switchConversation(key: string, id: string) {
  const cur = getState(key)
  if (cur.conversations.some((c) => c.id === id)) set(key, { ...cur, activeId: id })
}

function replaceConv(s: NodeAssistState, conv: Conversation): Conversation[] {
  return s.conversations.map((c) => (c.id === conv.id ? conv : c))
}

export async function sendAssist(key: string, payload: {
  workflow_id: number; node_id: string; node_type: string; model_config_id: number
  current_config: Record<string, any>; params: Record<string, any>
}) {
  const cur = getState(key)
  const text = cur.draft.trim()
  if (!text || cur.pending) return
  const active = activeConversation(cur)
  const history = active.messages.filter((m) => !m.error).map((m) => ({ role: m.role, text: m.text }))
  const withUser: Conversation = {
    ...active,
    title: active.title || text.slice(0, 20),
    messages: [...active.messages, { role: 'user', text }],
  }
  set(key, { ...cur, draft: '', pending: true, conversations: replaceConv(cur, withUser) })
  try {
    const r = await api.post<NodeAssistReply>('/api/agent/node-assist', { ...payload, instruction: text, history })
    const c = getState(key)
    const a = c.conversations.find((x) => x.id === active.id)
    if (!a) { set(key, { ...c, pending: false }); return }
    set(key, { ...c, pending: false, conversations: replaceConv(c,
      { ...a, messages: [...a.messages, { role: 'assistant', text: r.reply, config: r.config ?? undefined }] }) })
  } catch (e) {
    const c = getState(key)
    const a = c.conversations.find((x) => x.id === active.id)
    if (!a) { set(key, { ...c, pending: false }); return }
    set(key, { ...c, pending: false, conversations: replaceConv(c,
      { ...a, messages: [...a.messages, { role: 'assistant', text: '出错：' + (e as Error).message, error: true as const }] }) })
  }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npm test -- nodeAssistantStore`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add frontend/src/agent/nodeAssistantStore.ts frontend/src/agent/nodeAssistantStore.test.ts
git commit -m "feat(前端): 节点助手 store 重构为每节点多会话 + 消息持久化"
```

---

## Task 6: 节点助手 UI——会话切换 + 新会话(清除上下文) + 新消息布局

**Files:**
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx`（NodeAssist 组件第 351-398 行）
- Test: `frontend/src/canvas/forms/NodeConfigForm.test.tsx`

**Interfaces:**
- Consumes: Task 5 的 `useNodeAssist/newConversation/switchConversation/sendAssist/activeConversation`；Task 4 的 `roleLabel/ROLE_BG`。

- [ ] **Step 1: 写失败测试**（追加到 `NodeConfigForm.test.tsx`，沿用其 api mock；用 llm_synth 节点渲染助手）

```tsx
// 渲染含 NodeAssist 的表单后断言：
it('节点助手有会话选择和「新会话」按钮，且消息不左右对齐', async () => {
  // ...沿用本文件渲染 NodeConfigForm type="llm_synth" 的步骤...
  expect(await screen.findByRole('button', { name: '新会话' })).toBeInTheDocument()
  // 角色标签出现（你/助手 取决于是否有消息；至少「新会话」「发送」可见）
  expect(screen.getByRole('button', { name: '发送' })).toBeInTheDocument()
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npm test -- NodeConfigForm`
Expected: FAIL（无「新会话」按钮）

- [ ] **Step 3: 重写 NodeAssist 组件**（替换第 351-398 行）

```tsx
import {
  useNodeAssist, setDraft, setModelConfigId, sendAssist,
  newConversation, switchConversation, activeConversation,
} from '../../agent/nodeAssistantStore'
import { roleLabel, ROLE_BG } from '../../agent/chatPresentation'
// ...
function NodeAssist({ nodeType, workflowId, nodeId, config, onApply }: {
  nodeType: string; workflowId?: number; nodeId?: string
  config: Record<string, any>
  onApply: (config: Record<string, any>) => void
}) {
  const [models, setModels] = useState<ModelConfig[]>([])
  const key = `graphflow.nodeAssistant.v1:${workflowId ?? 0}:${nodeType}:${nodeId ?? ''}`
  const st = useNodeAssist(key)
  const active = activeConversation(st)
  const modelSel = st.modelConfigId
  useEffect(() => { void api.get<ModelConfig[]>('/api/models').then(setModels) }, [])
  const send = () => {
    if (!modelSel || !workflowId || !nodeId) return
    void sendAssist(key, {
      workflow_id: workflowId, node_id: nodeId, node_type: nodeType, model_config_id: modelSel,
      current_config: config, params: withThinkingParamDefaults(config.params),
    })
  }
  return (
    <div style={{ border: '1px dashed #d9d9d9', borderRadius: 6, padding: 8, marginBottom: 12 }}>
      <div style={{ color: '#722ed1', marginBottom: 6 }}>RedLotus 助手：多轮对话配置本节点</div>
      <Space style={{ marginBottom: 8 }} wrap>
        <Select size="small" style={{ width: 150 }} value={active.id}
                onChange={(v) => switchConversation(key, v)}
                options={st.conversations.map((c, i) => ({ value: c.id, label: c.title || `会话 ${st.conversations.length - i}` }))} />
        <Button size="small" onClick={() => newConversation(key)}>新会话</Button>
      </Space>
      <div style={{ maxHeight: 200, overflowY: 'auto', marginBottom: 8 }}>
        {active.messages.map((m, i) => (
          <div key={i} style={{ margin: '6px 0' }}>
            <div style={{ fontSize: 11, color: '#999' }}>{roleLabel(m.role)}</div>
            <div style={{ background: ROLE_BG[m.role], borderRadius: 6, padding: '4px 8px',
                          whiteSpace: 'pre-wrap', fontSize: 12 }}>{m.text}</div>
            {m.config && (
              <Button size="small" type="link" onClick={() => onApply(m.config!)}>应用到节点</Button>
            )}
          </div>
        ))}
        {st.pending && <Spin size="small" style={{ display: 'block', margin: 4 }} />}
      </div>
      <Input.TextArea rows={2} value={st.draft} placeholder="如：把 q 列翻译成英文存到 q_en；再严格点…"
                      onChange={(e) => setDraft(key, e.target.value)} />
      <Space style={{ marginTop: 8 }}>
        <Select size="small" style={{ width: 150 }} placeholder="生成用模型" value={modelSel}
                onChange={(v) => setModelConfigId(key, v)}
                options={models.map((m) => ({ value: m.id, label: m.name }))} />
        <Button size="small" loading={st.pending} disabled={!st.draft.trim() || !modelSel}
                onClick={send}>发送</Button>
      </Space>
    </div>
  )
}
```

- [ ] **Step 4: 跑测试 + tsc**

Run: `cd frontend && npm test -- NodeConfigForm && npx tsc -b`
Expected: PASS + tsc clean

- [ ] **Step 5: 提交**

```bash
git add frontend/src/canvas/forms/NodeConfigForm.tsx frontend/src/canvas/forms/NodeConfigForm.test.tsx
git commit -m "feat(前端): 节点助手会话切换+新会话(清除上下文)+全宽角色标签布局"
```

---

## Task 7: 加节点跟随视口

**Files:**
- Create: `frontend/src/canvas/layout.ts`（纯函数 `nodeDropPosition`）
- Create: `frontend/src/canvas/layout.test.ts`
- Modify: `frontend/src/pages/CanvasPage.tsx`（`addNode` 用 `useReactFlow().screenToFlowPosition`）

**Interfaces:**
- Produces: `nodeDropPosition(center: { x: number; y: number }, count: number): { x: number; y: number }`

- [ ] **Step 1: 写失败测试** `frontend/src/canvas/layout.test.ts`

```ts
import { describe, expect, it } from 'vitest'
import { nodeDropPosition } from './layout'

describe('nodeDropPosition', () => {
  it('落在视口中心（减半宽/半高）', () => {
    expect(nodeDropPosition({ x: 500, y: 300 }, 0)).toEqual({ x: 435, y: 280 })
  })
  it('按 count 错位防重叠（每个 +24，6 循环）', () => {
    expect(nodeDropPosition({ x: 500, y: 300 }, 1)).toEqual({ x: 459, y: 304 })
    expect(nodeDropPosition({ x: 500, y: 300 }, 6)).toEqual({ x: 435, y: 280 })  // 回到 0 偏移
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npm test -- layout`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现纯函数**

`frontend/src/canvas/layout.ts`：
```ts
// 新节点落点：视口中心减去节点半宽(≈65)/半高(≈20)，再按已有节点数错位（每个 +24，6 个一循环）防完全重叠。
export function nodeDropPosition(center: { x: number; y: number }, count: number): { x: number; y: number } {
  const k = count % 6
  return { x: center.x - 65 + k * 24, y: center.y - 20 + k * 24 }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && npm test -- layout`
Expected: PASS

- [ ] **Step 5: 接入 CanvasPage**

`frontend/src/pages/CanvasPage.tsx`：
- import：`import { Background, Controls, ReactFlow, ReactFlowProvider, addEdge, useEdgesState, useNodesState, useReactFlow, type Connection, type Edge, type Node } from '@xyflow/react'`
- import：`import { nodeDropPosition } from '../canvas/layout'`
- `Canvas` 内加 hook 与容器 ref：
```tsx
const rf = useReactFlow()
const flowWrap = useRef<HTMLDivElement>(null)
```
- 给包裹 ReactFlow 的根 div 挂 ref：`<div ref={flowWrap} style={{ height: 'calc(100vh - 48px)', position: 'relative' }}>`
- 改写 `addNode`：
```tsx
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
```

- [ ] **Step 6: tsc + 全量前端测试**

Run: `cd frontend && npx tsc -b && npm test`
Expected: tsc clean；全绿

- [ ] **Step 7: 提交**

```bash
git add frontend/src/canvas/layout.ts frontend/src/canvas/layout.test.ts frontend/src/pages/CanvasPage.tsx
git commit -m "feat(前端): 加节点落在当前视口中心(screenToFlowPosition)并错位防重叠"
```

---

## Task 8: HTTP 节点配置校验改 endpoint/params/body_format

**Files:**
- Modify: `backend/app/engine/runner.py`（`validate_node_config_shape` 第 394-402 行 http 分支）
- Test: `backend/tests/test_http_node.py`（更新 dirty-config 参数化 + 新增 params/body_format）

**Interfaces:**
- Produces: http_fetch 校验接受 `endpoint`(或 `url`)/`params`/`body`/`body_format`/`headers`/`extract`；脏值整 run failed 并点名节点+键。

- [ ] **Step 1: 更新/新增失败测试**（替换 `test_http_node.py` 第 143-164 行参数化）

```python
@pytest.mark.parametrize("bad_cfg, kw", [
    ({"endpoint": {"bad": 1}}, "endpoint"),
    ({"url": {"bad": 1}}, "endpoint"),                 # 旧 url 走兼容，仍按 endpoint 报
    ({"endpoint": "http://x", "params": ["a"]}, "params"),
    ({"endpoint": "http://x", "body": [1]}, "body"),
    ({"endpoint": "http://x", "body_format": "xml"}, "body_format"),
    ({"endpoint": "http://x", "headers": ["a"]}, "headers"),
    ({"endpoint": "http://x", "extract": ["a"]}, "extract"),
])
async def test_http_node_dirty_config_fails_run_named(session_factory, monkeypatch, bad_cfg, kw):
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        return 200, "{}"
    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    graph = json.loads(json.dumps(HTTP_GRAPH))
    for n in graph["nodes"]:
        if n["type"] == "http_fetch":
            cfg = {k: v for k, v in n["config"].items() if k != "url"}  # 去掉基础 url，避免覆盖被测键
            n["config"] = {**cfg, **bad_cfg}
    run_id = await make_run(session_factory, graph=graph)
    await run_it(session_factory, run_id)
    run = await get_run(session_factory, run_id)
    assert run.status == "failed"
    assert "fetch" in (run.error or "") and kw in (run.error or "")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_http_node.py -k dirty_config -v`
Expected: FAIL（旧校验只认 url，不报 endpoint/params/body_format）

- [ ] **Step 3: 实现校验**（替换 `runner.py` http_fetch 分支第 394-402 行）

```python
    elif node.type == "http_fetch":
        ep = cfg.get("endpoint", cfg.get("url", ""))
        if not isinstance(ep, str):
            raise ValueError(f"http_fetch 节点 {node.id}: endpoint 必须为字符串，当前为 {type(ep).__name__}")
        if cfg.get("params") is not None and not isinstance(cfg.get("params"), dict):
            raise ValueError(f"http_fetch 节点 {node.id}: params 必须为对象，当前为 {type(cfg.get('params')).__name__}")
        if cfg.get("body") and not isinstance(cfg.get("body"), str):
            raise ValueError(f"http_fetch 节点 {node.id}: body 必须为字符串，当前为 {type(cfg.get('body')).__name__}")
        bf = cfg.get("body_format")
        if bf is not None and bf not in ("json", "raw", "form"):
            raise ValueError(f"http_fetch 节点 {node.id}: body_format 必须为 json/raw/form，当前为 {bf!r}")
        if cfg.get("headers") is not None and not isinstance(cfg.get("headers"), dict):
            raise ValueError(f"http_fetch 节点 {node.id}: headers 必须为对象，当前为 {type(cfg.get('headers')).__name__}")
        if cfg.get("extract") is not None and not isinstance(cfg.get("extract"), dict):
            raise ValueError(f"http_fetch 节点 {node.id}: extract 必须为对象，当前为 {type(cfg.get('extract')).__name__}")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_http_node.py -v`
Expected: PASS（含原有非 dirty 用例——它们用 `url`，校验经 endpoint 兼容仍通过）

- [ ] **Step 5: 提交**

```bash
git add backend/app/engine/runner.py backend/tests/test_http_node.py
git commit -m "feat(http): 配置校验改 endpoint/params/body_format(兼容旧 url)，脏值点名"
```

---

## Task 9: run_http_fetch_row 接口/params 合并 + body_format Content-Type

**Files:**
- Modify: `backend/app/engine/nodes.py`（`run_http_fetch_row` 第 224-242 行 + 顶部 `import httpx`）
- Test: `backend/tests/test_http_node.py`

**Interfaces:**
- Consumes: `http.fetch(method, url, headers, body, timeout, retries)`（签名不变；url 为已合并 params 的最终 URL）。
- Produces: endpoint 渲染→合并 params 查询串；headers 按 body_format 注入 Content-Type（用户已设则不覆盖）；旧 `url` 兼容。

- [ ] **Step 1: 写失败测试**（追加到 `test_http_node.py`）

```python
async def test_http_fetch_merges_params_with_apikey(monkeypatch):
    seen = {}
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        seen.update(url=url, headers=headers, body=body)
        return 200, json.dumps({"v": 1})
    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    cfg = {"method": "GET", "endpoint": "http://api/{{city}}",
           "params": {"api_key": "SECRET", "q": "{{city}}"}, "extract": {"v": "v"}}
    out, _ = await nodes.run_http_fetch_row(cfg, {"city": "bj"})
    assert seen["url"].startswith("http://api/bj?")          # endpoint 渲染
    assert "api_key=SECRET" in seen["url"] and "q=bj" in seen["url"]  # params 合并(含 api_key)+模板渲染
    assert out == [{"city": "bj", "v": 1}]

async def test_http_fetch_body_format_sets_content_type(monkeypatch):
    seen = {}
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        seen.update(headers=headers, body=body)
        return 200, "{}"
    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    cfg = {"method": "POST", "endpoint": "http://api", "body": '{"a":1}', "body_format": "json", "extract": {}}
    await nodes.run_http_fetch_row(cfg, {})
    assert seen["headers"]["Content-Type"] == "application/json"
    assert seen["body"] == '{"a":1}'

async def test_http_fetch_user_content_type_wins(monkeypatch):
    seen = {}
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        seen.update(headers=headers)
        return 200, "{}"
    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    cfg = {"method": "POST", "endpoint": "http://api", "body": "x", "body_format": "json",
           "headers": {"Content-Type": "text/plain"}, "extract": {}}
    await nodes.run_http_fetch_row(cfg, {})
    assert seen["headers"]["Content-Type"] == "text/plain"   # 用户显式设置不被覆盖

async def test_http_fetch_legacy_url_still_works(monkeypatch):
    seen = {}
    async def fake_fetch(method, url, headers=None, body=None, timeout=30, retries=2):
        seen.update(url=url)
        return 200, json.dumps({"v": 9})
    monkeypatch.setattr("app.services.http.fetch", fake_fetch)
    out, _ = await nodes.run_http_fetch_row({"url": "http://api/{{q}}", "extract": {"v": "v"}}, {"q": "z"})
    assert seen["url"] == "http://api/z"                     # 无 params 时 endpoint=url 原样
    assert out == [{"q": "z", "v": 9}]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_http_node.py -k "params or body_format or content_type or legacy" -v`
Expected: FAIL（旧实现不读 endpoint/params/body_format）

- [ ] **Step 3: 实现**（顶部加 `import httpx`；替换 `run_http_fetch_row` 第 224-242 行）

```python
_CONTENT_TYPES = {"json": "application/json", "form": "application/x-www-form-urlencoded", "raw": "text/plain"}


async def run_http_fetch_row(config: dict, row: dict) -> tuple[list[dict], dict]:
    """处理一条输入行：渲染 endpoint/params/headers/body 后调接口，按 extract 的 JSON 路径提取落列。
    返回 (输出行列表, 空 usage)。请求失败/响应非 JSON 抛异常由 runner 记为行失败（逐行隔离）。
    params(含 api_key)合并进查询串；body_format 决定 Content-Type（用户已在 headers 设置则不覆盖）。"""
    base = strip_internal(row)
    method = config.get("method", "GET")
    endpoint = render_template(config.get("endpoint") or config.get("url", ""), base)
    params = {k: render_template(str(v), base) for k, v in (config.get("params") or {}).items()}
    url = str(httpx.URL(endpoint).copy_merge_params(params)) if params else endpoint
    headers = {k: render_template(str(v), base) for k, v in (config.get("headers") or {}).items()}
    body = render_template(config["body"], base) if config.get("body") else None
    ct = _CONTENT_TYPES.get(config.get("body_format"))
    if body and ct and not any(k.lower() == "content-type" for k in headers):
        headers["Content-Type"] = ct
    status, text = await http.fetch(method, url, headers=headers, body=body,
                                    timeout=config.get("timeout", 30), retries=config.get("retries", 2))
    try:   # parse_constant：响应含非标准 NaN/Infinity 归一为 None，杜绝非法浮点落库致读行端点 500
        data = _json.loads(text, parse_constant=lambda _v: None)
    except (ValueError, TypeError):
        raise ValueError(f"接口响应非 JSON，无法提取（HTTP {status} {url}）")
    extracted = {}
    for col, path in (config.get("extract") or {}).items():
        v = json_path_get(data, path)
        extracted[col] = "" if v is None else v   # 字段缺失→空串，非缺失保原类型
    return [{**base, **extracted}], {}
```

- [ ] **Step 4: 跑测试确认通过（全 http 文件）**

Run: `cd backend && python -m pytest tests/test_http_node.py -v`
Expected: PASS（含原 `test_run_http_fetch_row_renders_and_extracts` 等——它们无 params，url 经 endpoint 兼容）

- [ ] **Step 5: 提交**

```bash
git add backend/app/engine/nodes.py backend/tests/test_http_node.py
git commit -m "feat(http): run_http_fetch_row 支持 endpoint+params 合并查询串与 body_format Content-Type"
```

---

## Task 10: 导出脱敏覆盖 params 与 endpoint

**Files:**
- Modify: `backend/app/services/workflow_package.py`（`redact_secrets` 第 145-177 行）
- Test: `backend/tests/`（脱敏相关测试文件，沿用现有 `redact_secrets` 测试模式）

**Interfaces:**
- Produces: http_fetch 节点 `params` 里敏感键（api_key/token…）值 → REDACTED 并登记 `field=params.<k>`；`endpoint` 按 URL 脱敏；模板值(含 `{{`)放行。

- [ ] **Step 1: 写失败测试**（追加到现有脱敏测试文件，如 `tests/test_workflow_package.py`）

```python
def test_redact_http_params_and_endpoint():
    from app.services.workflow_package import redact_secrets
    graph = {"nodes": [{"id": "f", "type": "http_fetch", "config": {
        "endpoint": "http://api?token=SECRET",
        "params": {"api_key": "KKK", "q": "hi", "tpl": "{{x}}"},
    }}], "edges": []}
    reds = redact_secrets(graph)
    cfg = graph["nodes"][0]["config"]
    assert cfg["params"]["api_key"] == "***REDACTED***"      # 敏感键值打码
    assert cfg["params"]["q"] == "hi"                         # 非敏感保留
    assert cfg["params"]["tpl"] == "{{x}}"                    # 模板值放行
    assert "token=***REDACTED***" in cfg["endpoint"]          # endpoint 查询串脱敏
    fields = {r["field"] for r in reds}
    assert "params.api_key" in fields
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_workflow_package.py -k redact_http_params -v`
Expected: FAIL（params/endpoint 未脱敏）

- [ ] **Step 3: 实现**（在 `redact_secrets` 循环内，headers 块之后、url 块附近加 params 与 endpoint 处理）

```python
        params = cfg.get("params")
        if isinstance(params, dict):
            for k in list(params):
                if _SENSITIVE.search(str(k)) and _is_secret_value(params[k]):
                    params[k] = REDACTED
                    redactions.append({"node_id": nid, "field": f"params.{k}"})
        if isinstance(cfg.get("endpoint"), str):
            cfg["endpoint"] = _redact_url(cfg["endpoint"], nid, redactions)
        if isinstance(cfg.get("url"), str):                  # 兼容旧节点
            cfg["url"] = _redact_url(cfg["url"], nid, redactions)
```
（注：原文件已有 `if isinstance(cfg.get("url"), str): cfg["url"] = _redact_url(...)`——保留，不要重复；只新增 params 与 endpoint 两块。）

并把 `redact_secrets` 文档串第 146 行更新提及 endpoint/params。

- [ ] **Step 4: 跑测试确认通过 + 全脱敏文件回归**

Run: `cd backend && python -m pytest tests/test_workflow_package.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/workflow_package.py backend/tests/test_workflow_package.py
git commit -m "feat(http): 导出脱敏覆盖 http params(api_key) 与 endpoint 查询串"
```

---

## Task 11: 展示层适配 endpoint/params（node_info + CLI summarize）

**Files:**
- Modify: `backend/app/agent/node_info.py`（`_summarize_node` http 概览）
- Modify: `backend/app/cli/client.py`（`HTTP_STR_KEYS` + `summarize` http 行）
- Test: 沿用 `tests/` 内 node_info / cli 相关测试（若无则加轻量单测）

**Interfaces:**
- Produces: 节点摘要/CLI 展示用 `endpoint`(回退 url) 与 params 键名；不泄漏 params 值。

- [ ] **Step 1: 写失败测试**（在 node_info 测试文件追加；若无则建 `tests/test_node_info_http.py`）

```python
def test_summarize_http_uses_endpoint_and_param_keys():
    from app.agent.node_info import _summarize_node
    from app.engine.graph import Node
    n = Node(id="f", type="http_fetch", config={
        "method": "GET", "endpoint": "http://api", "params": {"api_key": "S", "q": "x"},
        "extract": {"v": "data.v"}})
    s = _summarize_node(n)
    assert s.get("endpoint") == "http://api"
    assert set(s.get("param_keys", [])) == {"api_key", "q"}   # 只给键名，不给值
    assert "S" not in json.dumps(s, ensure_ascii=False)        # 不泄漏 api_key 值
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_node_info_http.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

`backend/app/agent/node_info.py` `_summarize_node` http_fetch 分支（原返回 `{method, url, extract}`）：
```python
    if node.type == "http_fetch":
        c = node.config
        return {"method": c.get("method", "GET"),
                "endpoint": c.get("endpoint") or c.get("url", ""),
                "param_keys": sorted((c.get("params") or {}).keys()),
                "extract": c.get("extract", {})}
```
`backend/app/cli/client.py`：
- `HTTP_STR_KEYS` 加 `"endpoint"`（保留 url 兼容）。
- `summarize()` http 行：`endpoint = cfg.get("endpoint") or cfg.get("url", "")`，展示 `f"{method} {endpoint} -> {list(extract)}"`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_node_info_http.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/agent/node_info.py backend/app/cli/client.py backend/tests/test_node_info_http.py
git commit -m "feat(http): 节点摘要/CLI 展示改用 endpoint 与 param 键名(不泄漏值)"
```

---

## Task 12: HTTP 节点助手接入（后端白名单 + 提示词）

**Files:**
- Modify: `backend/app/routers/agent.py`（第 369 行白名单）
- Modify: `backend/app/agent/codegen.py`（`NODE_ASSIST_INSTRUCTIONS` 第 54-57 行）
- Create: `backend/app/agent/prompts/node_assist_http_fetch.md`
- Test: `backend/tests/test_agent_api.py`

**Interfaces:**
- Consumes: 既有 `generate_node_config` + 11 只读工具基建（无需新工具）。
- Produces: `POST /api/agent/node-assist` 放行 `http_fetch`，返回 `{reply, config}`，config 用新键。

- [ ] **Step 1: 写失败测试**（追加到 `test_agent_api.py`，沿用其对 node-assist 的 mock 模式——通常 monkeypatch `codegen.generate_node_config` 返回固定 `{reply, config}`）

```python
async def test_node_assist_allows_http_fetch(client, monkeypatch, ...):
    async def fake_gen(*a, **k):
        return {"reply": "已配置", "config": {"method": "GET", "endpoint": "http://api",
                                              "params": {"api_key": "x"}, "extract": {"v": "data.v"}}}
    monkeypatch.setattr("app.agent.codegen.generate_node_config", fake_gen)
    # ...建工作流含 http_fetch 节点、建模型，POST /api/agent/node-assist node_type=http_fetch...
    resp = await client.post("/api/agent/node-assist", json={
        "workflow_id": WF, "node_id": "f", "node_type": "http_fetch",
        "instruction": "配置天气接口", "model_config_id": MC, "current_config": {}, "history": []})
    assert resp.status_code == 200
    assert resp.json()["config"]["endpoint"] == "http://api"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_agent_api.py -k http_fetch -v`
Expected: FAIL（当前 422「该节点类型不支持助手」）

- [ ] **Step 3: 实现**

`backend/app/routers/agent.py` 第 369 行：
```python
    if body.node_type not in ("llm_synth", "qc", "http_fetch"):
```
`backend/app/agent/codegen.py` 第 54-57 行：
```python
NODE_ASSIST_INSTRUCTIONS = {
    "llm_synth": load_prompt("node_assist_llm_synth.md"),
    "qc": load_prompt("node_assist_qc.md"),
    "http_fetch": load_prompt("node_assist_http_fetch.md"),
}
```
新建 `backend/app/agent/prompts/node_assist_http_fetch.md`（镜像 llm_synth/qc 契约）：
```markdown
你是 HTTP 取数节点的配置助手。根据用户需求，在「现有节点配置」基础上增量产出本节点的配置补丁。

# 输出契约（必须严格遵守）
只输出一个 JSON 对象，不要任何额外文字/代码围栏：
{"reply": "<中文说明这一轮做了什么 / 还缺什么信息>", "config": <配置补丁对象 或 null>}
- 信息不足以确定接口时，config 置 null，在 reply 里反问澄清（如：调用哪个接口？鉴权方式？要从响应里取哪些字段？）。
- 信息足够时，config 给出要合并进节点的键。

# 节点配置键
- method: "GET" 或 "POST"
- endpoint: 接口地址，可用 {{列名}} 引用上游数据列（逐行渲染）
- params: 查询参数对象（会拼进 URL 查询串）；**api_key 等鉴权参数放这里**；值可用 {{列名}}
- body: 请求体字符串（POST 用）；可用 {{列名}}
- body_format: "json" | "raw" | "form"（决定 Content-Type）
- headers: 请求头对象（Authorization / Bearer 等鉴权头放这里）；值可用 {{列名}}
- extract: { 输出列名: "响应 JSON 路径" }，路径用点号+数字索引，如 data.weather.0.desc

# 规则
- 上游列名只能用「输入列」里列出的；引用语法是双花括号 {{列名}}。
- **绝不在 reply 里回显 headers / params 里的密钥值**（token/api_key 等），只描述结构。
- 保留用户现有配置里已有的内容，做增量修改，不要丢失之前的需求。
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_agent_api.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/routers/agent.py backend/app/agent/codegen.py backend/app/agent/prompts/node_assist_http_fetch.md backend/tests/test_agent_api.py
git commit -m "feat(http): 节点助手放行 http_fetch + 专属提示词(api_key 进 params、不回显密钥)"
```

---

## Task 13: HttpFetchForm 重构 + 挂载节点助手

**Files:**
- Modify: `frontend/src/canvas/forms/NodeConfigForm.tsx`（HttpFetchForm 第 809-862 行）
- Test: `frontend/src/canvas/forms/NodeConfigForm.test.tsx`

**Interfaces:**
- Consumes: 既有 `NodeAssist`（Task 6）、`KvEditor`、`Collapse`、`MissingColsWarning`。
- Produces: 接口(endpoint，回退 url) / Params(KvEditor，含 api_key) / body + body_format(GET 隐藏) / Headers 移入高级 / 顶部挂 NodeAssist(nodeType="http_fetch")。

- [ ] **Step 1: 写失败测试**（追加到 `NodeConfigForm.test.tsx`，渲染 `type="http_fetch"`）

```tsx
it('HTTP 表单有接口/Params/助手，Headers 在高级', async () => {
  // ...沿用本文件渲染 NodeConfigForm type="http_fetch" 的步骤...
  expect(await screen.findByText(/RedLotus 助手/)).toBeInTheDocument()   // 节点助手已挂载
  expect(screen.getByText(/接口/)).toBeInTheDocument()                  // endpoint 字段标签
  expect(screen.getByText(/Params/)).toBeInTheDocument()
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && npm test -- NodeConfigForm`
Expected: FAIL（无助手 / 无 Params 字段）

- [ ] **Step 3: 重写 HttpFetchForm**（替换第 809-862 行）

```tsx
function HttpFetchForm({ config, onChange, workflowId, nodeId, inputCols }: FormProps & {
  workflowId?: number; nodeId?: string; inputCols: string[]
}) {
  const collapse = usePersistentCollapse(nodeCollapseKey(workflowId, 'http_fetch', nodeId))
  const patch = (p: object) => onChange({ ...config, ...p })
  const method = config.method ?? 'GET'
  const endpoint = config.endpoint ?? config.url ?? ''        // 兼容旧 url
  return (
    <>
      <NodeAssist nodeType="http_fetch" workflowId={workflowId} nodeId={nodeId} config={config}
                  onApply={(c) => onChange({ ...config, ...c })} />
      <Collapse activeKey={collapse.activeKey} onChange={collapse.onChange} items={[
        { key: 'req', label: '请求', children: (
          <>
            <Field label="请求方法">
              <Radio.Group value={method} onChange={(e) => patch({ method: e.target.value })}>
                <Radio.Button value="GET">GET</Radio.Button>
                <Radio.Button value="POST">POST</Radio.Button>
              </Radio.Group>
            </Field>
            <Field label="接口 Endpoint（用 {{列名}} 引用上游数据列）">
              <Input.TextArea rows={2} value={endpoint}
                              onChange={(e) => patch({ endpoint: e.target.value, url: undefined })} />
              <MissingColsWarning text={endpoint} inputCols={inputCols} />
            </Field>
            <Field label="Params 查询参数（值可用 {{列名}}；api_key 放这里）">
              <KvEditor pairs={config.params ?? {}} onChange={(p) => patch({ params: p })}
                        keyPlaceholder="参数名 如 api_key" valPlaceholder="值" />
            </Field>
            {method === 'POST' && (
              <>
                <Field label="Body 格式">
                  <Radio.Group value={config.body_format ?? 'json'}
                               onChange={(e) => patch({ body_format: e.target.value })}>
                    <Radio.Button value="json">JSON</Radio.Button>
                    <Radio.Button value="raw">原始文本</Radio.Button>
                    <Radio.Button value="form">表单</Radio.Button>
                  </Radio.Group>
                </Field>
                <Field label="请求体 Body（{{列名}} 可引用）">
                  <Input.TextArea rows={3} value={config.body ?? ''}
                                  onChange={(e) => patch({ body: e.target.value })} />
                  <MissingColsWarning text={config.body ?? ''} inputCols={inputCols} />
                </Field>
              </>
            )}
          </>
        ) },
        { key: 'extract', label: '提取', children: (
          <Field label="提取（响应 JSON 路径 → 输出列；如 temp ← data.temp）">
            <KvEditor pairs={config.extract ?? {}} onChange={(e) => patch({ extract: e })}
                      keyPlaceholder="输出列名" valPlaceholder="JSON 路径 如 data.weather.0.desc" />
          </Field>
        ) },
        { key: 'advanced', label: '高级（请求头 / 并发 / 重试 / 超时）', children: (
          <>
            <Field label="请求头 Headers（Authorization / Bearer 等；值可用 {{列名}}）">
              <KvEditor pairs={config.headers ?? {}} onChange={(h) => patch({ headers: h })}
                        keyPlaceholder="Header 名" valPlaceholder="值" />
            </Field>
            <Space wrap>
              <Field label="节点并发"><InputNumber min={1} value={config.concurrency ?? 4}
                onChange={(v) => patch({ concurrency: v ?? 4 })} /></Field>
              <Field label="重试次数"><InputNumber min={0} value={config.retries ?? 2}
                onChange={(v) => patch({ retries: v ?? 2 })} /></Field>
              <Field label="超时(秒)"><InputNumber min={1} value={config.timeout ?? 30}
                onChange={(v) => patch({ timeout: v ?? 30 })} /></Field>
            </Space>
          </>
        ) },
      ]} />
    </>
  )
}
```

- [ ] **Step 4: 跑测试 + tsc + 全量**

Run: `cd frontend && npm test -- NodeConfigForm && npx tsc -b && npm test`
Expected: PASS + tsc clean；全绿

- [ ] **Step 5: 提交**

```bash
git add frontend/src/canvas/forms/NodeConfigForm.tsx frontend/src/canvas/forms/NodeConfigForm.test.tsx
git commit -m "feat(http): 表单重构 接口/Params(含api_key)/body格式/Headers移高级 + 挂载节点助手"
```

---

## 收尾：全量回归 + 活体

- [ ] 后端全量：`cd backend && python -m pytest -q` → 全绿（≥ 基线 715 + 新增）。
- [ ] 前端全量：`cd frontend && npm test && npx tsc -b` → 全绿 + tsc clean。
- [ ] （可选活体，需重启服务）真实 DeepSeek 跑一条含 http_fetch 的链路：endpoint+params(api_key) 真实发请求、节点助手生成 http 配置、改名/多会话/视口落点人工验证；smoke 用户建即删回基线。

---

## Self-Review（已对照 spec）

- **spec 覆盖**：①Task1-2；②⑥Task3；③Task4(主)+Task6(节点)；④Task5+6；⑤Task7；⑦后端 Task8-11、助手 Task12、前端 Task13。全覆盖。
- **占位扫描**：无 TBD/TODO；每个代码步均有真实测试+实现代码。Task3/6/12/13 的前端/后端测试 mock 沿用对应现有测试文件的既定方式（执行时先读该文件对齐 mock），断言为真实代码。
- **类型一致**：store API（`newConversation/switchConversation/sendAssist/activeConversation/getState`）在 Task5 定义、Task6 消费一致；`displayName`(Task1)→Task2；`roleLabel/ROLE_BG`(Task4)→Task6；`nodeDropPosition`(Task7) 自洽；http `endpoint/params/body_format` 在 Task8(校验)/9(执行)/10(脱敏)/11(展示)/13(表单) 命名一致。
- **back-compat**：旧 `url` 在校验/执行/脱敏/表单/展示全程回退；serialize 无 label 时 `toEqual(GRAPH)` 仍成立——既有测试不破。
