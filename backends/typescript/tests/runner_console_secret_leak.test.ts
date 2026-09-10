// The placement runner's OWN console channels are sinks (issue #815, found
// while writing the item-421 seam-leak coverage).
//
// `placement_runner.ts` prints `load` / `serve` / `proxy` / `fiber` / `probe`
// itself. Only the `host` trace is installed through `runtime._record`, so
// those channels reached the operator console without passing either stage of
// the 421 scrub contract. A provider whose extern raised LOCALLY — no seam, no
// failure reply, so no `seamFailure` funnel anywhere — printed a registered
// credential verbatim, in the same run in which the host trace line for the
// same value redacted it:
//
//   [provider] host  | Keeper.config   | {api_key="<redacted:secret>", ...}
//   [provider] probe | vault.open('a') | ERROR KeyError: 'SEKRIT-... @ pg://main'
//
// The java tier never had this bug (`PlacementRunner.log` has funnelled through
// `redactSecrets` since item 421) and the fix here mirrors it: route both halves
// of the line through `redactText`, the funnel that the host trace already
// reads. One marking, two sinks — no second registry.
//
// The `log` function is sliced VERBATIM out of the shipped runner and executed,
// rather than re-typed here: a copy would prove that some funnel works, not
// that the shipped one does. The runner itself cannot be imported (it reads
// `process.argv[2]` and boots a whole cordis composition at module scope), so
// the slice is the honest unit and its extraction is asserted below.
//
// Every assertion is paired — canary absent AND marker present — so none can
// pass on a silent console, and the false-positive half is the other side of
// the claim: the surrounding diagnostic stays readable, because a redaction
// that eats the sentence is its own failure.

import { afterEach, describe, expect, it, vi } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { redactText, forgetSecrets, markSecret } from '../runtime.ts'

// Spelled out rather than imported, so this suite can RUN against a tree with
// no runner-side scrub in it and fail on the leak instead of on a symbol.
const SECRET = 'SEKRIT-RUNNER-CONSOLE-815-4f2a9c1e'
const REDACTED_SECRET = '<redacted:secret>'

const RUNNER = path.join(
  path.dirname(fileURLToPath(import.meta.url)), '..', 'placement_runner.ts')

afterEach(() => {
  // The registry is process-wide by design, so each test starts and ends empty.
  forgetSecrets()
  vi.restoreAllMocks()
})

/** The shipped `log`, lifted verbatim out of `placement_runner.ts`.
 *
 * Verbatim matters (PR #225 does the same for the ref-pin check). Types are
 * erased for the `Function` wrapper — the transform every TS toolchain
 * performs — and nothing else is touched. The funnel call is asserted present,
 * so a refactor that renames or drops it fails this test loudly instead of
 * extracting a function that no longer scrubs. */
function shippedLog(): (channel: string, subject: string, detail?: string) => void {
  const lines = fs.readFileSync(RUNNER, 'utf8').split('\n')
  const start = lines.findIndex(line => line.startsWith('function log('))
  expect(start, 'the runner no longer defines `log`').toBeGreaterThan(-1)
  const end = lines.findIndex((line, i) => i > start && line === '}')
  const slice = lines.slice(start, end + 1).join('\n')

  expect(slice).toContain('redactText')       // the funnel itself
  expect(slice).toContain('console.log')      // ...and the sink it feeds

  const erased = slice.replace(
    /^function log\([^)]*\): void \{/m, "function log(channel, subject, detail = '') {")
  const make = new Function('redactText', 'name', `${erased}\nreturn log;`)
  return make(redactText, 'provider')
}

describe('the placement runner\'s own console channels', () => {
  it('redacts a registered secret in the probe failure line', () => {
    const log = shippedLog()
    markSecret(SECRET)
    const printed = vi.spyOn(console, 'log').mockImplementation(() => {})

    // the exact shape: the extern raised locally, so the runner built this
    // text itself and never went near a seam failure
    log('probe', "vault.open('alice')", `ERROR KeyError: '${SECRET} @ pg://main'`)

    const line = printed.mock.calls.map(c => c.join(' ')).join('\n')
    expect(line).not.toContain(SECRET)
    expect(line).toContain(REDACTED_SECRET)
    // the control: the other half of the SAME message survives, and so does
    // the expression the probe was written as
    expect(line).toContain('pg://main')
    expect(line).toContain("vault.open('alice')")
    expect(line).toContain('[provider] probe')
  })

  it('redacts a registered secret in the subject half too', () => {
    const log = shippedLog()
    markSecret(SECRET)
    const printed = vi.spyOn(console, 'log').mockImplementation(() => {})

    // the subject carries the component / key a channel names; a secret used
    // as a Map key (the shipped user_cache idiom, item 421 F6) lands here
    log('load', `${SECRET}.config`, 'state=ACTIVE')

    const line = printed.mock.calls.map(c => c.join(' ')).join('\n')
    expect(line).not.toContain(SECRET)
    expect(line).toContain(REDACTED_SECRET)
    expect(line).toContain('state=ACTIVE')
    expect(line).toContain('.config')   // the diagnostic around the value
  })

  it('prints an ordinary line verbatim', () => {
    const log = shippedLog()
    const printed = vi.spyOn(console, 'log').mockImplementation(() => {})

    log('fiber', 'Keeper', 'PENDING -> LOADING')

    const line = printed.mock.calls.map(c => c.join(' ')).join('\n')
    expect(line).toContain('Keeper')
    expect(line).toContain('PENDING -> LOADING')
    expect(line).not.toContain(REDACTED_SECRET)
  })
})
