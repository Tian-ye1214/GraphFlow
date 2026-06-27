# 节点复制（Ctrl+C / Ctrl+V）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在画布上用 Ctrl/⌘+C 复制选中节点、Ctrl/⌘+V 粘贴出一个去掉提示词、其余配置照搬、显示名自增的副本。

**Architecture:** 纯前端。`serialize.ts` 加两个可单测纯函数（`stripPrompt` 去提示词字段、`copyLabel` 显示名自增）；`CanvasPage.tsx` 加一段 `window` keydown 监听把它们接成复制/粘贴。无后端、无新依赖、无 schema 改动。

**Tech Stack:** React 19 + TypeScript + @xyflow/react（React Flow）+ Vitest（jsdom）。

## Global Constraints

- 只清提示词字段：`system_prompt`、`user_prompt`、`system_prompt_ref`、`user_prompt_ref`（这 4 个，含库引用）；其余配置一律保留。
- 显示名后缀数字自增 `_2`/`_3`（撞名继续递增），仅作用于有显示名的节点。
- 深拷贝用 `JSON.parse(JSON.stringify(...))`（config 一定可 JSON 序列化），不用 `structuredClone`。
- 焦点在 `input`/`textarea`/`[contenteditable="true"]` 内时 Ctrl+C/V 一律放行系统默认（不劫持）。
- 单节点复制、不复制连线、不接系统剪贴板。
- 提交信息中文、不出现 claude、不加 Co-Authored-By。
- 前端目录 `E:\代码\GraphFlow\frontend` 下跑命令。

---

### Task 1: 纯函数 `stripPrompt` / `copyLabel`（TDD）

**Files:**
- Modify: `frontend/src/canvas/serialize.ts`（在 `displayName` 之后追加导出）
- Test: `frontend/src/canvas/serialize.test.ts`（追加一个 describe）

**Interfaces:**
- Consumes: 现有 `displayName(label, id)`（同文件，无需改动）。
- Produces：
  - `PROMPT_KEYS: readonly string[]`
  - `stripPrompt(config: Record<string, any>): Record<string, any>` —— 深拷贝并删 4 个提示词键，返回新对象。
  - `copyLabel(base: string, existing: Set<string>): string` —— 剥结尾 `_<数字>` 得词干，返回最小未占用的 `词干_n`(n≥2)。

- [ ] **Step 1: 写失败测试**

在 `frontend/src/canvas/serialize.test.ts` 末尾追加（同时把顶部 import 改为 `import { fromFlow, toFlow, displayName, stripPrompt, copyLabel } from './serialize'`）：

```ts
describe('stripPrompt', () => {
  it('删掉 4 个提示词键、保留其余键', () => {
    const out = stripPrompt({
      model_config_id: 7, output_column: 'q_en', params: { temperature: 0 },
      system_prompt: 'sys', user_prompt: 'Q:{{q}}', system_prompt_ref: 3, user_prompt_ref: 4,
    })
    expect(out).toEqual({ model_config_id: 7, output_column: 'q_en', params: { temperature: 0 } })
  })
  it('深拷贝隔离：改返回对象不影响入参', () => {
    const src = { params: { temperature: 0 }, user_prompt: 'x' }
    const out = stripPrompt(src)
    out.params.temperature = 1
    expect(src.params.temperature).toBe(0)
  })
  it('没有提示词键的 config 原样返回（值相等、对象不同引用）', () => {
    const src = { dataset_ids: [1, 2] }
    const out = stripPrompt(src)
    expect(out).toEqual({ dataset_ids: [1, 2] })
    expect(out).not.toBe(src)
  })
})

describe('copyLabel', () => {
  it('原名未带后缀 → _2', () => {
    expect(copyLabel('翻译', new Set(['翻译']))).toBe('翻译_2')
  })
  it('_2 已占用 → _3', () => {
    expect(copyLabel('翻译', new Set(['翻译', '翻译_2']))).toBe('翻译_3')
  })
  it('复制已带后缀的名：剥词干再自增（翻译_2 → 翻译_3，而非 翻译_2_2）', () => {
    expect(copyLabel('翻译_2', new Set(['翻译', '翻译_2']))).toBe('翻译_3')
  })
})
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npm run test -- src/canvas/serialize.test.ts`
Expected: FAIL —— `stripPrompt is not a function` / `copyLabel is not a function`（或 import 报错）。

- [ ] **Step 3: 实现纯函数**

在 `frontend/src/canvas/serialize.ts` 中 `displayName` 函数之后追加：

```ts
export const PROMPT_KEYS = ['system_prompt', 'user_prompt', 'system_prompt_ref', 'user_prompt_ref'] as const

// 深拷贝 config 并删掉提示词字段（含库引用）。非提示词节点没这些键 → 等价于原样深拷贝。
export function stripPrompt(config: Record<string, any>): Record<string, any> {
  const c = JSON.parse(JSON.stringify(config ?? {}))
  for (const k of PROMPT_KEYS) delete c[k]
  return c
}

// 副本显示名：剥掉结尾的 _<数字> 得词干，返回最小未占用的「词干_n」(n≥2)。
export function copyLabel(base: string, existing: Set<string>): string {
  const stem = base.replace(/_\d+$/, '')
  for (let n = 2; ; n++) {
    const name = `${stem}_${n}`
    if (!existing.has(name)) return name
  }
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `npm run test -- src/canvas/serialize.test.ts`
Expected: PASS（含原有往返/label 用例全绿）。

- [ ] **Step 5: 提交**

```bash
git add frontend/src/canvas/serialize.ts frontend/src/canvas/serialize.test.ts
git commit -m "feat(canvas): 节点复制纯函数 stripPrompt/copyLabel"
```

---

### Task 2: CanvasPage 接 Ctrl+C / Ctrl+V

**Files:**
- Modify: `frontend/src/pages/CanvasPage.tsx`

**Interfaces:**
- Consumes: Task 1 的 `stripPrompt`、`copyLabel`；同文件既有 `nextId(type, existing)`、`displayName`；组件内既有 `nodes`、`selected`、`setNodes`、`setSelectedId`。
- Produces: 无对外接口（UI 行为）。

> 说明：此任务是薄 UI 胶水（DOM keydown + setNodes），逻辑已在 Task 1 纯函数里测过。按 spec 决策不为它单独搭 RTL 测试，验证 = 类型检查 + 手动验收。

- [ ] **Step 1: 扩充 serialize 的 import**

在 `frontend/src/pages/CanvasPage.tsx`，把 `'../canvas/serialize'` 的导入行（现为第 13 行）补上 `stripPrompt, copyLabel`。原为：

```ts
import { NODE_LABELS, RESCAN_EDGE, fromFlow, toFlow, displayName } from '../canvas/serialize'
```

改为：

```ts
import { NODE_LABELS, RESCAN_EDGE, fromFlow, toFlow, displayName, stripPrompt, copyLabel } from '../canvas/serialize'
```

- [ ] **Step 2: 加剪贴板 ref**

在 `Canvas()` 组件内，紧跟 `const flowWrap = useRef<HTMLDivElement>(null)`（约 `CanvasPage.tsx:36`）之后加：

```ts
const clip = useRef<{ type: string; config: Record<string, any>; label?: string; position: { x: number; y: number } } | null>(null)
const pasteSeq = useRef(0)
```

- [ ] **Step 3: 加 keydown 监听 useEffect**

在自动保存的 `useEffect`（以 `}, [nodes, edges, id])` 结尾，约 `CanvasPage.tsx:76`）之后插入：

```tsx
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
```

- [ ] **Step 4: 类型检查 + lint**

Run: `npm run build`
Expected: PASS（`tsc -b` 无类型错误，vite build 成功）。

Run: `npm run lint`
Expected: 无新增告警/错误（尤其 react-hooks/exhaustive-deps 对该 effect 不报：依赖 `[nodes, selected, setNodes]` 已含闭包用到的可变量；`nextId`/`setSelectedId`/`displayName`/`stripPrompt`/`copyLabel` 为模块级/稳定引用）。

- [ ] **Step 5: 手动验收（开发服务器）**

Run: `npm run dev`，浏览器打开任一工作流画布：
1. 点选一个 LLM 合成节点（已填模型/参数/提示词）→ 按 Ctrl+C → 按 Ctrl+V：旁边出现错位副本，自动选中；打开其配置抽屉，**模型/参数/输出列等都在，System/User Prompt 为空**（含库引用也已解除）。
2. 给原节点设显示名「翻译」，Ctrl+C 后连按两次 Ctrl+V → 得「翻译_2」「翻译_3」。
3. 在抽屉里点进 User Prompt 文本域，按 Ctrl+C：是正常文本复制，**不**新建副本。
4. 复制一个输入/输出/HTTP 节点 → 整体照搬（这些类型本就无提示词），id 唯一。
5. 刷新页面：副本已随防抖自动保存（图被 PUT）后仍在。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/pages/CanvasPage.tsx
git commit -m "feat(canvas): Ctrl+C/Ctrl+V 复制节点（去提示词·显示名自增）"
```

---

## 范围 / 非目标（与 spec 一致）

- 仅前端；不加后端端点、不改 graph schema、不引入新依赖。
- 应用内剪贴板，不接系统剪贴板、不跨工作流/标签页粘贴。
- 单节点；不做框选多节点、不复制连线。
- 无显示名的节点副本不自动起名（新 id 已唯一）。
