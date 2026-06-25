import { useEffect, useState } from 'react'
import { Button, Collapse, Input, InputNumber, Popover, Radio, Select, Space, Spin, Switch, Table, Tag } from 'antd'
import { api } from '../../api/client'
import type { CodegenOut, ColumnsMap, Dataset, ModelConfig, PromptDetail, PromptSummary, RowsPage } from '../../api/types'
import { activeConversation, newConversation, sendAssist, setDraft, setModelConfigId, switchConversation, useNodeAssist } from '../../agent/nodeAssistantStore'
import { roleLabel, ROLE_BG } from '../../agent/chatPresentation'
import { extractTplVars, renderCell } from '../../utils'

export interface FormProps {
  config: Record<string, any>
  onChange: (config: Record<string, any>) => void
}

export const THINKING_EFFORT_OPTIONS = ['low', 'medium', 'high', 'xhigh', 'max'] as const

function withThinkingParamDefaults(params?: Record<string, any>) {
  return {
    ...(params ?? {}),
    thinking_enabled: params?.thinking_enabled ?? true,
    reasoning_effort: params?.reasoning_effort ?? 'high',
  }
}

export function buildCodegenPayload(
  workflowId: number,
  nodeId: string,
  modelSel: number,
  op: Record<string, any>,
) {
  return {
    workflow_id: workflowId,
    node_id: nodeId,
    instruction: op.instruction,
    model_config_id: modelSel,
    current_code: op.code,
    params: withThinkingParamDefaults(op.params),
  }
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ marginBottom: 4, color: '#666' }}>{label}</div>
      {children}
    </div>
  )
}

type CollapseActiveKey = string | string[] | undefined

function nodeCollapseKey(workflowId: number | undefined, nodeType: string, nodeId: string | undefined) {
  return `graphflow.nodeConfigCollapse.v1:${workflowId ?? 0}:${nodeType}:${nodeId ?? ''}`
}

function readCollapseKeys(storageKey: string): string[] {
  try {
    const parsed = JSON.parse(globalThis.localStorage?.getItem(storageKey) ?? '[]') as unknown
    return Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === 'string') : []
  } catch {
    return []
  }
}

function writeCollapseKeys(storageKey: string, keys: string[]) {
  try {
    if (keys.length === 0) globalThis.localStorage?.removeItem(storageKey)
    else globalThis.localStorage?.setItem(storageKey, JSON.stringify(keys))
  } catch {
    /* localStorage may be unavailable in hardened browsers. */
  }
}

function normalizeCollapseKeys(keys: CollapseActiveKey): string[] {
  if (!keys) return []
  return Array.isArray(keys) ? keys : [keys]
}

function usePersistentCollapse(storageKey: string) {
  const [activeKey, setActiveKey] = useState<string[]>(() => readCollapseKeys(storageKey))
  useEffect(() => {
    setActiveKey(readCollapseKeys(storageKey))
  }, [storageKey])
  const onChange = (keys: CollapseActiveKey) => {
    const next = normalizeCollapseKeys(keys)
    setActiveKey(next)
    writeCollapseKeys(storageKey, next)
  }
  return { activeKey, onChange }
}

function ThinkingControls({ params, patchParams }: {
  params: Record<string, any>
  patchParams: (p: object) => void
}) {
  const thinking = withThinkingParamDefaults(params)
  return (
    <>
      <Field label="开启思考"><Switch checked={thinking.thinking_enabled}
        onChange={(v) => patchParams({ thinking_enabled: v })} /></Field>
      <Field label="思考力度"><Select style={{ width: 100 }}
        value={thinking.reasoning_effort} disabled={!thinking.thinking_enabled}
        onChange={(v) => patchParams({ reasoning_effort: v })}
        options={THINKING_EFFORT_OPTIONS.map((e) => ({ value: e, label: e }))} /></Field>
    </>
  )
}

function missingCols(text: string, inputCols: string[]): string[] {
  return extractTplVars(text).filter((c) => !inputCols.includes(c))
}

function referencedCols(text: string, inputCols: string[]): string[] {
  return extractTplVars(text).filter((c) => inputCols.includes(c))
}

// 切换某列在文本里的 {{列}} 引用：已存在则删除全部该列占位，否则在末尾追加
function toggleColRef(text: string, col: string): string {
  const esc = col.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  if (new RegExp('\\{\\{\\s*' + esc + '\\s*\\}\\}').test(text ?? '')) {
    return (text ?? '').replace(new RegExp('\\{\\{\\s*' + esc + '\\s*\\}\\}', 'g'), '')
  }
  return (text ?? '') + `{{${col}}}`
}

function hasColRef(text: any, col: string): boolean {
  const esc = col.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return new RegExp('\\{\\{\\s*' + esc + '\\s*\\}\\}').test(String(text ?? ''))
}

// 三态循环的纯函数：返回切换某列引用态后的新 config。
// 关键修复：移除 {{列}} 引用时，作用于「真正含该占位的字段」
// （http_fetch 搜 endpoint→url→body→headers→params，口径与上方 refText 一致），
// 而非盲目写默认字段——否则其它字段引用没被清掉，默认字段反被注入用户没写的 {{列}}。
export function cycleColRef(type: string, config: Record<string, any>, col: string): Record<string, any> {
  const dropped: string[] = config.drop_columns ?? []
  if (dropped.includes(col)) {
    return { ...config, drop_columns: dropped.filter((c) => c !== col) }
  }
  // 全新插入（灰→绿）落到默认字段即可。http_fetch 默认 endpoint，旧链路仅有 url 时沿用 url。
  const insertField = type === 'http_fetch'
    ? (config.endpoint === undefined && config.url !== undefined ? 'url' : 'endpoint')
    : 'user_prompt'
  // 移除（绿→红）：找到真正持有该占位的字段
  if (type === 'http_fetch') {
    for (const f of ['endpoint', 'url', 'body'] as const) {
      if (hasColRef(config[f], col)) {
        return { ...config, [f]: toggleColRef(config[f] ?? '', col), drop_columns: [...dropped, col] }
      }
    }
    for (const bag of ['headers', 'params'] as const) {
      const obj: Record<string, any> = config[bag] ?? {}
      const k = Object.keys(obj).find((key) => hasColRef(obj[key], col))
      if (k !== undefined) {
        return {
          ...config,
          [bag]: { ...obj, [k]: toggleColRef(obj[k] ?? '', col) },
          drop_columns: [...dropped, col],
        }
      }
    }
    // 未被任何字段引用：灰→绿，插入默认字段
    return { ...config, [insertField]: toggleColRef(config[insertField] ?? '', col) }
  }
  if (hasColRef(config[insertField], col)) {
    return { ...config, [insertField]: toggleColRef(config[insertField] ?? '', col), drop_columns: [...dropped, col] }
  }
  return { ...config, [insertField]: toggleColRef(config[insertField] ?? '', col) }
}

// 与后端 workflow_package.REDACTED 一致：喂给助手的 current_config 里的密钥被脱敏成此占位。
export const REDACTED_MARKER = '***REDACTED***'

// 应用助手返回的配置补丁：浅合并后，把 params/headers 里被模型回显的 ***REDACTED*** 占位
// 还原为本地现有真实值——避免脱敏标记覆盖用户真实 api_key/鉴权头（无本地值则丢弃该键）。
export function applyAssistPatch(config: Record<string, any>, patch: Record<string, any>): Record<string, any> {
  const merged: Record<string, any> = { ...config, ...patch }
  for (const bag of ['params', 'headers'] as const) {
    const m = merged[bag]
    if (m && typeof m === 'object' && !Array.isArray(m)) {
      const orig: Record<string, any> = config[bag] ?? {}
      const restored: Record<string, any> = {}
      for (const k of Object.keys(m)) {
        if (m[k] === REDACTED_MARKER) {
          if (k in orig) restored[k] = orig[k]   // 还原真实密钥；无本地值则丢弃占位
        } else {
          restored[k] = m[k]
        }
      }
      merged[bag] = restored
    }
  }
  return merged
}

function MissingColsWarning({ text, inputCols }: { text: string; inputCols: string[] }) {
  const miss = missingCols(text, inputCols)
  if (miss.length === 0) return null
  return (
    <div style={{ color: '#d4380d', fontSize: 12, marginTop: 4 }}>
      ⚠ 引用了上游未产出的列：{miss.map((c) => `{{${c}}}`).join('、')}
    </div>
  )
}

export function missingLibVars(promptVars: string[], inputCols: string[]): string[] {
  return promptVars.filter((v) => !inputCols.includes(v))
}

function LibraryPromptControl({ slot, config, patch, inputCols }: {
  slot: 'system_prompt' | 'user_prompt'
  config: Record<string, any>
  patch: (p: object) => void
  inputCols: string[]
}) {
  const [prompts, setPrompts] = useState<PromptSummary[]>([])
  const [pick, setPick] = useState<number | undefined>(undefined)
  useEffect(() => { void api.get<PromptSummary[]>('/api/prompts').then(setPrompts) }, [])
  const refId = config[`${slot}_ref`] as number | undefined
  const refName = prompts.find((p) => p.id === refId)?.name
  const picked = prompts.find((p) => p.id === pick)
  const miss = picked ? missingLibVars(picked.variables, inputCols) : []
  const copy = async () => {
    if (!pick) return
    const d = await api.get<PromptDetail>(`/api/prompts/${pick}`)
    patch({ [slot]: d.current.body, [`${slot}_ref`]: undefined })
  }
  const ref = () => { if (pick) patch({ [`${slot}_ref`]: pick }) }
  const unref = () => patch({ [`${slot}_ref`]: undefined })
  return (
    <div style={{ marginBottom: 6 }}>
      {refId ? (
        <div style={{ fontSize: 12, color: '#1677ff' }}>
          引用库提示词：{refName ?? `#${refId}`}（运行时取最新版）
          <a style={{ marginLeft: 8 }} onClick={unref}>解除引用</a>
        </div>
      ) : (
        <Space size={4} wrap>
          <span style={{ fontSize: 12, color: '#999' }}>从库：</span>
          <Select size="small" style={{ width: 160 }} placeholder="选择提示词" value={pick}
                  onChange={setPick}
                  options={prompts.map((p) => ({ value: p.id, label: p.name }))} />
          <a onClick={() => void copy()}>复制进来</a>
          <a onClick={ref}>引用</a>
          {miss.length > 0 && (
            <span style={{ color: '#d4380d', fontSize: 12 }}>
              缺列：{miss.map((c) => `{{${c}}}`).join('、')}
            </span>
          )}
        </Space>
      )}
    </div>
  )
}

const uniq = (arr: string[]) => arr.filter((c, i) => arr.indexOf(c) === i)

function liveOutput(type: string, config: Record<string, any>, inputCols: string[]): string[] {
  const drop = new Set<string>(config.drop_columns ?? [])
  const sub = (cols: string[]) => cols.filter((c) => !drop.has(c))
  if (type === 'llm_synth') {
    if ((config.output_mode ?? 'column') === 'json') return sub(uniq([...inputCols, ...(config.output_columns ?? [])]))
    return sub(uniq([...inputCols, config.output_column || 'output']))
  }
  if (type === 'auto_process') {
    let cols = [...inputCols]
    for (const op of config.operations ?? []) {
      if (op.op === 'rename') { const map = op.mapping ?? {}; cols = cols.map((c) => map[c] ?? c) }
      else if (op.op === 'drop') { const d = new Set(op.columns ?? []); cols = cols.filter((c) => !d.has(c)) }
      else if (op.op === 'concat') { if (op.target && !cols.includes(op.target)) cols = [...cols, op.target] }
      else if (op.op === 'agent') { cols = op.output_columns?.length ? uniq(op.output_columns) : cols }
    }
    return sub(cols)
  }
  if (type === 'http_fetch') return sub(uniq([...inputCols, ...Object.keys(config.extract ?? {})]))
  if (type === 'qc') return sub(uniq([...inputCols, config.status_column || 'qc_status', config.feedback_column || 'qc_feedback']))
  return sub(inputCols)   // qc / output 透传 - drop
}

function ColumnsBar({ inputCols, outputCols, referenced = [], dropped = [], onCycle }: {
  inputCols: string[]; outputCols: string[]; referenced?: string[]; dropped?: string[]
  onCycle?: (col: string) => void
}) {
  const refSet = new Set(referenced)
  const dropSet = new Set(dropped)
  const produced = outputCols.filter((c) => !inputCols.includes(c))
  const fed = inputCols.filter((c) => refSet.has(c) && !dropSet.has(c))
  const del = inputCols.filter((c) => dropSet.has(c))
  const colorOf = (c: string) => (dropSet.has(c) ? 'red' : refSet.has(c) ? 'green' : undefined)
  const inputList = (
    <div style={{ maxHeight: 280, overflowY: 'auto', maxWidth: 320 }}>
      {inputCols.length === 0
        ? <span style={{ color: '#bbb' }}>（无／先连好上游）</span>
        : inputCols.map((c) => (
          <Tag key={c} color={colorOf(c)}
               style={{ cursor: onCycle ? 'pointer' : 'default', marginBottom: 6 }}
               onClick={() => onCycle?.(c)}>{c}</Tag>))}
    </div>
  )
  const outputList = (
    <div style={{ maxHeight: 280, overflowY: 'auto', maxWidth: 320 }}>
      {outputCols.length === 0
        ? <span style={{ color: '#bbb' }}>（无）</span>
        : outputCols.map((c) => (
          <Tag key={c} color={produced.includes(c) ? 'blue' : undefined}
               style={{ marginBottom: 6 }}>{c}</Tag>))}
    </div>
  )
  return (
    <div style={{ background: '#fafafa', border: '1px solid #f0f0f0', borderRadius: 6, padding: 8, marginBottom: 12, fontSize: 12 }}>
      <div style={{ marginBottom: 6, display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 4 }}>
        <Popover trigger="click" placement="bottomLeft" content={inputList}
                 title={onCycle ? '点击列名循环：透传→喂模型→删除' : '全部输入列'}>
          <Button size="small">输入列 ({inputCols.length}) ▾</Button>
        </Popover>
        {fed.map((c) => <Tag key={c} color="green" style={{ margin: 0 }}>{c}</Tag>)}
        {del.map((c) => <Tag key={c} color="red" style={{ margin: 0 }}>{c}</Tag>)}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 4 }}>
        <Popover trigger="click" placement="bottomLeft" content={outputList} title="全部输出列">
          <Button size="small">输出列 ({outputCols.length}) ▾</Button>
        </Popover>
        {produced.map((c) => <Tag key={c} color="blue" style={{ margin: 0 }}>{c}</Tag>)}
      </div>
      {onCycle && <div style={{ color: '#999', marginTop: 6 }}>
        <span style={{ color: '#52c41a' }}>绿</span>=喂给模型；
        <span style={{ color: '#cf1322' }}>红</span>=删除(下游不可见)；
        <span style={{ color: '#1677ff' }}>蓝</span>=本节点新增；灰=透传保存。
      </div>}
    </div>
  )
}

function DatasetHeadPreview({ ds }: { ds: Dataset }) {
  const [rows, setRows] = useState<Record<string, any>[]>([])
  useEffect(() => {
    void api.get<RowsPage>(`/api/datasets/${ds.id}/rows?page=1&page_size=5`).then((r) => setRows(r.rows))
  }, [ds.id])
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ color: '#666', marginBottom: 4 }}>{ds.name}（前 {rows.length} 行 / 共 {ds.row_count}）</div>
      <Table
        size="small" rowKey={(_r, i) => String(i)} pagination={false} dataSource={rows}
        scroll={{ x: 'max-content' }}
        columns={ds.columns.map((c) => ({
          title: c, dataIndex: c, ellipsis: true, render: renderCell,
        }))}
      />
    </div>
  )
}

function InputNodeForm({ config, onChange, workflowId, nodeId }: FormProps & {
  workflowId?: number; nodeId?: string
}) {
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const collapse = usePersistentCollapse(nodeCollapseKey(workflowId, 'input', nodeId))
  useEffect(() => {
    void api.get<Dataset[]>('/api/datasets').then(setDatasets)
  }, [])
  const selected = (config.dataset_ids ?? [])
    .map((id: number) => datasets.find((d) => d.id === id))
    .filter(Boolean) as Dataset[]
  return (
    <Collapse activeKey={collapse.activeKey} onChange={collapse.onChange} items={[
      { key: 'dataset', label: '数据集', children: (
        <>
          <Field label="数据集（可多选，按行拼接）">
            <Select
              mode="multiple" style={{ width: '100%' }} value={config.dataset_ids ?? []}
              onChange={(v) => onChange({ ...config, dataset_ids: v })}
              options={datasets.map((d) => ({ value: d.id, label: `${d.name}（${d.row_count} 行）` }))}
            />
          </Field>
          {selected.map((d) => <DatasetHeadPreview key={d.id} ds={d} />)}
        </>
      ) },
    ]} />
  )
}

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
      // http_fetch 的 config.params 是 HTTP 查询参数（含 api_key），不是模型思考参数；
      // 别把它当模型参数透传，否则会把查询参数（甚至密钥）混进模型调用。
      current_config: config,
      params: withThinkingParamDefaults(nodeType === 'http_fetch' ? {} : config.params),
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

function LlmSynthForm({ config, onChange, workflowId, nodeId, inputCols }: FormProps & {
  workflowId?: number; nodeId?: string; inputCols: string[]
}) {
  const [models, setModels] = useState<ModelConfig[]>([])
  const collapse = usePersistentCollapse(nodeCollapseKey(workflowId, 'llm_synth', nodeId))
  useEffect(() => {
    void api.get<ModelConfig[]>('/api/models').then(setModels)
  }, [])
  const patch = (p: object) => onChange({ ...config, ...p })
  const params = config.params ?? {}
  const patchParams = (p: object) => onChange({ ...config, params: { ...params, ...p } })
  return (
    <>
      <NodeAssist nodeType="llm_synth" workflowId={workflowId} nodeId={nodeId} config={config}
                  onApply={(c) => onChange({ ...config, ...c })} />
      <Collapse activeKey={collapse.activeKey} onChange={collapse.onChange} items={[
        { key: 'model', label: '模型', children: (
          <Field label="模型">
            <Select
              style={{ width: '100%' }} value={config.model_config_id}
              onChange={(v) => patch({ model_config_id: v })}
              options={models.map((m) => ({ value: m.id, label: `${m.name}（${m.model_name}）` }))}
            />
          </Field>
        ) },
        { key: 'prompt', label: '提示词', children: (
          <>
            <Field label="System Prompt">
              <LibraryPromptControl slot="system_prompt" config={config} patch={patch} inputCols={inputCols} />
              <Input.TextArea rows={3} value={config.system_prompt ?? ''}
                              onChange={(e) => patch({ system_prompt: e.target.value })} />
            </Field>
            <Field label="User Prompt（用 {{列名}} 引用上游数据列）">
              <LibraryPromptControl slot="user_prompt" config={config} patch={patch} inputCols={inputCols} />
              <Input.TextArea rows={6} value={config.user_prompt ?? ''}
                              onChange={(e) => patch({ user_prompt: e.target.value })} />
              <MissingColsWarning text={config.user_prompt ?? ''} inputCols={inputCols} />
            </Field>
          </>
        ) },
        { key: 'advanced', label: '高级（输出 / 采样 / 参数）', children: (
          <>
            <Field label="输出方式">
              <Radio.Group value={config.output_mode ?? 'column'}
                           onChange={(e) => patch({ output_mode: e.target.value })}>
                <Radio.Button value="column">整段存到列</Radio.Button>
                <Radio.Button value="json">解析 JSON 拆多列</Radio.Button>
              </Radio.Group>
            </Field>
            {(config.output_mode ?? 'column') === 'json' && (
              <Field label="JSON 输出列（解析后拆出的列名，供下游识别）">
                <Select mode="tags" style={{ width: '100%' }} value={config.output_columns ?? []}
                        onChange={(v) => patch({ output_columns: v })} placeholder="如 q_en、category_en" />
              </Field>
            )}
            {(config.output_mode ?? 'column') === 'column' && (
              <Field label="输出列名">
                <Input value={config.output_column ?? 'output'}
                       onChange={(e) => patch({ output_column: e.target.value })} />
              </Field>
            )}
            <Space wrap>
              <Field label="扇出条数"><InputNumber min={1} value={config.fanout_n ?? 1}
                onChange={(v) => patch({ fanout_n: v ?? 1 })} /></Field>
              <Field label="节点并发"><InputNumber min={1} value={config.concurrency ?? 4}
                onChange={(v) => patch({ concurrency: v ?? 4 })} /></Field>
              <Field label="重试次数"><InputNumber min={1} value={config.retries ?? 3}
                onChange={(v) => patch({ retries: v ?? 3 })} /></Field>
            </Space>
            <Space wrap>
              <Field label="temperature"><InputNumber min={0} max={2} step={0.1} value={params.temperature}
                onChange={(v) => patchParams({ temperature: v })} /></Field>
              <Field label="top_p"><InputNumber min={0} max={1} step={0.05} value={params.top_p}
                onChange={(v) => patchParams({ top_p: v })} /></Field>
              <Field label="max_tokens"><InputNumber min={1} value={params.max_tokens}
                onChange={(v) => patchParams({ max_tokens: v })} /></Field>
              <Field label="超时(秒)"><InputNumber min={1} value={params.timeout ?? 120}
                onChange={(v) => patchParams({ timeout: v ?? 120 })} /></Field>
              <Field label="JSON 模式"><Switch checked={params.json_mode ?? false}
                onChange={(v) => patchParams({ json_mode: v })} /></Field>
              <ThinkingControls params={params} patchParams={patchParams} />
            </Space>
          </>
        ) },
      ]} />
    </>
  )
}

const OP_DEFAULTS: Record<string, Record<string, any>> = {
  dedup: { op: 'dedup', columns: [] },
  filter: { op: 'filter', column: '', mode: 'contains', value: '' },
  rename: { op: 'rename', mapping: {} },
  drop: { op: 'drop', columns: [] },
  concat: { op: 'concat', target: '', columns: [], sep: '' },
  cast: { op: 'cast', column: '', to: 'str' },
  sample: { op: 'sample', n: 100 },
  shuffle: { op: 'shuffle' },
  agent: { op: 'agent', instruction: '', code: '', output_columns: [] },
}
const OP_LABELS: Record<string, string> = {
  dedup: '去重', filter: '过滤', rename: '重命名', drop: '删除列',
  concat: '拼接列', cast: '类型转换', sample: '随机采样', shuffle: '打乱',
  agent: '智能处理',
}
const LEN_MODES = ['min_len', 'max_len']

function OpFields({ op, update }: { op: Record<string, any>; update: (p: object) => void }) {
  switch (op.op) {
    case 'dedup':
    case 'drop':
      return <Select mode="tags" placeholder="列名（去重留空=全列）" style={{ width: '100%' }}
                     value={op.columns} onChange={(v) => update({ columns: v })} />
    case 'filter':
      return (
        <Space wrap>
          <Input placeholder="列名" style={{ width: 100 }} value={op.column}
                 onChange={(e) => update({ column: e.target.value })} />
          <Select style={{ width: 120 }} value={op.mode} onChange={(v) => update({ mode: v })}
                  options={[
                    { value: 'min_len', label: '最小长度' }, { value: 'max_len', label: '最大长度' },
                    { value: 'contains', label: '包含' }, { value: 'not_contains', label: '不包含' },
                    { value: 'regex', label: '正则匹配' },
                  ]} />
          {LEN_MODES.includes(op.mode)
            ? <InputNumber placeholder="长度" value={op.value} onChange={(v) => update({ value: v })} />
            : <Input placeholder="值" style={{ width: 120 }} value={op.value}
                     onChange={(e) => update({ value: e.target.value })} />}
        </Space>
      )
    case 'rename': {
      const [from, to] = Object.entries(op.mapping ?? {})[0] ?? ['', '']
      return (
        <Space>
          <Input placeholder="原列名" style={{ width: 120 }} value={from}
                 onChange={(e) => update({ mapping: { [e.target.value]: to } })} />
          →
          <Input placeholder="新列名" style={{ width: 120 }} value={to as string}
                 onChange={(e) => update({ mapping: { [from]: e.target.value } })} />
        </Space>
      )
    }
    case 'concat':
      return (
        <Space wrap>
          <Select mode="tags" placeholder="来源列" style={{ minWidth: 160 }}
                  value={op.columns} onChange={(v) => update({ columns: v })} />
          <Input placeholder="分隔符" style={{ width: 80 }} value={op.sep}
                 onChange={(e) => update({ sep: e.target.value })} />
          <Input placeholder="目标列" style={{ width: 100 }} value={op.target}
                 onChange={(e) => update({ target: e.target.value })} />
        </Space>
      )
    case 'cast':
      return (
        <Space>
          <Input placeholder="列名" style={{ width: 120 }} value={op.column}
                 onChange={(e) => update({ column: e.target.value })} />
          <Select style={{ width: 90 }} value={op.to} onChange={(v) => update({ to: v })}
                  options={['str', 'int', 'float'].map((t) => ({ value: t, label: t }))} />
        </Space>
      )
    case 'sample':
      return <InputNumber addonBefore="保留" addonAfter="条" value={op.n}
                          onChange={(v) => update({ n: v })} />
    default:
      return null
  }
}

function AgentOpFields({ op, update, workflowId, nodeId, opIndex }: {
  op: Record<string, any>; update: (p: object) => void
  workflowId?: number; nodeId?: string; opIndex: number
}) {
  const [models, setModels] = useState<ModelConfig[]>([])
  const [busy, setBusy] = useState(false)
  const [info, setInfo] = useState('')
  const key = `graphflow.nodeAssistant.v1:${workflowId ?? 0}:auto_process:${nodeId ?? ''}:agent:${opIndex}`
  const st = useNodeAssist(key)
  const modelSel = st.modelConfigId
  const params = op.params ?? {}
  const patchParams = (p: object) => update({ params: { ...params, ...p } })
  useEffect(() => {
    void api.get<ModelConfig[]>('/api/models').then(setModels)
  }, [])
  const generate = async () => {
    if (!modelSel || !workflowId || !nodeId) return
    setBusy(true)
    setInfo('')
    try {
      const r = await api.post<CodegenOut>('/api/agent/codegen',
        buildCodegenPayload(workflowId, nodeId, modelSel, op))
      update({ code: r.code, output_columns: r.output_columns })
      if (r.sample_source === 'none') setInfo('未检测到上游列（先连好上游/上传数据集），AI 仅按指令生成')
    } catch (e) {
      setInfo((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div>
      <Input.TextArea rows={2} value={op.instruction} placeholder="自然语言指令，如：把 q 列翻译成英文存到 q_en，删掉空行"
                      onChange={(e) => update({ instruction: e.target.value })} />
      <Space style={{ margin: '8px 0' }}>
        <Select size="small" style={{ width: 150 }} placeholder="生成用模型" value={modelSel}
                onChange={(v) => setModelConfigId(key, v)}
                options={models.map((m) => ({ value: m.id, label: m.name }))} />
        <Button size="small" loading={busy} disabled={!op.instruction || !modelSel}
                onClick={() => void generate()}>生成代码</Button>
      </Space>
      <Space wrap>
        <ThinkingControls params={params} patchParams={patchParams} />
      </Space>
      {info && <div style={{ color: '#d46b08', fontSize: 12, marginBottom: 4 }}>{info}</div>}
      {op.code && (
        <Input.TextArea rows={8} style={{ fontFamily: 'monospace', fontSize: 12 }} value={op.code}
                        onChange={(e) => update({ code: e.target.value })} />
      )}
      {op.code && (
        <div style={{ marginTop: 8 }}>
          <div style={{ color: '#666', fontSize: 12, marginBottom: 4 }}>产出列（本操作运行后的全部列，AI 已填，可改）</div>
          <Select mode="tags" style={{ width: '100%' }} value={op.output_columns ?? []}
                  onChange={(v) => update({ output_columns: v })} placeholder="如 q_english" />
        </div>
      )}
    </div>
  )
}

function AutoProcessForm({ config, onChange, workflowId, nodeId }: FormProps & {
  workflowId?: number; nodeId?: string
}) {
  const ops: Record<string, any>[] = config.operations ?? []
  const collapse = usePersistentCollapse(nodeCollapseKey(workflowId, 'auto_process', nodeId))
  const setOps = (next: Record<string, any>[]) => onChange({ ...config, operations: next })
  return (
    <Collapse activeKey={collapse.activeKey} onChange={collapse.onChange} items={[
      { key: 'ops', label: '处理操作', children: (
        <>
          {ops.map((op, i) => (
            <div key={i} style={{ border: '1px solid #eee', borderRadius: 6, padding: 8, marginBottom: 8 }}>
              <Space style={{ marginBottom: 8 }}>
                <Select style={{ width: 130 }} value={op.op}
                        onChange={(v) => setOps(ops.map((o, j) => (j === i ? { ...OP_DEFAULTS[v] } : o)))}
                        options={Object.entries(OP_LABELS).map(([v, l]) => ({ value: v, label: l }))} />
                <a onClick={() => setOps(ops.filter((_, j) => j !== i))}>删除</a>
              </Space>
              {op.op === 'agent'
                ? <AgentOpFields op={op} workflowId={workflowId} nodeId={nodeId}
                                 opIndex={i}
                                 update={(p) => setOps(ops.map((o, j) => (j === i ? { ...o, ...p } : o)))} />
                : <OpFields op={op} update={(p) => setOps(ops.map((o, j) => (j === i ? { ...o, ...p } : o)))} />}
            </div>
          ))}
          <Space direction="vertical" style={{ width: '100%' }}>
            <Button block onClick={() => setOps([...ops, { ...OP_DEFAULTS.dedup }])}>+ 添加操作</Button>
            <Button block type="dashed" onClick={() => setOps([...ops, { ...OP_DEFAULTS.agent }])}>
              ✨ 用 AI 写处理代码
            </Button>
            <div style={{ color: '#999', fontSize: 12 }}>复杂处理（如按 session 分组去重）建议用 AI 写代码。</div>
          </Space>
        </>
      ) },
    ]} />
  )
}

function QcForm({ config, onChange, workflowId, nodeId, inputCols }: FormProps & {
  workflowId?: number; nodeId?: string; inputCols: string[]
}) {
  const [models, setModels] = useState<ModelConfig[]>([])
  const collapse = usePersistentCollapse(nodeCollapseKey(workflowId, 'qc', nodeId))
  useEffect(() => {
    void api.get<ModelConfig[]>('/api/models').then(setModels)
  }, [])
  const patch = (p: object) => onChange({ ...config, ...p })
  const params = config.params ?? {}
  const patchParams = (p: object) => onChange({ ...config, params: { ...params, ...p } })
  return (
    <>
      <NodeAssist nodeType="qc" workflowId={workflowId} nodeId={nodeId} config={config}
                  onApply={(c) => onChange({ ...config, ...c })} />
      <Collapse activeKey={collapse.activeKey} onChange={collapse.onChange} items={[
        { key: 'judge', label: '判定', children: (
          <>
            <Field label="判定模型（多选，N 个模型同提示词判定）">
              <Select mode="multiple" style={{ width: '100%' }}
                      value={config.judge_model_ids ?? (config.model_config_id ? [config.model_config_id] : [])}
                      onChange={(v) => patch({ judge_model_ids: v })}
                      options={models.map((m) => ({ value: m.id, label: `${m.name}（${m.model_name}）` }))} />
            </Field>
            <Field label="至少通过数 K（≥K 个模型通过即输出）">
              <InputNumber min={1} value={config.pass_k ?? 1} onChange={(v) => patch({ pass_k: v ?? 1 })} />
            </Field>
          </>
        ) },
        { key: 'prompt', label: '提示词', children: (
          <>
            <Field label='System Prompt（判定规则；要求模型只输出 {"status":"pass"|"failed"|...,"reason":"..."}，只有 "pass" 算通过）'>
              <LibraryPromptControl slot="system_prompt" config={config} patch={patch} inputCols={inputCols} />
              <Input.TextArea rows={3} value={config.system_prompt ?? ''}
                              onChange={(e) => patch({ system_prompt: e.target.value })} />
            </Field>
            <Field label="User Prompt（用 {{列名}} 引用上游数据列）">
              <LibraryPromptControl slot="user_prompt" config={config} patch={patch} inputCols={inputCols} />
              <Input.TextArea rows={5} value={config.user_prompt ?? ''}
                              onChange={(e) => patch({ user_prompt: e.target.value })} />
              <MissingColsWarning text={config.user_prompt ?? ''} inputCols={inputCols} />
            </Field>
          </>
        ) },
        { key: 'advanced', label: '高级（回扫 / 反馈 / 参数）', children: (
          <>
            <Field label="最多回扫轮数">
              <InputNumber min={0} value={config.max_rounds ?? 3}
                           onChange={(v) => patch({ max_rounds: v ?? 3 })} />
            </Field>
            <Field label="状态列名">
              <Input value={config.status_column ?? 'qc_status'}
                     onChange={(e) => patch({ status_column: e.target.value || 'qc_status' })} />
            </Field>
            <Field label="反馈列名">
              <Input value={config.feedback_column ?? 'qc_feedback'}
                     onChange={(e) => patch({ feedback_column: e.target.value || 'qc_feedback' })} />
            </Field>
            <Space wrap>
              <Field label="temperature"><InputNumber min={0} max={2} step={0.1} value={params.temperature}
                onChange={(v) => patchParams({ temperature: v })} /></Field>
              <Field label="top_p"><InputNumber min={0} max={1} step={0.05} value={params.top_p}
                onChange={(v) => patchParams({ top_p: v })} /></Field>
              <Field label="max_tokens"><InputNumber min={1} value={params.max_tokens}
                onChange={(v) => patchParams({ max_tokens: v })} /></Field>
              <Field label="超时(秒)"><InputNumber min={1} value={params.timeout ?? 120}
                onChange={(v) => patchParams({ timeout: v ?? 120 })} /></Field>
              <ThinkingControls params={params} patchParams={patchParams} />
            </Space>
            <div style={{ color: '#999', fontSize: 12 }}>判定默认 temperature 0（确定性）；留空即用 0。</div>
            <div style={{ color: '#999', fontSize: 12 }}>
              把质检节点底部的橙色圆点拖回上游 LLM 节点形成回扫边；不通过的行带原因重生成，满 N 轮仍不过则丢弃。
            </div>
          </>
        ) },
      ]} />
    </>
  )
}

function OutputNodeForm({ config, onChange, workflowId, nodeId }: FormProps & {
  workflowId?: number; nodeId?: string
}) {
  const collapse = usePersistentCollapse(nodeCollapseKey(workflowId, 'output', nodeId))
  return (
    <Collapse activeKey={collapse.activeKey} onChange={collapse.onChange} items={[
      { key: 'output', label: '输出', children: (
        <>
          <Field label="同时保存为新数据集">
            <Switch checked={config.save_as_dataset ?? false}
                    onChange={(v) => onChange({ ...config, save_as_dataset: v })} />
          </Field>
          {config.save_as_dataset && (
            <Field label="数据集名称">
              <Input value={config.dataset_name ?? ''}
                     onChange={(e) => onChange({ ...config, dataset_name: e.target.value })} />
            </Field>
          )}
          <Field label="最多输出行数">
            <InputNumber min={1} value={config.count ?? undefined} style={{ width: 160 }}
                         placeholder="留空=不限"
                         onChange={(v) => onChange({ ...config, count: v ?? undefined })} />
          </Field>
          <div style={{ color: '#999' }}>设了行数：合成产够即停、省大模型调用（失败/质检淘汰都不补，最终 ≤ 该数）。
            仅对「单链：合成→(质检)→本输出」省成本；遇分叉/多父合并按全量跑、只在此截断。
            导出文件在运行详情页选择格式下载。</div>
        </>
      ) },
    ]} />
  )
}

function KvEditor({ pairs, onChange, keyPlaceholder, valPlaceholder }: {
  pairs: Record<string, string>; onChange: (p: Record<string, string>) => void
  keyPlaceholder: string; valPlaceholder: string
}) {
  const entries = Object.entries(pairs)
  const setEntry = (i: number, k: string, v: string) => {
    const next: Record<string, string> = {}
    entries.forEach(([ek, ev], j) => {
      const [nk, nv] = j === i ? [k, v] : [ek, ev]
      if (nk) next[nk] = nv
    })
    onChange(next)
  }
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      {entries.map(([k, v], i) => (
        <Space key={i}>
          <Input placeholder={keyPlaceholder} style={{ width: 150 }} value={k}
                 onChange={(e) => setEntry(i, e.target.value, v)} />
          <Input placeholder={valPlaceholder} style={{ width: 220 }} value={v}
                 onChange={(e) => setEntry(i, k, e.target.value)} />
          <a onClick={() => onChange(Object.fromEntries(entries.filter((_, j) => j !== i)))}>删除</a>
        </Space>
      ))}
      <Button size="small" onClick={() => onChange({ ...pairs, '': '' })}>+ 添加</Button>
    </Space>
  )
}

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
                  onApply={(c) => onChange(applyAssistPatch(config, c))} />
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

export default function NodeConfigForm({ type, config, onChange, workflowId, nodeId }: FormProps & {
  type: string; workflowId?: number; nodeId?: string
}) {
  const [colsMap, setColsMap] = useState<ColumnsMap>({})
  useEffect(() => {
    if (workflowId) void api.get<ColumnsMap>(`/api/workflows/${workflowId}/columns`).then(setColsMap).catch(() => {})
  }, [workflowId, nodeId])
  const nodeCols = (nodeId && colsMap[nodeId]) || { input: [], output: [] }
  const inputCols = nodeCols.input
  const outputCols = type === 'input' ? nodeCols.output : liveOutput(type, config, inputCols)
  // 绿标：本节点模板字段里 {{列}} 引用到、且确实在输入列中的列 = 实际用到的列
  const refText = type === 'http_fetch'
    ? [config.endpoint ?? config.url, config.body, ...Object.values(config.params ?? {}), ...Object.values(config.headers ?? {})].filter(Boolean).map(String).join('\n')
    : `${config.system_prompt ?? ''}\n${config.user_prompt ?? ''}`
  const referenced = referencedCols(refText, inputCols)
  const canInsert = type === 'llm_synth' || type === 'qc' || type === 'http_fetch'
  const dropped: string[] = config.drop_columns ?? []
  // 三态循环：灰(透传)→绿(喂模型,插{{列}})→红(删除,移{{列}}并入 drop_columns)→灰
  const cycle = (col: string) => onChange(cycleColRef(type, config, col))
  const bar = type === 'input' ? null : (
    <ColumnsBar inputCols={inputCols} outputCols={outputCols} referenced={referenced}
                dropped={dropped} onCycle={canInsert ? cycle : undefined} />
  )
  switch (type) {
    case 'input':
      return <InputNodeForm config={config} onChange={onChange} workflowId={workflowId} nodeId={nodeId} />
    case 'llm_synth':
      return <>{bar}<LlmSynthForm config={config} onChange={onChange} workflowId={workflowId} nodeId={nodeId} inputCols={inputCols} /></>
    case 'auto_process':
      return <>{bar}<AutoProcessForm config={config} onChange={onChange} workflowId={workflowId} nodeId={nodeId} /></>
    case 'qc':
      return <>{bar}<QcForm config={config} onChange={onChange} workflowId={workflowId} nodeId={nodeId} inputCols={inputCols} /></>
    case 'http_fetch':
      return <>{bar}<HttpFetchForm config={config} onChange={onChange} workflowId={workflowId} nodeId={nodeId} inputCols={inputCols} /></>
    case 'output':
      return <>{bar}<OutputNodeForm config={config} onChange={onChange} workflowId={workflowId} nodeId={nodeId} /></>
    default:
      return null
  }
}
