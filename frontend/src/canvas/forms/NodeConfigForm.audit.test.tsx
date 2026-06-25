import { describe, expect, it } from 'vitest'
import { cycleColRef } from './NodeConfigForm'

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
