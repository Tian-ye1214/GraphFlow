import { useEffect, useState } from 'react'
import { Button, Input, InputNumber, Radio, Select, Space, Switch, Table, Tag } from 'antd'
import { api } from '../../api/client'
import type { CodegenOut, ColumnsMap, Dataset, ModelConfig, NodeAssistOut, RowsPage } from '../../api/types'

export interface FormProps {
  config: Record<string, any>
  onChange: (config: Record<string, any>) => void
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 12 }}>
      <div style={{ marginBottom: 4, color: '#666' }}>{label}</div>
      {children}
    </div>
  )
}

const TPL_RE = /\{\{\s*([^{}]+?)\s*\}\}/g
function missingCols(text: string, inputCols: string[]): string[] {
  const out: string[] = []
  for (const m of (text ?? '').matchAll(TPL_RE)) {
    if (!inputCols.includes(m[1]) && !out.includes(m[1])) out.push(m[1])
  }
  return out
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

const uniq = (arr: string[]) => arr.filter((c, i) => arr.indexOf(c) === i)

function liveOutput(type: string, config: Record<string, any>, inputCols: string[]): string[] {
  if (type === 'llm_synth') {
    if ((config.output_mode ?? 'column') === 'json') return uniq([...inputCols, ...(config.output_columns ?? [])])
    return uniq([...inputCols, config.output_column || 'output'])
  }
  if (type === 'auto_process') {
    let cols = [...inputCols]
    for (const op of config.operations ?? []) {
      if (op.op === 'rename') { const map = op.mapping ?? {}; cols = cols.map((c) => map[c] ?? c) }
      else if (op.op === 'drop') { const d = new Set(op.columns ?? []); cols = cols.filter((c) => !d.has(c)) }
      else if (op.op === 'concat') { if (op.target && !cols.includes(op.target)) cols = [...cols, op.target] }
      else if (op.op === 'agent') { cols = op.output_columns?.length ? uniq(op.output_columns) : cols }
    }
    return cols
  }
  return inputCols
}

function ColumnsBar({ inputCols, outputCols, onInsert }: {
  inputCols: string[]; outputCols: string[]; onInsert?: (col: string) => void
}) {
  return (
    <div style={{ background: '#fafafa', border: '1px solid #f0f0f0', borderRadius: 6, padding: 8, marginBottom: 12, fontSize: 12 }}>
      <div style={{ color: '#666', marginBottom: 4 }}>
        输入列：{inputCols.length === 0
          ? <span style={{ color: '#bbb' }}>（无／先连好上游）</span>
          : inputCols.map((c) => (
            <Tag key={c} style={{ cursor: onInsert ? 'pointer' : 'default', marginInlineEnd: 4 }}
                 onClick={() => onInsert?.(c)}>{c}</Tag>))}
      </div>
      <div style={{ color: '#666' }}>
        输出列：{outputCols.length === 0
          ? <span style={{ color: '#bbb' }}>（无）</span>
          : outputCols.map((c) => <Tag key={c} color="blue" style={{ marginInlineEnd: 4 }}>{c}</Tag>)}
      </div>
      {onInsert && <div style={{ color: '#999', marginTop: 4 }}>点输入列标签即可插入 {'{{列}}'} 到 User Prompt</div>}
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
          title: c, dataIndex: c, ellipsis: true,
          render: (v: unknown) => (typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v ?? '')),
        }))}
      />
    </div>
  )
}

function InputNodeForm({ config, onChange }: FormProps) {
  const [datasets, setDatasets] = useState<Dataset[]>([])
  useEffect(() => {
    void api.get<Dataset[]>('/api/datasets').then(setDatasets)
  }, [])
  const selected = (config.dataset_ids ?? [])
    .map((id: number) => datasets.find((d) => d.id === id))
    .filter(Boolean) as Dataset[]
  return (
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
  )
}

function NodeAssist({ nodeType, workflowId, nodeId, onApply }: {
  nodeType: string; workflowId?: number; nodeId?: string
  onApply: (config: Record<string, any>) => void
}) {
  const [models, setModels] = useState<ModelConfig[]>([])
  const [modelSel, setModelSel] = useState<number>()
  const [instruction, setInstruction] = useState('')
  const [busy, setBusy] = useState(false)
  const [info, setInfo] = useState('')
  useEffect(() => {
    void api.get<ModelConfig[]>('/api/models').then(setModels)
  }, [])
  const run = async () => {
    if (!modelSel || !workflowId || !nodeId) return
    setBusy(true)
    setInfo('')
    try {
      const r = await api.post<NodeAssistOut>('/api/agent/node-assist', {
        workflow_id: workflowId, node_id: nodeId, node_type: nodeType,
        instruction, model_config_id: modelSel,
      })
      onApply(r.config)
      if (r.sample_source === 'none') setInfo('未检测到上游列，可先连好上游')
    } catch (e) {
      setInfo((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div style={{ border: '1px dashed #d9d9d9', borderRadius: 6, padding: 8, marginBottom: 12 }}>
      <div style={{ color: '#722ed1', marginBottom: 4 }}>RedLotus 助手：描述需求，自动写提示词</div>
      <Input.TextArea rows={2} value={instruction} placeholder="如：把 q 列翻译成英文存到 q_en"
                      onChange={(e) => setInstruction(e.target.value)} />
      <Space style={{ marginTop: 8 }}>
        <Select size="small" style={{ width: 150 }} placeholder="生成用模型" value={modelSel}
                onChange={setModelSel} options={models.map((m) => ({ value: m.id, label: m.name }))} />
        <Button size="small" loading={busy} disabled={!instruction || !modelSel}
                onClick={() => void run()}>让 RedLotus 配置</Button>
      </Space>
      {info && <div style={{ color: '#d46b08', fontSize: 12, marginTop: 4 }}>{info}</div>}
    </div>
  )
}

function LlmSynthForm({ config, onChange, workflowId, nodeId, inputCols }: FormProps & {
  workflowId?: number; nodeId?: string; inputCols: string[]
}) {
  const [models, setModels] = useState<ModelConfig[]>([])
  useEffect(() => {
    void api.get<ModelConfig[]>('/api/models').then(setModels)
  }, [])
  const patch = (p: object) => onChange({ ...config, ...p })
  const params = config.params ?? {}
  const patchParams = (p: object) => onChange({ ...config, params: { ...params, ...p } })
  return (
    <>
      <NodeAssist nodeType="llm_synth" workflowId={workflowId} nodeId={nodeId}
                  onApply={(c) => onChange({ ...config, ...c })} />
      <Field label="模型">
        <Select
          style={{ width: '100%' }} value={config.model_config_id}
          onChange={(v) => patch({ model_config_id: v })}
          options={models.map((m) => ({ value: m.id, label: `${m.name}（${m.model_name}）` }))}
        />
      </Field>
      <Field label="System Prompt">
        <Input.TextArea rows={3} value={config.system_prompt ?? ''}
                        onChange={(e) => patch({ system_prompt: e.target.value })} />
      </Field>
      <Field label="User Prompt（用 {{列名}} 引用上游数据列）">
        <Input.TextArea rows={6} value={config.user_prompt ?? ''}
                        onChange={(e) => patch({ user_prompt: e.target.value })} />
        <MissingColsWarning text={config.user_prompt ?? ''} inputCols={inputCols} />
      </Field>
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
      </Space>
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

function AgentOpFields({ op, update, workflowId, nodeId }: {
  op: Record<string, any>; update: (p: object) => void
  workflowId?: number; nodeId?: string
}) {
  const [models, setModels] = useState<ModelConfig[]>([])
  const [modelSel, setModelSel] = useState<number>()
  const [busy, setBusy] = useState(false)
  const [info, setInfo] = useState('')
  useEffect(() => {
    void api.get<ModelConfig[]>('/api/models').then(setModels)
  }, [])
  const generate = async () => {
    if (!modelSel || !workflowId || !nodeId) return
    setBusy(true)
    setInfo('')
    try {
      const r = await api.post<CodegenOut>('/api/agent/codegen', {
        workflow_id: workflowId, node_id: nodeId,
        instruction: op.instruction, model_config_id: modelSel,
      })
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
                onChange={setModelSel} options={models.map((m) => ({ value: m.id, label: m.name }))} />
        <Button size="small" loading={busy} disabled={!op.instruction || !modelSel}
                onClick={() => void generate()}>生成代码</Button>
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
  const setOps = (next: Record<string, any>[]) => onChange({ ...config, operations: next })
  return (
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
  )
}

function QcForm({ config, onChange, workflowId, nodeId, inputCols }: FormProps & {
  workflowId?: number; nodeId?: string; inputCols: string[]
}) {
  const [models, setModels] = useState<ModelConfig[]>([])
  useEffect(() => {
    void api.get<ModelConfig[]>('/api/models').then(setModels)
  }, [])
  const patch = (p: object) => onChange({ ...config, ...p })
  const params = config.params ?? {}
  const patchParams = (p: object) => onChange({ ...config, params: { ...params, ...p } })
  return (
    <>
      <NodeAssist nodeType="qc" workflowId={workflowId} nodeId={nodeId}
                  onApply={(c) => onChange({ ...config, ...c })} />
      <Field label="判定模型（多选，N 个模型同提示词判定）">
        <Select mode="multiple" style={{ width: '100%' }}
                value={config.judge_model_ids ?? (config.model_config_id ? [config.model_config_id] : [])}
                onChange={(v) => patch({ judge_model_ids: v })}
                options={models.map((m) => ({ value: m.id, label: `${m.name}（${m.model_name}）` }))} />
      </Field>
      <Field label="至少通过数 K（≥K 个模型通过即输出）">
        <InputNumber min={1} value={config.pass_k ?? 1} onChange={(v) => patch({ pass_k: v ?? 1 })} />
      </Field>
      <Field label='System Prompt（判定规则；要求模型只输出 {"pass":true|false,"reason":"..."}）'>
        <Input.TextArea rows={3} value={config.system_prompt ?? ''}
                        onChange={(e) => patch({ system_prompt: e.target.value })} />
      </Field>
      <Field label="User Prompt（用 {{列名}} 引用上游数据列）">
        <Input.TextArea rows={5} value={config.user_prompt ?? ''}
                        onChange={(e) => patch({ user_prompt: e.target.value })} />
        <MissingColsWarning text={config.user_prompt ?? ''} inputCols={inputCols} />
      </Field>
      <Field label="最多回扫轮数">
        <InputNumber min={0} value={config.max_rounds ?? 3}
                     onChange={(v) => patch({ max_rounds: v ?? 3 })} />
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
      </Space>
      <div style={{ color: '#999', fontSize: 12 }}>判定默认 temperature 0（确定性）；留空即用 0。</div>
      <div style={{ color: '#999', fontSize: 12 }}>
        把质检节点底部的橙色圆点拖回上游 LLM 节点形成回扫边；不通过的行带原因重生成，满 N 轮仍不过则丢弃。
      </div>
    </>
  )
}

function OutputNodeForm({ config, onChange }: FormProps) {
  return (
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
      <div style={{ color: '#999' }}>导出文件在运行详情页选择格式下载。</div>
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
  const outputCols = type === 'llm_synth' || type === 'auto_process'
    ? liveOutput(type, config, inputCols) : nodeCols.output
  const canInsert = type === 'llm_synth' || type === 'qc'
  const bar = type === 'input' ? null : (
    <ColumnsBar inputCols={inputCols} outputCols={outputCols}
                onInsert={canInsert
                  ? (c) => onChange({ ...config, user_prompt: (config.user_prompt ?? '') + `{{${c}}}` })
                  : undefined} />
  )
  switch (type) {
    case 'input':
      return <InputNodeForm config={config} onChange={onChange} />
    case 'llm_synth':
      return <>{bar}<LlmSynthForm config={config} onChange={onChange} workflowId={workflowId} nodeId={nodeId} inputCols={inputCols} /></>
    case 'auto_process':
      return <>{bar}<AutoProcessForm config={config} onChange={onChange} workflowId={workflowId} nodeId={nodeId} /></>
    case 'qc':
      return <>{bar}<QcForm config={config} onChange={onChange} workflowId={workflowId} nodeId={nodeId} inputCols={inputCols} /></>
    case 'output':
      return <>{bar}<OutputNodeForm config={config} onChange={onChange} /></>
    default:
      return null
  }
}
