// The ts tier of `stdlib/socket.rvl`'s accepted-socket provider (issue #3357):
// `revl_socket_ts.ts`, the module each of the provider's `@ts ref` externs
// imports. The py tier's suite is tests/test_socket_stdlib.py; this one pins
// the same behaviour on node, with node's global `WebSocket` as the client
// wherever a client that reads will do.
//
// Every entry point is called as the emitted thunk calls it: revl's `Int` is a
// `bigint` on this tier, so the door's numbers and the line numbers go in as
// `bigint`s, and the knocks' `seq` and `line` come back as `bigint`s (the
// emitted `Raw` record's fields).
//
// What it pins (the numbered list of the #3357 proposal, 1, 2, 3, 5 and 7):
//   1. Opened, Said, Shut arrive in order on one line, `who` the Origin;
//   2. an Origin that is not listed, two Origins, or none (unless `bare`) is
//      answered 403 before the upgrade and makes no knock;
//   3. a message over `most` shuts the line 1009, none of it delivered;
//   5. `say` to a line over `limit` unwritten bytes answers false, and a line
//      that reads still hears;
//   7. `shut` (the row's teardown) closes open lines 1001 and frees the port.
// Then the same through `stdlib/socket.rvl` itself, emitted for this tier and
// plugged into a cordis Context, and the module loaded the way the emitted
// thunk loads it, under plain node.
import { afterEach, describe, expect, it } from 'vitest'
import { Context } from 'cordis'
import * as fs from 'node:fs'
import * as http from 'node:http'
import * as net from 'node:net'
import * as crypto from 'node:crypto'
import * as path from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { pythonInterpreter } from '../scripts/emit-fixtures.ts'
import { type Listener, type Raw, listen, say, shut, take } from '../revl_socket_ts.ts'

const BACKEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const ROOT = path.resolve(BACKEND, '..', '..')

const ORIGIN = 'http://play.test'

const made: Listener[] = []

afterEach(() => {
  for (const l of made.splice(0)) shut(l)
})

async function open(over: Partial<Record<'origins' | 'bare' | 'most' | 'limit' | 'port',
  unknown>> = {}): Promise<Listener> {
  const l = listen({
    address: '127.0.0.1', port: 0n, origins: [ORIGIN], bare: false,
    most: 65536n, held: 1024n, limit: 1048576n, ...over,
  } as Parameters<typeof listen>[0])
  made.push(l)
  await l.ready
  return l
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

/** Take until `count` knocks have arrived or five seconds pass. */
async function until(l: Listener, count: number): Promise<Raw[]> {
  const got: Raw[] = []
  const deadline = Date.now() + 5000
  while (got.length < count && Date.now() < deadline) {
    got.push(...take(l, 100n))
    if (got.length < count) await sleep(20)
  }
  return got
}

function kinds(knocks: Raw[]): string[] {
  return knocks.map((k) => (k.kind === 'shut' ? `shut(${k.why})` : k.kind))
}

/** A node `WebSocket` with an Origin, open, what it heard and its closing code. */
async function client(port: number, origin = ORIGIN) {
  const heard: string[] = []
  const ws = new WebSocket(`ws://127.0.0.1:${port}/wire`,
    { headers: { Origin: origin } } as unknown as string[])
  ws.onmessage = (e) => { heard.push(String(e.data)) }
  // the closing code, or -1 if the line is not closed within five seconds
  const closed = Promise.race([
    new Promise<number>((resolve) => { ws.onclose = (e) => resolve(e.code) }),
    sleep(5000).then(() => -1),
  ])
  await new Promise<void>((resolve, reject) => {
    ws.onopen = () => resolve()
    ws.onerror = () => reject(new Error('the upgrade failed'))
  })
  return { ws, heard, closed }
}

/** The status line an upgrade request is answered with, Origins as given. */
function handshake(port: number, origins: string[]): Promise<number> {
  return new Promise((resolve, reject) => {
    const headers: [string, string][] = [
      ['Host', `127.0.0.1:${port}`], ['Upgrade', 'websocket'],
      ['Connection', 'Upgrade'], ['Sec-WebSocket-Version', '13'],
      ['Sec-WebSocket-Key', crypto.randomBytes(16).toString('base64')],
      ...origins.map((o): [string, string] => ['Origin', o]),
    ]
    const req = http.request({ host: '127.0.0.1', port, path: '/wire',
      headers: headers.flat() as unknown as http.OutgoingHttpHeaders })
    req.on('response', (res) => { res.resume(); resolve(res.statusCode ?? 0) })
    req.on('upgrade', (_res, sock) => { sock.destroy(); resolve(101) })
    req.on('error', reject)
    req.end()
  })
}

/** A raw client that upgrades and then never reads. */
function stalled(port: number): Promise<net.Socket> {
  return new Promise((resolve) => {
    const sock = net.connect(port, '127.0.0.1', () => {
      sock.write(`GET /wire HTTP/1.1\r\nHost: 127.0.0.1:${port}\r\n`
        + 'Upgrade: websocket\r\nConnection: Upgrade\r\n'
        + `Sec-WebSocket-Key: ${crypto.randomBytes(16).toString('base64')}\r\n`
        + `Sec-WebSocket-Version: 13\r\nOrigin: ${ORIGIN}\r\n\r\n`)
      sock.once('data', () => {
        sock.pause()
        resolve(sock)
      })
    })
  })
}

describe('the three events', () => {
  it('arrive in order on one line, the Origin as who (1)', async () => {
    const l = await open()
    const c = await client(l.port)
    c.ws.send('hi')
    c.ws.close()
    await c.closed
    const knocks = await until(l, 3)
    expect(kinds(knocks)).toEqual(['opened', 'said', 'shut(closed)'])
    expect(new Set(knocks.map((k) => k.line)).size).toBe(1)
    for (const k of knocks) {
      expect(typeof k.seq).toBe('bigint')
      expect(typeof k.line).toBe('bigint')
    }
    expect(knocks[0].seq < knocks[1].seq && knocks[1].seq < knocks[2].seq).toBe(true)
    expect(knocks[0].who).toBe(ORIGIN)
    expect(knocks[1].who).toBe('')
    expect(knocks[1].text).toBe('hi')
  })
})

describe('the origin rule', () => {
  it('answers 403 before the upgrade and makes no knock (2)', async () => {
    const l = await open()
    expect(await handshake(l.port, ['http://evil.test'])).toBe(403)
    expect(await handshake(l.port, [])).toBe(403)
    expect(await handshake(l.port, [ORIGIN, ORIGIN])).toBe(403)
    await sleep(200)
    expect(take(l, 100n)).toEqual([])

    const bare = await open({ bare: true })
    expect(await handshake(bare.port, [])).toBe(101)
    const [opened] = await until(bare, 1)
    expect(opened.kind).toBe('opened')
    expect(opened.who).toBe('')
  })
})

describe('the limits', () => {
  it('shuts a message over `most` with 1009, none of it delivered (3)', async () => {
    const l = await open({ most: 16n })
    const c = await client(l.port)
    c.ws.send('x'.repeat(17))
    expect(await c.closed).toBe(1009)
    expect(kinds(await until(l, 2))).toEqual(['opened', 'shut(too_large)'])
  })

  it('skips `say` to a line over `limit`, and a reading line still hears (5)',
    async () => {
      const l = await open({ limit: 1024n })
      const quiet = await stalled(l.port)
      const reading = await client(l.port)
      const lines = (await until(l, 2)).map((k) => k.line)
      const big = 'y'.repeat(65536)
      const answers: boolean[] = []
      for (let i = 0; i < 400; i++) {
        answers.push(say(l, lines[0], big))
        if (!answers[answers.length - 1]) break
        await sleep(5)
      }
      expect(answers[0]).toBe(true)
      expect(answers[answers.length - 1]).toBe(false)
      expect(say(l, lines[1], 'still here')).toBe(true)
      const deadline = Date.now() + 5000
      while (reading.heard.length === 0 && Date.now() < deadline) await sleep(20)
      expect(reading.heard).toEqual(['still here'])
      quiet.destroy()
    })
})

describe('the listener is an acquisition', () => {
  it('closes open lines 1001 and frees the port on shut (7)', async () => {
    const l = await open()
    const port = l.port
    const c = await client(port)
    await until(l, 1)
    // while the listener lives, a bind of its port is refused
    expect(await rebind(port)).toBe('EADDRINUSE')
    shut(made.splice(made.indexOf(l), 1)[0])
    expect(await c.closed).toBe(1001)
    expect(await rebind(port)).toBe('bound')
  })
})

/** `stdlib/socket.rvl` compiled and emitted for this tier, as a module under
 * tests/generated/ (the emitter's own `--runtime` path from there). */
function emitSocketWire(): string {
  const generated = path.join(BACKEND, 'tests', 'generated')
  fs.mkdirSync(generated, { recursive: true })
  const ir = path.join(generated, 'socket_wire.ir.json')
  const python = pythonInterpreter()
  const compiled = spawnSync(python, ['-c', [
    'import json, sys',
    'sys.path.insert(0, sys.argv[1] + "/src")',
    'from revl import compile_files',
    'print(json.dumps(compile_files([sys.argv[1] + "/stdlib/socket.rvl"])))',
  ].join('\n'), ROOT], { encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 })
  expect(compiled.stderr, 'compile stdlib/socket.rvl').toBe('')
  fs.writeFileSync(ir, compiled.stdout)
  const emitted = spawnSync(python, ['emit.py', '--runtime', '../../runtime.ts', ir],
    { cwd: BACKEND, encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 })
  expect(emitted.status, emitted.stderr).toBe(0)
  const out = path.join(generated, 'socket_wire.ts')
  fs.writeFileSync(out, emitted.stdout)
  return out
}

describe('the emitted provider', () => {
  it('runs as a component: it hears, answers, shuts, and frees its port on dispose',
    async () => {
      // the stdlib refs resolve against the install root the runner names
      ;(globalThis as { __REVL_STDLIB_REF_ROOT__?: string }).__REVL_STDLIB_REF_ROOT__ = ROOT
      const mod = await import(emitSocketWire())
      const probe = net.createServer()
      const port = await new Promise<number>((resolve) => probe.listen(0, '127.0.0.1',
        () => resolve((probe.address() as net.AddressInfo).port)))
      await new Promise<void>((resolve) => probe.close(() => resolve()))

      const ctx = new Context()
      const fiber = ctx.plugin(mod.SocketWire, { port: BigInt(port), origins: [ORIGIN] })
      await fiber.await()
      const wire = (ctx as unknown as Record<string, {
        take(most: bigint): { seq: bigint, kind: { kind: string, value?: { kind: string } },
          line: bigint, who: string, text: string }[]
        say(line: bigint, text: string): boolean
        close(line: bigint, why: unknown, text: string): boolean
      }>).wire
      let c: Awaited<ReturnType<typeof client>> | null = null
      for (let i = 0; i < 100 && c === null; i++) {
        c = await client(port).catch(() => null)   // the bind lands on the event loop
        if (c === null) await sleep(20)
      }
      if (c === null) throw new Error('the emitted provider never listened')
      c.ws.send('hi')
      const knocks: ReturnType<typeof wire.take> = []
      const deadline = Date.now() + 5000
      while (knocks.length < 2 && Date.now() < deadline) {
        knocks.push(...wire.take(512n))
        await sleep(20)
      }
      expect(knocks.map((k) => k.kind.kind)).toEqual(['Opened', 'Said'])
      expect(knocks[0].who).toBe(ORIGIN)
      expect(knocks[1].text).toBe('hi')
      const line = knocks[0].line
      expect(wire.say(line, 'yo')).toBe(true)
      while (c.heard.length === 0 && Date.now() < deadline) await sleep(20)
      expect(c.heard).toEqual(['yo'])
      expect(wire.close(line, mod.Refused(), 'banned')).toBe(true)
      expect(await c.closed).toBe(1008)
      const [gone] = wire.take(512n)
      expect(gone.kind).toEqual({ kind: 'Shut', value: { kind: 'Refused' } })
      expect(gone.text).toBe('banned')
      expect(wire.say(line, 'late')).toBe(false)

      expect(await rebind(port)).toBe('EADDRINUSE')
      await fiber.dispose()           // the row's teardown: `socket_shut(door)`
      expect(await rebind(port)).toBe('bound')
    })
})

describe('the module the emitted thunk loads', () => {
  it('loads under plain node through createRequire, every ref a function', () => {
    // vitest transforms TypeScript itself; the shipping runtime is node's own
    // type stripping, which refuses some syntax vitest accepts (a parameter
    // property, an `enum`). Load it the way `_revl_require` does.
    const url = new URL('../revl_socket_ts.ts', import.meta.url).href
    const probe = spawnSync(process.execPath, ['--input-type=module', '-e', [
      "import { createRequire } from 'node:module'",
      'const m = createRequire(import.meta.url)(new URL(process.argv[1]).pathname)',
      "for (const s of ['listen', 'shut', 'settled', 'take', 'say', 'close'])",
      "  if (typeof m[s] !== 'function') throw new Error(s + ' is not a function')",
    ].join('\n'), url], { encoding: 'utf8' })
    expect(probe.stderr).toBe('')
    expect(probe.status).toBe(0)
  })
})

/** Bind `port` the way a server does (node sets SO_REUSEADDR), then let go. */
function rebind(port: number): Promise<string> {
  return new Promise((resolve) => {
    const again = net.createServer()
    again.once('error', (e: NodeJS.ErrnoException) => resolve(e.code ?? 'error'))
    again.listen(port, '127.0.0.1', () => again.close(() => resolve('bound')))
  })
}
