export function roleLabel(role: string): string {
  return role === 'user' ? '你' : role === 'assistant' ? '助手' : '工具'
}
export const ROLE_BG: Record<string, string> = { user: '#e6f4ff', assistant: '#f6ffed' }
