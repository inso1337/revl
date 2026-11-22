// The ts tier of `stdlib/channel.rvl`'s same-origin provider of `Wire` (issue
// #3418, first slice): `revl_channel_ts.ts`, the module each of the provider's
// `@ts ref` externs imports. Two ends in one process, each its own
// `BroadcastChannel` of one name, as two pages of one origin are: the host
// (`listen`, the `Hub`) and a joining page (`join`, the `Peer`).
//
// Every host entry point is called as the emitted thunk calls it: revl's `Int`
// is a `bigint` on this tier, so the door's numbers and the line numbers go in
// as `bigint`s, and the knocks' `seq` and `line` come back as `bigint`s.
//
// What it pins:
//   1. a call said by a joining end reaches the host, and the result the host
//      says comes back: Opened, Said, Said in order, `who` the offered label;
//   2. `leave` is `Shut(closed)`; a host `close` is told to the peer with its
//      reason; `shut` (the row's teardown) tells every line `host_gone`;
//   3. a message over `most` shuts the line `too_large` and none is delivered;
//      the joining end refuses to send one;
//   4. at `held` waiting knocks a line that says more is shut `too_fast`;
//   5. a join past `lines` is `refused` and makes no knock;
//   6. a line silent for `silence` ms is shut `silent`; its next word is
//      answered `expired`, and a new join gets a new line;
//   7. a join no host answers shuts `host_gone` on the joining end; one host
//      per channel name, settled at once; a channel of another name is not
//      heard;
//   8. what the host's door says reaches the joining end (`most`, `beat`), a
//      join while `held` knocks wait is refused, a label is cut to `most`, and
//      a forged or malformed message makes no knock and breaks nothing.
// Then the same through `stdlib/channel.rvl` itself, emitted for this tier and
// plugged into a cordis Context, and the module loaded the way the emitted
// thunk loads it, under plain node.
import { afterEach, describe, expect, it } from 'vitest'
import { Context } from 'cordis'
import * as fs from 'node:fs'
import * as path from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { pythonInterpreter } from '../scripts/emit-fixtures.ts'
import { type Event, type Hub, type Peer, type Raw, close, join, listen, say, shut, take }
  from '../revl_channel_ts.ts'

const BACKEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const ROOT = path.resolve(BACKEND, '..', '..')

const hubs: Hub[] = []
const peers: Peer[] = []
const raws: BroadcastChannel[] = []
let counter = 0

afterEach(() => {
  for (const p of peers.splice(0)) p.leave()
  for (const h of hubs.splice(0)) shut(h)
  for (const c of raws.splice(0)) c.close()
})

/** A channel name no other test in this process has used. */
function room(): string {
  return `room-${process.pid}-${Date.now()}-${counter++}`
}

function host(channel: string, over: Partial<Record<'most' | 'held' | 'lines' | 'beat'
  | 'silence', number>> = {}): Hub {
  const h = listen({
    channel, most: 65536n, held: 1024n, lines: 256n, beat: 20n, silence: 5000n,
    ...Object.fromEntries(Object.entries(over).map(([k, v]) => [k, BigInt(v)])),
  } as Parameters<typeof listen>[0])
  hubs.push(h)
  return h
}

function visitor(channel: string, over: Partial<Parameters<typeof join>[0]> = {}): Peer {
  const p = join({ channel, label: 'ann', beat: 20, silence: 5000, ...over })
  peers.push(p)
  return p
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

/** Take from the host until `count` knocks have arrived or five seconds pass. */
async function until(h: Hub, count: number): Promise<Raw[]> {
  const got: Raw[] = []
  const deadline = Date.now() + 5000
  while (got.length < count && Date.now() < deadline) {
    got.push(...take(h, 100n))
    if (got.length < count) await sleep(10)
  }
  return got
}

/** The same for the joining end. */
async function heard(p: Peer, count: number): Promise<Event[]> {
  const got: Event[] = []
  const deadline = Date.now() + 5000
  while (got.length < count && Date.now() < deadline) {
    got.push(...p.take(100))
    if (got.length < count) await sleep(10)
  }
  return got
}

function kinds(knocks: Raw[]): string[] {
  return knocks.map((k) => (k.kind === 'shut' ? `shut(${k.why})` : k.kind))
}

describe('two ends of one channel', () => {
  it('a call said by the joining end reaches the host and its result comes back', async () => {
    const name = room()
    const h = host(name)
    const p = visitor(name, { label: 'ann' })
    expect(p.say('too early')).toBe(false)           // not open yet: nothing sent
    expect((await heard(p, 1)).map((e) => e.kind)).toEqual(['opened'])
    expect(p.say(JSON.stringify({ call: 'add', a: 2, b: 3 }))).toBe(true)

    const knocks = await until(h, 2)
    expect(kinds(knocks)).toEqual(['opened', 'said'])
    expect(knocks[0].who).toBe('ann')
    expect(knocks[0].seq).toBe(1n)
    expect(knocks[1].seq).toBe(2n)
    expect(knocks[1].line).toBe(knocks[0].line)
    const call = JSON.parse(knocks[1].text)
    expect(call).toEqual({ call: 'add', a: 2, b: 3 })

    expect(say(h, knocks[0].line, JSON.stringify({ result: call.a + call.b }))).toBe(true)
    const [reply] = await heard(p, 1)
    expect(reply).toEqual({ kind: 'said', why: '', text: '{"result":5}' })
  })

  it('two joining ends get two lines and each hears only its own', async () => {
    const name = room()
    const h = host(name)
    const a = visitor(name, { label: 'a' })
    const b = visitor(name, { label: 'b' })
    await heard(a, 1)
    await heard(b, 1)
    const opened = await until(h, 2)
    expect(opened.map((k) => k.who).sort()).toEqual(['a', 'b'])
    const lineOf = (who: string) => opened.find((k) => k.who === who)!.line
    expect(lineOf('a')).not.toBe(lineOf('b'))
    say(h, lineOf('b'), 'for b')
    expect((await heard(b, 1))[0].text).toBe('for b')
    await sleep(60)
    expect(a.take(10)).toEqual([])
  })

  it('a channel of another name is not heard', async () => {
    const h = host(room())
    const p = visitor(room(), { silence: 100 })
    const [gone] = await heard(p, 1)
    expect(gone).toEqual({ kind: 'shut', why: 'host_gone', text: 'no host answered' })
    expect(take(h, 10n)).toEqual([])
  })
})

describe('how a line ends', () => {
  it('leave is Shut(closed) with its text, and the line is gone', async () => {
    const name = room()
    const h = host(name)
    const p = visitor(name)
    await heard(p, 1)
    p.leave('bye now')
    const knocks = await until(h, 2)
    expect(kinds(knocks)).toEqual(['opened', 'shut(closed)'])
    expect(knocks[1].text).toBe('bye now')
    expect(say(h, knocks[0].line, 'late')).toBe(false)
    expect(p.say('late')).toBe(false)
  })

  it('a host close tells the peer the reason, and says false the second time', async () => {
    const name = room()
    const h = host(name)
    const p = visitor(name)
    await heard(p, 1)
    const [opened] = await until(h, 1)
    expect(close(h, opened.line, 'refused', 'banned')).toBe(true)
    expect(await heard(p, 1)).toEqual([{ kind: 'shut', why: 'refused', text: 'banned' }])
    const [gone] = take(h, 10n)
    expect(gone.kind).toBe('shut')
    expect(gone.why).toBe('refused')
    expect(gone.text).toBe('banned')
    expect(close(h, opened.line, 'closed', '')).toBe(false)
  })

  it('shut, the row teardown, tells every open line the host is gone', async () => {
    const name = room()
    const h = host(name)
    const a = visitor(name)
    const b = visitor(name)
    await heard(a, 1)
    await heard(b, 1)
    shut(h)
    expect((await heard(a, 1))[0]).toMatchObject({ kind: 'shut', why: 'host_gone' })
    expect((await heard(b, 1))[0]).toMatchObject({ kind: 'shut', why: 'host_gone' })
  })

  it('a peer that stops beating is shut silent, and its next word is expired', async () => {
    const name = room()
    const h = host(name, { silence: 150 })
    // a raw end that joins and then says nothing, as a page that was frozen
    const raw = new BroadcastChannel(name)
    raws.push(raw)
    const inbox: { t: string, to?: string, host?: string, why?: string }[] = []
    raw.onmessage = (ev) => { if (ev.data?.to === 'frozen') inbox.push(ev.data) }
    raw.postMessage({ v: 1, t: 'join', from: 'frozen', label: 'slow' })
    const knocks = await until(h, 2)
    expect(kinds(knocks)).toEqual(['opened', 'shut(silent)'])
    expect(knocks[0].who).toBe('slow')
    for (let i = 0; i < 200 && inbox.length < 2; i++) await sleep(10)   // the word to the peer lands after the knock
    expect(inbox.map((m) => m.t)).toEqual(['open', 'shut'])
    expect(inbox[1].why).toBe('silent')

    // it was only slow: it says something on the old line and is told so
    const hostId = inbox[0].host
    raw.postMessage({ v: 1, t: 'say', from: 'frozen', host: hostId, text: 'still here' })
    const deadline = Date.now() + 2000
    while (inbox.length < 3 && Date.now() < deadline) await sleep(10)
    expect(inbox[2]).toMatchObject({ t: 'shut', why: 'expired' })
    expect(take(h, 10n)).toEqual([])                 // no knock for the dropped line

    // the way back in is a new join: a new line
    raw.postMessage({ v: 1, t: 'join', from: 'frozen', label: 'slow' })
    const again = await until(h, 1)
    expect(kinds(again)).toEqual(['opened'])
    expect(again[0].line).toBe(2n)
  })

  it('a host that stops beating is host_gone on the joined end', async () => {
    const name = room()
    const h = host(name)
    const p = visitor(name, { silence: 150 })
    await heard(p, 1)
    // silence the host without telling anyone: its timer and channel stop
    ;(h as unknown as { tell: () => void }).tell = () => undefined
    const [gone] = await heard(p, 1)
    expect(gone).toEqual({ kind: 'shut', why: 'host_gone', text: 'no beat from the host' })
  })
})

describe('the limits are the door', () => {
  it('a message over most shuts the line too_large and delivers none of it', async () => {
    const name = room()
    const h = host(name, { most: 8 })
    const p = visitor(name, { most: 1000 })
    expect((await heard(p, 1)).map((e) => e.kind)).toEqual(['opened'])
    expect(p.say('12345678')).toBe(true)
    expect(p.say('123456789')).toBe(false)    // the host's `most` came with its open
    // a page that does not keep the rule: said by hand, as raw messages
    const raw = new BroadcastChannel(name)
    raws.push(raw)
    const hostId = (h as unknown as { me: string }).me
    const peerId = p.me
    raw.postMessage({ v: 1, t: 'say', from: peerId, host: hostId, text: '123456789' })
    const knocks = await until(h, 3)
    expect(kinds(knocks)).toEqual(['opened', 'said', 'shut(too_large)'])
    expect(knocks[1].text).toBe('12345678')
    expect(await heard(p, 1)).toEqual([{ kind: 'shut', why: 'too_large',
      text: 'message over the limit' }])
  })

  it('counts bytes, not characters, and the joining end refuses its own over-limit', async () => {
    const name = room()
    const h = host(name, { most: 4 })
    const p = visitor(name, { most: 4 })
    await heard(p, 1)
    expect(p.say('éé')).toBe(true)                   // 4 bytes
    expect(p.say('ééé')).toBe(false)                 // 6 bytes: never sent
    const knocks = await until(h, 2)
    expect(kinds(knocks)).toEqual(['opened', 'said'])
  })

  it('a line that makes held knocks wait is shut too_fast, and none queued is dropped',
    async () => {
      const name = room()
      const h = host(name, { held: 3 })
      const p = visitor(name)
      await heard(p, 1)
      for (const t of ['a', 'b', 'c', 'd']) p.say(t)
      await sleep(100)
      const knocks = take(h, 100n)
      // opened, a, b waited (3 held); c made it too fast
      expect(kinds(knocks)).toEqual(['opened', 'said', 'said', 'shut(too_fast)'])
      expect(knocks.filter((k) => k.kind === 'said').map((k) => k.text)).toEqual(['a', 'b'])
      expect((await heard(p, 1))[0]).toMatchObject({ kind: 'shut', why: 'too_fast' })
    })

  it('a join past lines is refused and makes no knock', async () => {
    const name = room()
    const h = host(name, { lines: 1 })
    const a = visitor(name, { label: 'a' })
    await heard(a, 1)
    await until(h, 1)
    const b = visitor(name, { label: 'b' })
    expect(await heard(b, 1)).toEqual([{ kind: 'shut', why: 'refused', text: 'channel full' }])
    expect(take(h, 10n)).toEqual([])
  })

  it('a door number that is not positive is refused by name', () => {
    expect(() => listen({ channel: room(), most: 0n, held: 1n, lines: 1n, beat: 1n,
      silence: 1n })).toThrow(/`most` must be a positive integer/)
    expect(() => listen({ channel: '', most: 1n, held: 1n, lines: 1n, beat: 1n,
      silence: 2n })).toThrow(/needs a name/)
    expect(() => listen({ channel: room(), most: 1n, held: 1n, lines: 1n, beat: 50n,
      silence: 50n })).toThrow(/`silence` \(50\) must be longer than `beat` \(50\)/)
    expect(() => join({ channel: room(), beat: 0 })).toThrow(/`beat` must be a positive integer/)
    expect(() => join({ channel: room(), most: Number.NaN })).toThrow(/`most` must be/)
  })

  it('a join while held knocks wait is refused and makes no knock', async () => {
    const name = room()
    const h = host(name, { held: 2 })
    const raw = new BroadcastChannel(name)
    raws.push(raw)
    const inbox: { t: string, to?: string, why?: string }[] = []
    raw.onmessage = (ev) => { if (String(ev.data?.to).startsWith('c')) inbox.push(ev.data) }
    // join, leave, again and again with fresh ids, while the host never takes
    for (const id of ['c1', 'c2', 'c3', 'c4']) {
      raw.postMessage({ v: 1, t: 'join', from: id, label: id })
      await sleep(15)
    }
    const knocks = take(h, 100n)
    expect(kinds(knocks)).toEqual(['opened', 'opened'])          // never more than held
    expect(inbox.filter((m) => m.t === 'shut')).toEqual([
      { v: 1, t: 'shut', to: 'c3', host: (h as unknown as { me: string }).me,
        why: 'refused', text: 'host is not reading' },
      { v: 1, t: 'shut', to: 'c4', host: (h as unknown as { me: string }).me,
        why: 'refused', text: 'host is not reading' }])
  })

  it('a label and a bye text are cut to most characters', async () => {
    const name = room()
    const h = host(name, { most: 4 })
    const p = visitor(name, { label: 'abcdefgh' })
    await heard(p, 1)
    p.leave('goodbye')
    const knocks = await until(h, 2)
    expect(knocks[0].who).toBe('abcd')
    expect(knocks[1].text).toBe('good')
  })

  it('the host beat reaches the joining end: it answers at the host rate, not its own',
    async () => {
      const name = room()
      // the host would shut a line silent after 120 ms; the page's own beat is 2000
      const h = host(name, { beat: 30, silence: 120 })
      const p = visitor(name, { beat: 2000 })
      await heard(p, 1)
      await sleep(400)
      expect(kinds(take(h, 100n))).toEqual(['opened'])    // still open: it kept beating
      expect(p.take(10)).toEqual([])
    })

  it('forged and malformed messages make no knock and break nothing', async () => {
    const name = room()
    const h = host(name)
    const p = visitor(name)
    await heard(p, 1)
    await until(h, 1)
    const raw = new BroadcastChannel(name)
    raws.push(raw)
    const hostId = (h as unknown as { me: string }).me
    for (const junk of [null, 'text', 7, [], {}, { v: 2, t: 'say' }, { v: 1 }, { v: 1, t: 'say' },
      { v: 1, t: 'say', host: hostId }, { v: 1, t: 'say', host: hostId, from: 7, text: 1 },
      { v: 1, t: 'join' }, { v: 1, t: 'join', from: 5 }, { v: 1, t: 'bye', host: hostId },
      { v: 1, t: 'say', from: 'nobody', host: 'not-the-host', text: 'x' },
      { v: 1, t: 'host' }, { v: 1, t: 'host', host: hostId, since: 0 }]) raw.postMessage(junk)
    await sleep(80)
    expect(take(h, 100n)).toEqual([])
    expect(p.take(10)).toEqual([])
    expect(h.failure).toBeNull()
  })
})

describe('one host per channel name', () => {
  it('the later host stops at once, not a beat later, and its next take throws', async () => {
    const name = room()
    const first = host(name, { beat: 5000, silence: 20000 })
    await sleep(5)
    const second = host(name, { beat: 5000, silence: 20000 })
    const deadline = Date.now() + 2000    // a beat is 5000: the claim is answered at once
    while (second.failure === null && Date.now() < deadline) await sleep(10)
    expect(() => take(second, 10n)).toThrow(`channel "${name}" already has a host`)
    expect(first.failure).toBeNull()
    const p = visitor(name)
    expect((await heard(p, 1))[0].kind).toBe('opened')
    expect(kinds(await until(first, 1))).toEqual(['opened'])
  })
})

/** `stdlib/channel.rvl` compiled and emitted for this tier, as a module under
 * tests/generated/ (the emitter's own `--runtime` path from there). */
function emitChannelWire(): string {
  const generated = path.join(BACKEND, 'tests', 'generated')
  fs.mkdirSync(generated, { recursive: true })
  const ir = path.join(generated, 'channel_wire.ir.json')
  const python = pythonInterpreter()
  const compiled = spawnSync(python, ['-c', [
    'import json, sys',
    'sys.path.insert(0, sys.argv[1] + "/src")',
    'from revl import compile_files',
    'print(json.dumps(compile_files([sys.argv[1] + "/stdlib/channel.rvl"])))',
  ].join('\n'), ROOT], { encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 })
  expect(compiled.stderr, 'compile stdlib/channel.rvl').toBe('')
  fs.writeFileSync(ir, compiled.stdout)
  const emitted = spawnSync(python, ['emit.py', '--runtime', '../../runtime.ts', ir],
    { cwd: BACKEND, encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 })
  expect(emitted.status, emitted.stderr).toBe(0)
  const out = path.join(generated, 'channel_wire.ts')
  fs.writeFileSync(out, emitted.stdout)
  return out
}

describe('the emitted provider', () => {
  it('runs as a component: a joining page calls, the program answers, the row frees the channel',
    async () => {
      // the stdlib refs resolve against the install root the runner names
      ;(globalThis as { __REVL_STDLIB_REF_ROOT__?: string }).__REVL_STDLIB_REF_ROOT__ = ROOT
      const mod = await import(emitChannelWire())
      const name = room()
      const ctx = new Context()
      const fiber = ctx.plugin(mod.ChannelWire, { channel: name, beat: 20n })
      await fiber.await()
      const wire = (ctx as unknown as Record<string, {
        take(most: bigint): { seq: bigint, kind: { kind: string, value?: { kind: string } },
          line: bigint, who: string, text: string }[]
        say(line: bigint, text: string): boolean
        close(line: bigint, why: unknown, text: string): boolean
      }>).wire

      const p = visitor(name, { label: 'ann' })
      expect((await heard(p, 1))[0].kind).toBe('opened')
      p.say('{"call":"add","a":2,"b":3}')
      const knocks: ReturnType<typeof wire.take> = []
      const deadline = Date.now() + 5000
      while (knocks.length < 2 && Date.now() < deadline) {
        knocks.push(...wire.take(512n))
        await sleep(10)
      }
      expect(knocks.map((k) => k.kind.kind)).toEqual(['Opened', 'Said'])
      expect(knocks[0].who).toBe('ann')
      expect(knocks[1].text).toBe('{"call":"add","a":2,"b":3}')
      const line = knocks[0].line
      expect(wire.say(line, '{"result":5}')).toBe(true)
      expect((await heard(p, 1))[0].text).toBe('{"result":5}')

      expect(wire.close(line, mod.Refused(), 'banned')).toBe(true)
      expect(await heard(p, 1)).toEqual([{ kind: 'shut', why: 'refused', text: 'banned' }])
      const [gone] = wire.take(512n)
      expect(gone.kind).toEqual({ kind: 'Shut', value: { kind: 'Refused' } })
      expect(gone.text).toBe('banned')
      expect(wire.say(line, 'late')).toBe(false)

      // the row's teardown: `channel_shut(door)` closes the channel, so a
      // later join finds no host
      await fiber.dispose()
      const later = visitor(name, { silence: 100 })
      expect((await heard(later, 1))[0]).toMatchObject({ kind: 'shut', why: 'host_gone' })
    })
})

describe('the emitted provider carries each door field', () => {
  it('most, held, lines and silence are the row config, each its own', async () => {
    ;(globalThis as { __REVL_STDLIB_REF_ROOT__?: string }).__REVL_STDLIB_REF_ROOT__ = ROOT
    const mod = await import(emitChannelWire())
    const name = room()
    const ctx = new Context()
    const fiber = ctx.plugin(mod.ChannelWire,
      { channel: name, most: 8n, held: 3n, lines: 1n, beat: 20n, silence: 300n })
    await fiber.await()
    const wire = (ctx as unknown as Record<string, {
      take(most: bigint): { kind: { kind: string, value?: { kind: string } }, text: string }[]
    }>).wire
    const kindsOf = (ks: ReturnType<typeof wire.take>) => ks.map((k) =>
      k.kind.kind === 'Shut' ? `Shut(${k.kind.value?.kind})` : k.kind.kind)

    const a = visitor(name, { label: 'a' })
    await heard(a, 1)
    expect(a.say('123456789')).toBe(false)            // most is 8, as the host's door says
    const b = visitor(name, { label: 'b' })
    expect(await heard(b, 1)).toEqual([{ kind: 'shut', why: 'refused', text: 'channel full' }])
    for (const t of ['12345678', 'x', 'y']) a.say(t)  // held is 3: opened, 12345678, x wait
    await sleep(100)
    const knocks = wire.take(100n)
    expect(kindsOf(knocks)).toEqual(['Opened', 'Said', 'Said', 'Shut(TooFast)'])
    expect(knocks.filter((k) => k.kind.kind === 'Said').map((k) => k.text)).toEqual(['12345678', 'x'])

    // silence is 300 ms, not the default 8000: a page that never beats is shut
    const raw = new BroadcastChannel(name)
    raws.push(raw)
    raw.postMessage({ v: 1, t: 'join', from: 'frozen', label: 'z' })
    const got: ReturnType<typeof wire.take> = []
    const deadline = Date.now() + 3000
    while (got.length < 2 && Date.now() < deadline) {
      got.push(...wire.take(100n))
      await sleep(20)
    }
    expect(kindsOf(got)).toEqual(['Opened', 'Shut(Silent)'])
    await fiber.dispose()
  })
})

describe('the module the emitted thunk loads', () => {
  it('loads under plain node through createRequire, every ref a function', () => {
    // vitest transforms TypeScript itself; the shipping runtime is node's own
    // type stripping. Load it the way `_revl_require` does.
    const url = new URL('../revl_channel_ts.ts', import.meta.url).href
    const probe = spawnSync(process.execPath, ['--input-type=module', '-e', [
      "import { createRequire } from 'node:module'",
      'const m = createRequire(import.meta.url)(new URL(process.argv[1]).pathname)',
      "for (const s of ['listen', 'shut', 'settled', 'take', 'say', 'close', 'join'])",
      "  if (typeof m[s] !== 'function') throw new Error(s + ' is not a function')",
    ].join('\n'), url], { encoding: 'utf8' })
    expect(probe.stderr).toBe('')
    expect(probe.status).toBe(0)
  })
})
