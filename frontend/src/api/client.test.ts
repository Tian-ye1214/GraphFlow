import { describe, expect, it, vi, afterEach } from 'vitest'
import { api, ApiError } from './client'

afterEach(() => vi.restoreAllMocks())

function mockFetch(status: number, body: unknown) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(body), { status })))
}

describe('api client', () => {
  it('返回 JSON 数据', async () => {
    mockFetch(200, { id: 1 })
    expect(await api.get<{ id: number }>('/api/me')).toEqual({ id: 1 })
  })

  it('非 2xx 抛 ApiError 并带后端 detail', async () => {
    mockFetch(422, { detail: '数据集不存在' })
    await expect(api.post('/api/runs', { workflow_id: 1 })).rejects.toThrowError(
      new ApiError(422, '数据集不存在'),
    )
  })
})
