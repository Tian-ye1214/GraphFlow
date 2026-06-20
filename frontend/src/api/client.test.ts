import { describe, expect, it, vi, afterEach } from 'vitest'
import { api, ApiError, filenameFromDisposition } from './client'

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

describe('filenameFromDisposition', () => {
  it('解析 RFC5987 filename* 并解码', () => {
    expect(filenameFromDisposition("attachment; filename=\"x.gfpkg\"; filename*=UTF-8''%E9%93%BE%E8%B7%AF.gfpkg", 'fb.gfpkg'))
      .toBe('链路.gfpkg')
  })
  it('缺失时回退', () => {
    expect(filenameFromDisposition(null, 'fb.gfpkg')).toBe('fb.gfpkg')
  })
})
