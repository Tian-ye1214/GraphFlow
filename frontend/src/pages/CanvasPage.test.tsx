import '@testing-library/jest-dom/vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import CanvasPage from './CanvasPage'

vi.mock('../api/events', () => ({ useEvents: () => {} }))

class ResizeObserverStub { observe() {} unobserve() {} disconnect() {} }
vi.stubGlobal('ResizeObserver', ResizeObserverStub)
vi.stubGlobal('matchMedia', (q: string) => ({
  matches: false, media: q, onchange: null,
  addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false },
}))

const WF = {
  id: 1, name: 'wf',
  graph: {
    nodes: [{
      id: 'llm_1', type: 'llm_synth', position: { x: 100, y: 100 }, label: '翻译',
      config: { system_prompt: '机密提示词', system_prompt_ref: 7, model_config_id: 5, temperature: 0.7, output_columns: ['译文'] },
    }],
    edges: [],
  },
}

// 捕获自动保存 PUT 的请求体（去抖 800ms 后触发），用于断言「复制了什么」
function mockFetch() {
  const puts: any[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    const j = (x: unknown) => new Response(JSON.stringify(x), { status: 200 })
    if (init?.method === 'PUT' && /\/api\/workflows\/1$/.test(path)) puts.push(JSON.parse(String(init.body)))
    if (/\/api\/workflows\/1$/.test(path)) return j(WF)
    if (/\/api\/workflows\/1\/columns$/.test(path)) return j({})
    return j([]) // /api/models、/api/prompts、/api/datasets 等列表端点
  }))
  return puts
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/workflows/1']}>
      <Routes><Route path="/workflows/:id" element={<CanvasPage />} /></Routes>
    </MemoryRouter>,
  )
}

afterEach(() => { vi.restoreAllMocks() })

describe('CanvasPage Ctrl+C/Ctrl+V 节点复制', () => {
  it('选中节点后 Ctrl+C/Ctrl+V：复制除提示词外的全部配置，显示名 _2', async () => {
    const puts = mockFetch()
    const { container } = renderPage()

    // 工作流加载后节点渲染出来
    expect(await screen.findByText('翻译')).toBeInTheDocument()

    // 点击节点 → 选中（右侧抽屉打开，出现显示名说明文案）
    const node = container.querySelector('.react-flow__node') as HTMLElement
    expect(node).toBeTruthy()
    fireEvent.click(node)
    expect(await screen.findByText(/显示名（仅画布展示/)).toBeInTheDocument()

    // Ctrl+C 复制、Ctrl+V 粘贴（在 body 上派发，target 为元素，贴近真实键盘）
    fireEvent.keyDown(document.body, { key: 'c', ctrlKey: true })
    fireEvent.keyDown(document.body, { key: 'v', ctrlKey: true })

    // 画布上出现副本（显示名自增 _2）
    expect(await screen.findByText('翻译_2')).toBeInTheDocument()

    // 去抖自动保存把图 PUT 回后端 → 断言副本的真实落库配置
    await waitFor(() => expect(puts.length).toBeGreaterThan(0), { timeout: 2500 })
    const graph = puts[puts.length - 1].graph
    expect(graph.nodes).toHaveLength(2)
    const copy = graph.nodes.find((n: any) => n.label === '翻译_2')
    expect(copy).toBeTruthy()
    // 提示词被清掉（正文 + 库引用）
    expect(copy.config.system_prompt).toBeUndefined()
    expect(copy.config.system_prompt_ref).toBeUndefined()
    // 其余配置原样保留
    expect(copy.config.model_config_id).toBe(5)
    expect(copy.config.temperature).toBe(0.7)
    expect(copy.config.output_columns).toEqual(['译文'])
    // 是个新节点（id 不与原节点重复）
    expect(copy.id).not.toBe('llm_1')
  })

  it('在输入框内按 Ctrl+C 不复制节点（放行正常文本复制）', async () => {
    mockFetch()
    const { container } = renderPage()
    expect(await screen.findByText('翻译')).toBeInTheDocument()
    fireEvent.click(container.querySelector('.react-flow__node') as HTMLElement)

    // 焦点在显示名输入框（placeholder = 节点 id），此处 Ctrl+C 应被放行不抢
    const nameInput = await screen.findByPlaceholderText('llm_1')
    fireEvent.keyDown(nameInput, { key: 'c', ctrlKey: true })
    fireEvent.keyDown(document.body, { key: 'v', ctrlKey: true })

    expect(screen.queryByText('翻译_2')).toBeNull()
    expect(container.querySelectorAll('.react-flow__node')).toHaveLength(1)
  })
})
