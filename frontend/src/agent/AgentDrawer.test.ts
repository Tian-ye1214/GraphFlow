import { describe, expect, it } from 'vitest'
import { AGENT_ROLES, buildSessionPayload } from './AgentDrawer'

describe('AgentDrawer session payload helpers', () => {
  it('builds simple mode with model_config_id and empty params (thinking 服务端硬编码 xhigh)', () => {
    const payload = buildSessionPayload({ advanced: false, modelSel: 7, roleSel: {} })

    expect(payload).toMatchObject({ model_config_id: 7 })
    for (const role of AGENT_ROLES) {
      expect(payload.model_params[role]).toEqual({})
    }
  })

  it('builds advanced mode with separate role models', () => {
    const payload = buildSessionPayload({
      advanced: true,
      roleSel: { coordinator: 1, manager: 2, worker: 3, compactor: 4 },
    })

    expect(payload.models).toEqual({ coordinator: 1, manager: 2, worker: 3, compactor: 4 })
    for (const role of AGENT_ROLES) {
      expect(payload.model_params[role]).toEqual({})
    }
  })
})
