import { describe, expect, it } from 'vitest'
import { buildModelPayload, endpointLabelForProvider } from './ModelsPage'

describe('ModelsPage helpers', () => {
  it('builds azure payload with provider and api version', () => {
    const payload = buildModelPayload({
      name: 'az',
      model_name: 'gemini-3.1-p',
      base_url: 'https://aidp.bytedance.net/api/modelhub/online/v2/crawl',
      provider: 'azure',
      api_version: '2024-03-01-preview',
      api_key: 'k',
      temperature: 0.2,
    })

    expect(payload).toMatchObject({
      name: 'az',
      model_name: 'gemini-3.1-p',
      base_url: 'https://aidp.bytedance.net/api/modelhub/online/v2/crawl',
      provider: 'azure',
      api_version: '2024-03-01-preview',
      api_key: 'k',
    })
    expect(payload.default_params).toMatchObject({ temperature: 0.2 })
  })

  it('clears api version for openai payloads', () => {
    const payload = buildModelPayload({
      name: 'openai',
      model_name: 'qwen-max',
      base_url: 'http://x/v1',
      provider: 'openai',
      api_version: '2024-03-01-preview',
    })

    expect(payload.provider).toBe('openai')
    expect(payload.api_version).toBe('')
  })

  it('uses azure endpoint label only for azure provider', () => {
    expect(endpointLabelForProvider('azure')).toBe('Azure Endpoint')
    expect(endpointLabelForProvider('openai')).toBe('Base URL')
  })
})
