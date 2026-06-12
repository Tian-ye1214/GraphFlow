import { describe, expect, it } from 'vitest'
import { extractConfirmDeletes, stripGoalMarkers } from './parse'

describe('stripGoalMarkers', () => {
  it('去掉 CONTINUE/DONE 标记', () => {
    expect(stripGoalMarkers('推进中 <!-- REDLOTUS_GOAL:CONTINUE -->')).toBe('推进中')
    expect(stripGoalMarkers('完成 <!--  redlotus_goal : DONE -->')).toBe('完成')
  })
  it('无标记原样返回', () => {
    expect(stripGoalMarkers('普通回复')).toBe('普通回复')
  })
})

describe('extractConfirmDeletes', () => {
  it('提取确认命令并从正文移除', () => {
    const r = extractConfirmDeletes('要删两个资源\n[confirm_delete] gf data rm 种子集\n[confirm_delete] gf wf rm 旧流水线')
    expect(r.commands).toEqual(['gf data rm 种子集', 'gf wf rm 旧流水线'])
    expect(r.text).toBe('要删两个资源')
  })
  it('无确认块', () => {
    expect(extractConfirmDeletes('正常').commands).toEqual([])
  })
})
