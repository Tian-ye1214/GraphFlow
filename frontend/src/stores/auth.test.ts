import { describe, expect, it, vi, beforeEach } from 'vitest'

vi.mock('../api/client', () => ({ api: { post: vi.fn(), get: vi.fn() } }))
import { api } from '../api/client'
import { useAuth } from './auth'

describe('auth.logout 本地优先', () => {
  beforeEach(() => {
    useAuth.setState({ user: { id: 1, username: 'x' } as never, ready: true })
    vi.clearAllMocks()
  })

  it('登出请求失败（断网/401/500）也清本地登录态', async () => {
    ;(api.post as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error('network'))
    await useAuth.getState().logout()
    expect(useAuth.getState().user).toBeNull()
  })

  it('登出请求成功照常清本地登录态', async () => {
    ;(api.post as ReturnType<typeof vi.fn>).mockResolvedValueOnce({})
    await useAuth.getState().logout()
    expect(useAuth.getState().user).toBeNull()
  })
})
