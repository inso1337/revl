import { describe, expect, it } from 'vitest'
import { forgetSecrets, markSecret, redactText, secretResult } from '../runtime.ts'

// Long enough that an exact match means something, and not a substring of
// anything else the run prints.
const CANARY = 'TS-CYCLE-CANARY-421-F6'

describe('the declared-secret registry walk survives a cyclic value', () => {
  it('marks the leaves of a list that holds itself, and terminates', () => {
    forgetSecrets()
    const self: unknown[] = [CANARY]
    self.push(self)
    markSecret(self)
    expect(redactText(`host said ${CANARY}`)).not.toContain(CANARY)
  })

  it('marks the leaves of a record that points back at itself', () => {
    forgetSecrets()
    const rec: Record<string, unknown> = { leaf: CANARY }
    rec.parent = rec
    secretResult(rec)
    expect(redactText(`host said ${CANARY}`)).not.toContain(CANARY)
  })

  it('marks a leaf below any plausible depth cap', () => {
    forgetSecrets()
    let deep: unknown = CANARY
    for (let i = 0; i < 40; i += 1) deep = [deep]
    markSecret(deep)
    expect(redactText(`host said ${CANARY}`)).not.toContain(CANARY)
  })
})
