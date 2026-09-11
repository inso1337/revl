import { describe, expect, it } from 'vitest'
import { forgetSecrets, markSecret, redactText, secretResult } from '../runtime.ts'

// Long enough that an exact match means something, and not a substring of
// anything else the run prints.
const CANARY = 'TS-MAP-CANARY-421-F6'
const SECOND = 'TS-MAP-SECOND-CANARY-421-F6'

describe('the declared-secret registry walk reaches a Map value', () => {
  it('marks the value of a map handed to the receiver', () => {
    forgetSecrets()
    markSecret(new Map([['token', CANARY]]))
    expect(redactText(`host said ${CANARY}`)).not.toContain(CANARY)
  })

  it('marks every value, not just the first', () => {
    forgetSecrets()
    markSecret(new Map([['a', CANARY], ['b', SECOND]]))
    expect(redactText(`host said ${CANARY}`)).not.toContain(CANARY)
    expect(redactText(`host said ${SECOND}`)).not.toContain(SECOND)
  })

  it('marks a map nested inside a list, a record and another map', () => {
    forgetSecrets()
    secretResult({ rows: [new Map([['k', new Map([['inner', CANARY]])]])] })
    expect(redactText(`host said ${CANARY}`)).not.toContain(CANARY)
  })

  it('marks the map KEYS too, because a key is the caller own data', () => {
    forgetSecrets()
    markSecret(new Map([['account-token', CANARY]]))
    expect(redactText('host said account-token')).not.toContain('account-token')
  })

  it('marks a key nested inside a list, a record and another map', () => {
    forgetSecrets()
    secretResult({ rows: [new Map([[CANARY, new Map([['inner', SECOND]])]])] })
    expect(redactText(`host said ${CANARY}`)).not.toContain(CANARY)
    expect(redactText(`host said ${SECOND}`)).not.toContain(SECOND)
  })

  it('still keeps the field names of a RECORD, which the author wrote', () => {
    forgetSecrets()
    markSecret({ 'account-token': CANARY })
    expect(redactText('host said account-token')).toContain('account-token')
    expect(redactText(`host said ${CANARY}`)).not.toContain(CANARY)
  })

  it('leaves an unmarked value alone', () => {
    forgetSecrets()
    markSecret(new Map([['token', CANARY]]))
    expect(redactText('host said TS-MAP-UNMARKED-421-F6')).toContain(
      'TS-MAP-UNMARKED-421-F6',
    )
  })

  it('survives a map that holds itself', () => {
    forgetSecrets()
    const self = new Map<string, unknown>([['leaf', CANARY]])
    self.set('self', self)
    markSecret(self)
    expect(redactText(`host said ${CANARY}`)).not.toContain(CANARY)
  })
})
