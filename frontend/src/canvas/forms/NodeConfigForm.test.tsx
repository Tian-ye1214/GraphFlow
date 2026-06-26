import '@testing-library/jest-dom/vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import NodeConfigForm, { THINKING_EFFORT_OPTIONS, buildCodegenPayload, missingLibVars } from './NodeConfigForm'

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal('ResizeObserver', ResizeObserverStub)

afterEach(() => {
  vi.restoreAllMocks()
})

beforeEach(() => {
  localStorage.clear()
})

function mockColumns(columns: Record<string, { input: string[]; output: string[] }>) {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.includes('/api/workflows/1/columns')) {
      return new Response(JSON.stringify(columns), { status: 200 })
    }
    if (path.includes('/api/models') || path.includes('/api/prompts')) {
      return new Response(JSON.stringify([]), { status: 200 })
    }
    return new Response(JSON.stringify({ detail: 'unexpected' }), { status: 404 })
  }))
}

const MODEL_A = {
  id: 1, name: 'Model A', model_name: 'model-a', base_url: '',
  provider: 'openai', azure_api_mode: 'legacy', api_version: '',
  api_key_set: true, default_params: {},
}

const MODEL_B = {
  id: 2, name: 'Model B', model_name: 'model-b', base_url: '',
  provider: 'openai', azure_api_mode: 'legacy', api_version: '',
  api_key_set: true, default_params: {},
}

function persistAssistState(key: string, draft: string, modelConfigId?: number) {
  localStorage.setItem(key, JSON.stringify({ draft, modelConfigId }))
}

function persistCollapseState(key: string, activeKeys: string[]) {
  localStorage.setItem(key, JSON.stringify(activeKeys))
}

function mockNodeConfigApis(columns: Record<string, { input: string[]; output: string[] }> = {}) {
  const posts: { path: string; body: any }[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.includes('/api/workflows/')) {
      return new Response(JSON.stringify(columns), { status: 200 })
    }
    if (path.includes('/api/models')) {
      return new Response(JSON.stringify([MODEL_A, MODEL_B]), { status: 200 })
    }
    if (path.includes('/api/prompts')) {
      return new Response(JSON.stringify([]), { status: 200 })
    }
    if (path.includes('/api/agent/node-assist')) {
      posts.push({ path, body: JSON.parse(String(init?.body ?? '{}')) })
      return new Response(JSON.stringify({ reply: 'ok', config: null, sample_source: 'none' }), { status: 200 })
    }
    if (path.includes('/api/agent/codegen')) {
      posts.push({ path, body: JSON.parse(String(init?.body ?? '{}')) })
      return new Response(JSON.stringify({
        code: 'return rows',
        output_columns: ['out'],
        columns: ['out'],
        sample_source: 'none',
      }), { status: 200 })
    }
    return new Response(JSON.stringify({ detail: 'unexpected' }), { status: 404 })
  }))
  return posts
}

describe('NodeConfigForm QC feedback column', () => {
  it('shows configurable status and feedback columns on qc forms', async () => {
    mockColumns({ qc: { input: ['q', 'answer'], output: ['q', 'answer', 'qc_status', 'qc_feedback'] } })

    render(<NodeConfigForm type="qc" workflowId={1} nodeId="qc" config={{}} onChange={() => {}} />)

    // 折叠布局：状态列名 / 反馈列名在「高级」分组内，先展开
    fireEvent.click(await screen.findByText('高级（回扫 / 反馈 / 参数）'))
    expect(await screen.findByText('状态列名')).toBeInTheDocument()
    expect(screen.getByDisplayValue('qc_status')).toBeInTheDocument()
    expect(screen.getByText('反馈列名')).toBeInTheDocument()
    expect(screen.getByDisplayValue('qc_feedback')).toBeInTheDocument()
  })

  it('shows qc status and feedback as produced output columns', async () => {
    mockColumns({ qc: { input: ['q', 'answer'], output: ['q', 'answer'] } })

    render(<NodeConfigForm type="qc" workflowId={1} nodeId="qc" config={{}} onChange={() => {}} />)

    await screen.findByText('输出列 (4) ▾')
    expect(screen.getByText('qc_status')).toBeInTheDocument()
    expect(screen.getByText('qc_feedback')).toBeInTheDocument()
  })

  it('allows downstream llm prompts to reference qc feedback', async () => {
    mockColumns({ llm: { input: ['q', 'answer', 'qc_feedback'], output: ['q', 'answer', 'qc_feedback'] } })

    render(
      <NodeConfigForm
        type="llm_synth"
        workflowId={1}
        nodeId="llm"
        config={{ user_prompt: '根据{{qc_feedback}}改写答案' }}
        onChange={() => {}}
      />,
    )

    // 折叠布局：提示词在「提示词」分组内，先展开
    fireEvent.click(await screen.findByText('提示词'))
    await waitFor(() => expect(screen.getByDisplayValue('根据{{qc_feedback}}改写答案')).toBeInTheDocument())
    await waitFor(() => expect(screen.queryByText(/引用了上游未产出的列/)).not.toBeInTheDocument())
    const prompt = screen.getByDisplayValue('根据{{qc_feedback}}改写答案')
    expect(within(prompt.closest('div') as HTMLElement).queryByText(/引用了上游未产出的列/)).not.toBeInTheDocument()
  })
})

describe('NodeConfigForm collapse state', () => {
  it('restores expanded node panels from localStorage', async () => {
    persistCollapseState('graphflow.nodeConfigCollapse.v1:201:llm_synth:llm_a', ['prompt'])
    mockNodeConfigApis({ llm_a: { input: ['q'], output: ['q'] } })

    render(
      <NodeConfigForm
        type="llm_synth"
        workflowId={201}
        nodeId="llm_a"
        config={{ user_prompt: 'visible prompt draft' }}
        onChange={() => {}}
      />,
    )

    expect(await screen.findByDisplayValue('visible prompt draft')).toBeInTheDocument()
  })

  it('persists expanded panels when a user opens a section', async () => {
    const key = 'graphflow.nodeConfigCollapse.v1:202:llm_synth:llm_a'
    mockNodeConfigApis({ llm_a: { input: ['q'], output: ['q'] } })

    render(<NodeConfigForm type="llm_synth" workflowId={202} nodeId="llm_a" config={{}} onChange={() => {}} />)

    fireEvent.click(await screen.findByText('提示词'))

    await waitFor(() => {
      expect(JSON.parse(localStorage.getItem(key) ?? '[]')).toContain('prompt')
    })
  })

  it('isolates expanded panels by node id', async () => {
    persistCollapseState('graphflow.nodeConfigCollapse.v1:203:llm_synth:llm_a', ['prompt'])
    mockNodeConfigApis({
      llm_a: { input: ['q'], output: ['q'] },
      llm_b: { input: ['q'], output: ['q'] },
    })

    const view = render(
      <NodeConfigForm
        type="llm_synth"
        workflowId={203}
        nodeId="llm_b"
        config={{ user_prompt: 'node b prompt' }}
        onChange={() => {}}
      />,
    )
    expect(screen.queryByDisplayValue('node b prompt')).not.toBeInTheDocument()

    view.rerender(
      <NodeConfigForm
        type="llm_synth"
        workflowId={203}
        nodeId="llm_a"
        config={{ user_prompt: 'node a prompt' }}
        onChange={() => {}}
      />,
    )
    expect(await screen.findByDisplayValue('node a prompt')).toBeInTheDocument()
  })

  it('restores auto_process expanded operation panels from localStorage', async () => {
    persistCollapseState('graphflow.nodeConfigCollapse.v1:204:auto_process:auto_a', ['ops'])
    mockNodeConfigApis({ auto_a: { input: ['q'], output: ['q'] } })

    render(
      <NodeConfigForm
        type="auto_process"
        workflowId={204}
        nodeId="auto_a"
        config={{ operations: [{ op: 'agent', instruction: 'write persisted operation', code: '', output_columns: [] }] }}
        onChange={() => {}}
      />,
    )

    expect(await screen.findByDisplayValue('write persisted operation')).toBeInTheDocument()
  })
})

describe('NodeConfigForm node assistant drafts', () => {
  it('restores a node assistant draft and model from localStorage', async () => {
    persistAssistState('graphflow.nodeAssistant.v1:101:llm_synth:llm_a', 'keep this requirement', 2)
    mockNodeConfigApis({ llm_a: { input: ['q'], output: ['q'] } })

    render(<NodeConfigForm type="llm_synth" workflowId={101} nodeId="llm_a" config={{}} onChange={() => {}} />)

    expect(await screen.findByDisplayValue('keep this requirement')).toBeInTheDocument()
    expect(await screen.findByText('Model B')).toBeInTheDocument()
  })

  it('isolates node assistant drafts by workflow, type, and node id', async () => {
    persistAssistState('graphflow.nodeAssistant.v1:102:llm_synth:llm_a', 'draft for node a', 1)
    persistAssistState('graphflow.nodeAssistant.v1:102:llm_synth:llm_b', 'draft for node b', 2)
    mockNodeConfigApis({
      llm_a: { input: ['q'], output: ['q'] },
      llm_b: { input: ['q'], output: ['q'] },
    })

    const view = render(
      <NodeConfigForm type="llm_synth" workflowId={102} nodeId="llm_a" config={{}} onChange={() => {}} />,
    )
    expect(await screen.findByDisplayValue('draft for node a')).toBeInTheDocument()
    expect(screen.queryByDisplayValue('draft for node b')).not.toBeInTheDocument()

    view.rerender(
      <NodeConfigForm type="llm_synth" workflowId={102} nodeId="llm_b" config={{}} onChange={() => {}} />,
    )
    expect(await screen.findByDisplayValue('draft for node b')).toBeInTheDocument()
    expect(screen.queryByDisplayValue('draft for node a')).not.toBeInTheDocument()
  })

  it('sends node-assist with the persisted assistant model id', async () => {
    persistAssistState('graphflow.nodeAssistant.v1:103:llm_synth:llm_a', 'generate prompt', 2)
    const posts = mockNodeConfigApis({ llm_a: { input: ['q'], output: ['q'] } })

    render(<NodeConfigForm type="llm_synth" workflowId={103} nodeId="llm_a" config={{}} onChange={() => {}} />)

    fireEvent.click(await screen.findByRole('button', { name: /发\s*送/ }))

    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0]).toMatchObject({
      path: '/api/agent/node-assist',
      body: { model_config_id: 2, instruction: 'generate prompt' },
    })
  })

  it('isolates auto_process AI operation model selections by operation index', async () => {
    persistAssistState('graphflow.nodeAssistant.v1:104:auto_process:auto_a:agent:0', '', 1)
    persistAssistState('graphflow.nodeAssistant.v1:104:auto_process:auto_a:agent:1', '', 2)
    mockNodeConfigApis({ auto_a: { input: ['q'], output: ['q'] } })

    render(
      <NodeConfigForm
        type="auto_process"
        workflowId={104}
        nodeId="auto_a"
        config={{
          operations: [
            { op: 'agent', instruction: 'first', code: '', output_columns: [] },
            { op: 'agent', instruction: 'second', code: '', output_columns: [] },
          ],
        }}
        onChange={() => {}}
      />,
    )

    fireEvent.click(await screen.findByText('处理操作'))

    await waitFor(() => {
      expect(screen.getByText('Model A')).toBeInTheDocument()
      expect(screen.getByText('Model B')).toBeInTheDocument()
    })
  })

  it('sends codegen with the persisted auto_process AI operation model id', async () => {
    persistAssistState('graphflow.nodeAssistant.v1:105:auto_process:auto_a:agent:0', '', 2)
    const posts = mockNodeConfigApis({ auto_a: { input: ['q'], output: ['q'] } })

    render(
      <NodeConfigForm
        type="auto_process"
        workflowId={105}
        nodeId="auto_a"
        config={{ operations: [{ op: 'agent', instruction: 'write code', code: '', output_columns: [] }] }}
        onChange={() => {}}
      />,
    )

    fireEvent.click(await screen.findByText('处理操作'))
    fireEvent.click(await screen.findByRole('button', { name: '生成代码' }))

    await waitFor(() => expect(posts).toHaveLength(1))
    expect(posts[0]).toMatchObject({
      path: '/api/agent/codegen',
      body: { model_config_id: 2, instruction: 'write code' },
    })
  })
})

describe('missingLibVars', () => {
  it('returns prompt vars not present in input columns', () => {
    expect(missingLibVars(['q', 'a'], ['q'])).toEqual(['a'])
    expect(missingLibVars(['q'], ['q', 'a'])).toEqual([])
    expect(missingLibVars([], ['q'])).toEqual([])
  })
})

describe('NodeConfigForm node assistant UI', () => {
  it('节点助手有「新会话」按钮和「发送」按钮', async () => {
    mockNodeConfigApis({ llm_a: { input: ['q'], output: ['q'] } })

    render(
      <NodeConfigForm type="llm_synth" workflowId={110} nodeId="llm_a" config={{}} onChange={() => {}} />,
    )

    expect(await screen.findByRole('button', { name: '新会话' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /发\s*送/ })).toBeInTheDocument()
  })
})

describe('NodeConfigForm http_fetch form', () => {
  it('HTTP 表单有接口/Params/助手，Headers 在高级', async () => {
    mockNodeConfigApis({ http_node: { input: ['id'], output: ['id'] } })

    render(
      <NodeConfigForm
        type="http_fetch"
        workflowId={301}
        nodeId="http_node"
        config={{}}
        onChange={() => {}}
      />,
    )

    // 节点助手在折叠面板外，直接可见
    expect(await screen.findByText(/RedLotus 助手/)).toBeInTheDocument()
    // 展开请求面板查看 接口/Params 字段
    fireEvent.click(await screen.findByText('请求'))
    expect(await screen.findByText(/接口/)).toBeInTheDocument()
    expect(await screen.findByText(/Params/)).toBeInTheDocument()
  })
})

describe('NodeConfigForm thinking params', () => {
  it('offers max reasoning effort', () => {
    expect(THINKING_EFFORT_OPTIONS).toContain('max')
  })

  it('builds smart processing codegen payload with op params', () => {
    const payload = buildCodegenPayload(1, 'ap', 10, {
      instruction: 'write code',
      code: 'old',
      params: { thinking_enabled: false, reasoning_effort: 'max' },
    })

    expect(payload).toMatchObject({
      workflow_id: 1,
      node_id: 'ap',
      model_config_id: 10,
      current_code: 'old',
      params: { thinking_enabled: false, reasoning_effort: 'max' },
    })
  })
})

describe('HttpFetchForm 轮询与展开', () => {
  it('展示轮询字段，编辑写入 poll_status_path', async () => {
    mockNodeConfigApis({ http_node: { input: ['id'], output: ['id'] } })
    const onChange = vi.fn()
    render(
      <NodeConfigForm type="http_fetch" workflowId={301} nodeId="http_node"
        config={{ url: 'http://x' }} onChange={onChange} />,
    )
    fireEvent.click(await screen.findByText('轮询（异步任务等待；留空状态路径=不轮询）'))
    const input = screen.getByPlaceholderText('状态字段 JSON 路径 如 status')
    fireEvent.change(input, { target: { value: 'status' } })
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ poll_status_path: 'status' }))
  })

  it('展示 records_path，编辑写入 records_path', async () => {
    mockNodeConfigApis({ http_node: { input: ['id'], output: ['id'] } })
    const onChange = vi.fn()
    render(
      <NodeConfigForm type="http_fetch" workflowId={301} nodeId="http_node"
        config={{ url: 'http://x' }} onChange={onChange} />,
    )
    fireEvent.click(await screen.findByText('提取'))
    const input = screen.getByPlaceholderText('数组 JSON 路径 如 data.items（留空=不展开）')
    fireEvent.change(input, { target: { value: 'items' } })
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ records_path: 'items' }))
  })
})
