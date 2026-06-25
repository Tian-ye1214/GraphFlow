import { describe, expect, it } from 'vitest'
import { applyAssistPatch, cycleColRef, REDACTED_MARKER } from './NodeConfigForm'

// L15 回归：http_fetch 三态循环只改 url，导致只在 body/headers 引用的列被点击时
// 把用户从未写过的 {{列}} 注入到 URL。修复后移除引用要作用于真正持有占位的字段。
describe('cycleColRef http_fetch field resolution (L15)', () => {
  it('removes a body-only reference from body and never injects {{col}} into the url', () => {
    const config = {
      method: 'POST',
      url: 'https://api.example.com/search',
      body: '{"q":"{{x}}"}',
      drop_columns: [],
    }

    const next = cycleColRef('http_fetch', config, 'x')

    // url 保持用户原文，绝不被注入 {{x}}
    expect(next.url).toBe('https://api.example.com/search')
    // 引用从真正持有占位的 body 中清除
    expect(next.body).toBe('{"q":""}')
    // 绿→红：列进入 drop_columns
    expect(next.drop_columns).toEqual(['x'])
  })

  it('removes a header-only reference from that header value, not the url', () => {
    const config = {
      method: 'GET',
      url: 'https://api.example.com/search',
      headers: { Authorization: 'Bearer {{token}}' },
      drop_columns: [],
    }

    const next = cycleColRef('http_fetch', config, 'token')

    expect(next.url).toBe('https://api.example.com/search')
    expect(next.headers).toEqual({ Authorization: 'Bearer ' })
    expect(next.drop_columns).toEqual(['token'])
  })

  it('removes a url reference from the url when the placeholder lives there', () => {
    const config = { method: 'GET', url: 'https://api.example.com/{{city}}', drop_columns: [] }

    const next = cycleColRef('http_fetch', config, 'city')

    expect(next.url).toBe('https://api.example.com/')
    expect(next.drop_columns).toEqual(['city'])
  })

  it('inserts into url for a fresh (grey to green) reference, leaving body untouched', () => {
    const config = { method: 'POST', url: 'https://api.example.com', body: '{}', drop_columns: [] }

    const next = cycleColRef('http_fetch', config, 'x')

    expect(next.url).toBe('https://api.example.com{{x}}')
    expect(next.body).toBe('{}')
    expect(next.drop_columns ?? []).toEqual([])
  })

  it('red to grey only clears drop_columns without touching any field', () => {
    const config = {
      method: 'POST',
      url: 'https://api.example.com/search',
      body: '{"q":"{{x}}"}',
      drop_columns: ['x'],
    }

    const next = cycleColRef('http_fetch', config, 'x')

    expect(next.url).toBe('https://api.example.com/search')
    expect(next.body).toBe('{"q":"{{x}}"}')
    expect(next.drop_columns).toEqual([])
  })
})

// I1 回归：http 节点重构成 endpoint/params/body 后，三态循环只搜 url/body/headers，
// 漏掉 endpoint 与 params——引用活在这两处时点击绿→红清不掉，反把 {{列}} 注入默认字段。
// 修复后搜索口径与 refText 一致：endpoint→url→body→headers→params。
describe('cycleColRef http_fetch endpoint/params resolution (I1)', () => {
  it('removes an endpoint reference from endpoint, never injecting into url', () => {
    const config = { method: 'GET', endpoint: 'https://api.example.com/{{city}}', drop_columns: [] }

    const next = cycleColRef('http_fetch', config, 'city')

    expect(next.endpoint).toBe('https://api.example.com/')
    expect(next.url).toBeUndefined()
    expect(next.drop_columns).toEqual(['city'])
  })

  it('removes a params-only reference from that params value', () => {
    const config = {
      method: 'GET',
      endpoint: 'https://api.example.com/search',
      params: { city: '{{c}}', api_key: 'secret' },
      drop_columns: [],
    }

    const next = cycleColRef('http_fetch', config, 'c')

    expect(next.endpoint).toBe('https://api.example.com/search')
    expect(next.params).toEqual({ city: '', api_key: 'secret' })
    expect(next.drop_columns).toEqual(['c'])
  })

  it('inserts a fresh reference into endpoint when endpoint is the configured field', () => {
    const config = { method: 'GET', endpoint: 'https://api.example.com', drop_columns: [] }

    const next = cycleColRef('http_fetch', config, 'x')

    expect(next.endpoint).toBe('https://api.example.com{{x}}')
    expect(next.url).toBeUndefined()
    expect(next.drop_columns ?? []).toEqual([])
  })
})

// 后端把喂给助手的 current_config 里的密钥脱敏成 ***REDACTED***（防密钥进模型输入/日志）。
// 助手可能把该占位回显进返回配置，浅合并 onApply 会用占位覆盖用户真实 api_key/鉴权头——
// applyAssistPatch 在合并后把 params/headers 里的占位还原为本地真实值（无本地值则丢弃该键）。
describe('applyAssistPatch restores redacted secrets on apply', () => {
  it('restores a redacted params.api_key from the live config', () => {
    const config = { method: 'GET', params: { city: '{{c}}', api_key: 'realkey' } }
    const patch = { params: { city: '{{c}}', api_key: REDACTED_MARKER, lang: 'en' } }

    const merged = applyAssistPatch(config, patch)

    expect(merged.params).toEqual({ city: '{{c}}', api_key: 'realkey', lang: 'en' })
  })

  it('restores a redacted Authorization header from the live config', () => {
    const config = { headers: { Authorization: 'Bearer realtoken' } }
    const patch = { headers: { Authorization: REDACTED_MARKER } }

    const merged = applyAssistPatch(config, patch)

    expect(merged.headers).toEqual({ Authorization: 'Bearer realtoken' })
  })

  it('drops a redacted key that has no live value to restore', () => {
    const config = { params: { city: '{{c}}' } }
    const patch = { params: { api_key: REDACTED_MARKER } }

    const merged = applyAssistPatch(config, patch)

    expect(merged.params).toEqual({})
  })

  it('merges normally when no marker is present', () => {
    const config = { method: 'GET', params: { api_key: 'realkey' } }
    const patch = { endpoint: 'https://x/y', params: { api_key: 'realkey', city: '{{c}}' } }

    const merged = applyAssistPatch(config, patch)

    expect(merged).toEqual({ method: 'GET', endpoint: 'https://x/y', params: { api_key: 'realkey', city: '{{c}}' } })
  })

  it('is a plain shallow merge for non-http configs (no params/headers)', () => {
    const config = { user_prompt: 'old', output_column: 'q_en' }
    const patch = { user_prompt: 'new' }

    expect(applyAssistPatch(config, patch)).toEqual({ user_prompt: 'new', output_column: 'q_en' })
  })
})

describe('cycleColRef llm field resolution', () => {
  it('green to red removes the prompt reference and drops the column', () => {
    const config = { user_prompt: 'answer {{q}}', drop_columns: [] }

    const next = cycleColRef('llm_synth', config, 'q')

    expect(next.user_prompt).toBe('answer ')
    expect(next.drop_columns).toEqual(['q'])
  })

  it('grey to green inserts the prompt reference', () => {
    const config = { user_prompt: 'answer ', drop_columns: [] }

    const next = cycleColRef('llm_synth', config, 'q')

    expect(next.user_prompt).toBe('answer {{q}}')
    expect(next.drop_columns ?? []).toEqual([])
  })
})
