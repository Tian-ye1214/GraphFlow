export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = init?.body instanceof FormData ? undefined : { 'Content-Type': 'application/json' }
  const res = await fetch(path, { headers, ...init })
  if (!res.ok) {
    let detail = res.statusText
    try {
      detail = String((await res.json()).detail ?? detail)
    } catch {
      /* 非 JSON 错误体，保留 statusText */
    }
    throw new ApiError(res.status, detail)
  }
  return res.json() as Promise<T>
}

export const api = {
  get: <T>(p: string) => request<T>(p),
  post: <T>(p: string, body?: unknown) =>
    request<T>(p, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  postForm: <T>(p: string, form: FormData) => request<T>(p, { method: 'POST', body: form }),
  put: <T>(p: string, body: unknown) => request<T>(p, { method: 'PUT', body: JSON.stringify(body) }),
  del: <T>(p: string) => request<T>(p, { method: 'DELETE' }),
}

// 从 Content-Disposition 取文件名（优先 RFC5987 的 filename*），缺失则回退。
export function filenameFromDisposition(dispo: string | null, fallback: string): string {
  const m = /filename\*=UTF-8''([^;]+)/.exec(dispo || '')
  return m ? decodeURIComponent(m[1]) : fallback
}

// 链路导出：取 zip blob 触发浏览器下载（绕开 api.request 的 res.json()）。
export async function downloadWorkflowPackage(id: number, fallback: string): Promise<void> {
  const res = await fetch(`/api/workflows/${id}/export`)
  if (!res.ok) throw new ApiError(res.status, '导出失败')
  const name = filenameFromDisposition(res.headers.get('content-disposition'), `${fallback}.gfpkg`)
  const url = URL.createObjectURL(await res.blob())
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  URL.revokeObjectURL(url)
}
