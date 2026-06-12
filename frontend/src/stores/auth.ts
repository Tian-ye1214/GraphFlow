import { create } from 'zustand'
import { api } from '../api/client'
import type { UserInfo } from '../api/types'

interface AuthState {
  user: UserInfo | null
  ready: boolean
  init: () => Promise<void>
  login: (username: string) => Promise<void>
  logout: () => Promise<void>
}

export const useAuth = create<AuthState>((set) => ({
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
    set({ user: await api.post<UserInfo>('/api/auth/login', { username }) })
  },
  logout: async () => {
    await api.post('/api/auth/logout')
    set({ user: null })
  },
}))
