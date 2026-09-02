// A declared `Secret[T]` must not reach the host trace verbatim
// (roadmap item 421 F6, the typescript half).
//
// `record` interpolates a `Map` key and value, a `pool.query`/`pool.execute`
// sql, a stream item, a job name and a component's resolved config straight into
// `hostLog` — this tier's shared observability channel, exported and forwarded to
// every `onHostEvent` subscriber a host installs. Nothing on this tier knew what
// a declared `Secret[T]` was: the marking exists in the IR
// (`externs[i].secret_return`, `params[i].secret`, a config field's `secret`) and
// only the py emitter read it.
//
// The scrub is placed at `record`, the ONE choke point every event passes
// through, not at each call site: an event is a string the runtime has already
// interpolated into, so a VALUE funnel cannot reach it, and per-printer
// redaction is the discipline the funnel exists to replace. What drives it is the
// emitted program registering the declared marking itself, at both ends, so it
// fires with no recorder and no host subscription attached.
//
// Every assertion below is PAIRED — the canary must be ABSENT and the
// placeholder PRESENT — so none can pass because nothing was recorded at all;
// and the false-positive tests are the other half of the claim: an ordinary
// value is still recorded verbatim, so the trace stays worth reading.
import { beforeEach, describe, expect, it } from 'vitest'
import { Context } from 'cordis'
import { hostLog, resetHost, resolvedConfig } from '../runtime.ts'
import { UserCache, Vault } from './generated/host_trace_secret.ts'

// Spelled out, not imported, so this file asserts the CONTRACT rather than
// whatever the runtime happens to define.
const REDACTED = '<redacted:secret>'
const CANARY = 'SEKRIT-CANARY-421-F6'
const CONFIG_CANARY = 'CONFIG-CANARY-421-F6'

const trace = () => hostLog.join('\n')

beforeEach(() => resetHost())

async function liveCache(): Promise<{ cache: any; dispose: () => Promise<void> }> {
  const ctx = new Context()
  const fiber = ctx.plugin(UserCache)
  await fiber
  return { cache: (ctx as any).cache, dispose: async () => { await fiber.dispose() } }
}

describe('a declared Secret[T] in the host trace', () => {
  it('does not carry a Secret[T] extern return (the origin)', async () => {
    const { cache, dispose } = await liveCache()
    expect(cache.put('alice')).toBe('ok')
    await dispose()

    expect(trace()).not.toContain(CANARY)
    // ...and the insert/remove events ARE there, redacted — so the absence
    // above cannot be an absence of trace.
    expect(hostLog).toContain(`map#1.insert(${REDACTED}, PUBLIC-VALUE-421)`)
    expect(hostLog).toContain(`map#1.remove(${REDACTED})`)
  })

  it('does not carry a Secret[T] method parameter (the receiver)', async () => {
    // No origin runs here: the value arrives as a declared `Secret[T]` argument,
    // so this fails on its own if the receiver-side marking is missing.
    const { cache, dispose } = await liveCache()
    expect(cache.store(CANARY)).toBe('ok')
    await dispose()

    expect(trace()).not.toContain(CANARY)
    expect(hostLog).toContain(`map#1.insert(${REDACTED}, PUBLIC-VALUE-421)`)
  })

  it('does not spell out a Secret[T] config field', async () => {
    const ctx = new Context()
    const fiber = ctx.plugin(Vault)
    await fiber

    const line = hostLog.find((e) => e.startsWith('Vault.config'))
    expect(line).toBeDefined()
    expect(line).not.toContain(CONFIG_CANARY)
    // the field is still NAMED and still in position, and the non-secret field
    // beside it is untouched — the line keeps saying what resolved
    expect(line).toContain(`api_key="${REDACTED}"`)
    expect(line).toContain('region="eu"')

    // the component itself was granted the real value; only the trace was not
    expect(resolvedConfig.get('Vault')).toEqual({
      api_key: CONFIG_CANARY, region: 'eu',
    })
    await fiber.dispose()
  })

  it('leaves an ordinary value verbatim (the false-positive control)', async () => {
    const { cache, dispose } = await liveCache()
    cache.note('an-ordinary-argument')
    await dispose()

    expect(hostLog).toContain('map#1.insert(an-ordinary-argument, PUBLIC-VALUE-421)')
    expect(trace()).not.toContain(REDACTED)
  })

  it('leaves a secret-free composition byte-identical', async () => {
    // `map#1.new` / `map#1.drop` carry no interpolated value at all, so they
    // pin that the funnel is a pass-through when nothing is registered.
    const ctx = new Context()
    const fiber = ctx.plugin(UserCache)
    await fiber
    await fiber.dispose()
    expect(hostLog).toEqual(['map#1.new', 'map#1.drop'])
  })
})
