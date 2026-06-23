// 表格单元格渲染：对象/数组 → JSON 串，其余 → 字符串（null/undefined → 空串）。
export function renderCell(v: unknown): string {
  return typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v ?? '')
}

// 运行时长：start→end（end 缺省=进行中，按当前时间算）；start 缺省返回占位符。
export function fmtDuration(start: string | null, end: string | null): string {
  if (!start) return '—'
  const sec = Math.max(0, Math.round(((end ? new Date(end).getTime() : Date.now()) - new Date(start).getTime()) / 1000))
  const m = Math.floor(sec / 60)
  return m ? `${m}分${sec % 60}秒` : `${sec}秒`
}

// 提取模板里的 {{变量}} 名（按首次出现顺序去重）。
const TPL_RE = /\{\{\s*([^{}]+?)\s*\}\}/g
export function extractTplVars(text: string): string[] {
  const out: string[] = []
  for (const m of (text ?? '').matchAll(TPL_RE)) {
    if (!out.includes(m[1])) out.push(m[1])
  }
  return out
}
