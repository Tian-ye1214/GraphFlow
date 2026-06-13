import { Handle, Position, type NodeProps } from '@xyflow/react'
import { NODE_LABELS } from './serialize'

const COLORS: Record<string, string> = {
  input: '#1677ff', llm_synth: '#722ed1', auto_process: '#13c2c2', output: '#52c41a', qc: '#fa8c16',
}

function GFNode({ id, type, selected }: NodeProps) {
  const t = type as keyof typeof NODE_LABELS
  return (
    <div style={{
      background: '#fff', borderRadius: 8, padding: '8px 16px', minWidth: 130,
      border: `2px solid ${COLORS[t]}`, boxShadow: selected ? `0 0 0 3px ${COLORS[t]}55` : 'none',
    }}>
      {t !== 'input' && <Handle type="target" position={Position.Left} />}
      <div style={{ fontSize: 12, color: COLORS[t] }}>{NODE_LABELS[t]}</div>
      <div style={{ fontWeight: 600 }}>{id}</div>
      {t !== 'output' && <Handle type="source" position={Position.Right} />}
      {/* 质检节点底部的回扫源点：从这里连回上游 LLM 即生成 rescan 回扫边 */}
      {t === 'qc' && (
        <Handle id="rescan" type="source" position={Position.Bottom}
                title="回扫：连回上游 LLM 节点" style={{ background: '#fa8c16' }} />
      )}
    </div>
  )
}

export const nodeTypes = {
  input: GFNode, llm_synth: GFNode, auto_process: GFNode, output: GFNode, qc: GFNode,
}
