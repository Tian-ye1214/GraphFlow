import { describe, expect, it } from 'vitest'
import { AGENT_ROLES, buildSessionPayload } from './AgentDrawer'

describe('AgentDrawer session payload helpers', () => {
  it('builds simple mode model params for every agent role', () => {
    const payload = buildSessionPayload({
      advanced: false,
      modelSel: 7,
      roleSel: {},
      sharedParams: { reasoning_effort: 'max' },
      roleParams: {},
    })

    expect(payload).toMatchObject({ model_config_id: 7 })
    for (const role of AGENT_ROLES) {
      expect(payload.model_params[role]).toEqual({
        thinking_enabled: true,
        reasoning_effort: 'max',
      })
    }
  })

  it('builds advanced mode with separate role params', () => {
    const payload = buildSessionPayload({
      advanced: true,
      roleSel: { coordinator: 1, manager: 2, worker: 3, compactor: 4 },
      sharedParams: {},
      roleParams: { worker: { thinking_enabled: false } },
    })

    expect(payload.models).toEqual({ coordinator: 1, manager: 2, worker: 3, compactor: 4 })
    expect(payload.model_params.worker).toEqual({
      thinking_enabled: false,
      reasoning_effort: 'high',
    })
    expect(payload.model_params.coordinator.reasoning_effort).toBe('high')
    expect(payload.model_params.compactor.reasoning_effort).toBe('high')
  })
})
