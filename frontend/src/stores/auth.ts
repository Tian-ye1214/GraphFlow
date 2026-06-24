import { create } from 'zustand'
import { api } from '../api/client'
import type { UserInfo } from '../api/types'

interface AuthState {
  user: UserInfo | null
  ready: boolean
  init: () => Promise<void>
  login: (username: string) => Promise<void>
  logout: () => Promise<void>
  actAs: (userId: number | null) => Promise<void>
}

export const useAuth = create<AuthState>((set, get) => ({
  user: null,
  ready: false,
  init: async () => {
    try {
      set({ user: await api.get<UserInfo>('/api/me'), ready: true })
    } catch {
      set({ user: null, ready: true })
    }
  },
  login: async (username) => {
    await api.post<UserInfo>('/api/auth/login', { username })
    await get().init()
  },
  logout: async () => {
    // 本地优先：服务端登出请求失败（断网/401/500）也吞掉，照常清本地登录态，对照 CLI 版 logout。
    try {
      await api.post('/api/auth/logout')
    } catch {
      // 忽略：登出是本地优先操作
    }
    set({ user: null })
  },
  actAs: async (userId) => {
    await api.post('/api/admin/act-as', { user_id: userId })
    await get().init()
  },
}))
