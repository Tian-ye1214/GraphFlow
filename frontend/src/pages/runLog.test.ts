import { describe, expect, it } from 'vitest'
import { formatRunLog } from './runLog'

describe('formatRunLog', () => {
  it('每条渲染成带时间戳与等级的一行', () => {
    const text = formatRunLog([
      { created_at: 't1', node_id: '', level: 'info', message: '运行开始' },
      { created_at: 't2', node_id: 'gen', level: 'error', message: '节点 gen 失败' },
    ])
    expect(text).toBe('[t1] INFO 运行开始\n[t2] ERROR 节点 gen 失败')
  })
})
