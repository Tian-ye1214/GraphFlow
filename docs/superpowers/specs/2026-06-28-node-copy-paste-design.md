# 节点复制（Ctrl+C / Ctrl+V）设计

日期：2026-06-28

## 背景与问题

用户在画布上把一个节点调好后（模型、采样/思考参数、输出列、列输入输出三态、扇出/并发/重试、
质检判定模型与回扫等），常想再要一个**设置完全一样、只是提示词不同**的兄弟节点。
现在只能从「+ LLM 合成」新建一个空节点，把上述配置一项项重抄，繁琐且易抄漏。

缺一个「复制节点」：照搬除提示词外的全部配置，名字加后缀区分。

## 用户确定的决策

1. **触发方式**：键盘 `Ctrl/⌘+C` 复制选中节点、`Ctrl/⌘+V` 粘贴出副本。不做按钮、不做右键菜单。
2. **清空范围**：只清 `system_prompt` / `user_prompt` 正文**及其库引用** `system_prompt_ref` / `user_prompt_ref`。
   其余配置一律保留——包括「自动处理」节点 agent 操作的 `instruction` 与生成的 `code`（不视为提示词）。
3. **命名后缀**：作用于**显示名**，数字自增 `_2` / `_3`…（撞名则继续递增）。
   无显示名的节点 id 本就自动唯一，副本不另设显示名。
4. **单节点**：贴合现有单选模型（`selectedId`），不做框选多节点复制。
5. **不复制连线**：副本是游离节点，用户自行连。

## 架构现状（约束与可复用点）

- 画布是**纯前端 React Flow 状态**（`CanvasPage.tsx`）。节点形如
  `{ id, type, position, data: { config, label } }`，图改动后防抖 `useEffect`（`CanvasPage.tsx:68`）
  自动 `PUT /api/workflows/{id}`。**复制纯属前端 `setNodes`，无后端、无新端点。**
- `nextId(type, existing)`（`CanvasPage.tsx:18`）已保证生成全局唯一的 `<type>_<n>` id，直接复用。
- `displayName(label, id)`（`serialize.ts:16`）= 有 label 用 label，否则用 id——副本撞名检测的口径。
- `config` 全程作为 JSON 持久化（PUT 的 graph），故**一定可 JSON 序列化**，深拷贝用
  `JSON.parse(JSON.stringify(config))` 即足够、零环境依赖（不依赖 `structuredClone`）。
- 提示词字段集中在 `llm_synth` / `qc` 两类的 config 上（`system_prompt`/`user_prompt` 正文 +
  `*_ref` 库引用，见 `NodeConfigForm.tsx` 的 `LibraryPromptControl`）。其余节点类型 config 里**没有这些键**
  → 一套删键逻辑对所有类型安全（删不存在的键是空操作）。
- `Drawer` 用 `mask={false}`（`CanvasPage.tsx:159`），**画布在抽屉后仍可交互**；抽屉里有多个
  `Input.TextArea`（提示词等）。故 keydown 监听**必须放行输入控件内的 Ctrl+C**，否则会抢掉正常文本复制。
- 现有 `deleteKeyCode={['Backspace','Delete']}` 由 React Flow 处理删除；本设计的 Ctrl+C/V 走
  独立的 `window` keydown 监听，互不冲突。

## 设计

### 1. 纯函数（`serialize.ts`，与 `displayName` 同处，便于单测）

```ts
export const PROMPT_KEYS = ['system_prompt', 'user_prompt', 'system_prompt_ref', 'user_prompt_ref'] as const

// 深拷贝 config 并删掉提示词字段（含库引用）。非提示词节点没这些键 → 原样深拷贝。
export function stripPrompt(config: Record<string, any>): Record<string, any> {
  const c = JSON.parse(JSON.stringify(config ?? {}))
  for (const k of PROMPT_KEYS) delete c[k]
  return c
}

// 副本显示名：剥掉结尾的 _<数字> 得词干，返回最小未占用的「词干_n」(n≥2)。
// 连续复制「翻译」→ 翻译_2、翻译_3；复制「翻译_2」→ 翻译_3（而非 翻译_2_2）。
export function copyLabel(base: string, existing: Set<string>): string {
  const stem = base.replace(/_\d+$/, '')
  for (let n = 2; ; n++) {
    const name = `${stem}_${n}`
    if (!existing.has(name)) return name
  }
}
```

### 2. CanvasPage 接线

应用内剪贴板（一个 ref，**非系统剪贴板**）+ 一个 `window` keydown 监听：

```ts
const clip = useRef<{ type: string; config: Record<string, any>; label?: string; position: { x: number; y: number } } | null>(null)
const pasteSeq = useRef(0)   // 同一次复制内多次粘贴的错位序号

useEffect(() => {
  const onKey = (e: KeyboardEvent) => {
    if (!(e.ctrlKey || e.metaKey)) return
    // 放行输入控件内的 Ctrl+C/V（编辑提示词时正常文本复制）
    const t = e.target as HTMLElement | null
    if (t?.closest('input, textarea, [contenteditable="true"]')) return

    const key = e.key.toLowerCase()
    if (key === 'c' && selected) {
      clip.current = {
        type: selected.type!,
        config: (selected.data as any).config ?? {},
        label: (selected.data as any).label,
        position: selected.position,
      }
      pasteSeq.current = 0
    } else if (key === 'v' && clip.current) {
      e.preventDefault()
      const c = clip.current
      pasteSeq.current += 1
      const off = 40 * pasteSeq.current
      const existing = new Set(nodes.map((n) => displayName((n.data as any)?.label, n.id)))
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

- **Ctrl+C**：有选中节点就把 `{type, config, label, position}` 存进 `clip`，并把 `pasteSeq` 归零。
- **Ctrl+V**：从 `clip` 造新节点——`nextId` 取唯一 id、`stripPrompt` 去提示词、有显示名才 `copyLabel`
  自增、`position` 按 `pasteSeq` 递增错位（+40/次，防与源及上一份完全重叠）、选中新节点。
  追加进 `nodes` 后现有防抖 `useEffect` 自动落库。
- id / 撞名集合 / 递增序号都在 `setNodes` 更新器**之外**算好，更新器保持纯函数（避免 React 18
  StrictMode 双调用更新器导致偏移翻倍 / 重复选中）。代价是 `nodes` 进监听依赖、节点变动时重绑
  keydown（bind/unbind 极廉价，可接受）。

## 错误处理 / 边界

- 焦点在 `input` / `textarea` / `contenteditable` 内的 Ctrl+C/V 一律放行系统默认行为（不劫持）。
- 无选中节点时 Ctrl+C 空操作；剪贴板为空时 Ctrl+V 空操作。
- `Ctrl` 与 `⌘`（mac）都认（`ctrlKey || metaKey`）。
- 撞名集合用全体节点的 `displayName`（含未设 label 时的 id），保证副本显示名不与任一现有节点相同。
- 深拷贝隔离：副本 config 是独立对象，改副本不动原节点（params/operations/headers 等嵌套对象同样隔离）。
- 删提示词键对没有这些键的节点类型（input/output/http_fetch/auto_process）是空操作，行为不变。

## 测试（TDD，先红后绿）

`serialize.test.ts` 追加：

- `stripPrompt`：删掉 4 个提示词键、保留其余键（如 `model_config_id`/`params`/`output_column`）；
  改返回对象不影响入参（深拷贝隔离）；对没有提示词键的 config 原样返回。
- `copyLabel`：`copyLabel('翻译', {'翻译'})` → `翻译_2`；存在 `翻译_2` 时 → `翻译_3`；
  `copyLabel('翻译_2', {'翻译','翻译_2'})` → `翻译_3`（词干剥离）。
- 既有 `serialize.test.ts` / `NodeConfigForm.test.tsx` 全绿。

keydown 接线属薄 UI 胶水（DOM 事件 + setNodes），逻辑都在已测纯函数里，不为它单独搭 RTL 测试（KISS）。

## 范围 / 非目标

- 仅前端；不加后端端点、不改 graph schema、不引入新依赖。
- 应用内剪贴板，不接系统剪贴板、不跨工作流/跨标签页粘贴。
- 单节点复制，不做框选多节点、不复制连线。
- 无显示名的节点副本不自动起名（其新 id 已唯一）。
