// issue #1566: an `Int` crosses the ts seam as a `bigint`.
//
// The wire carries `Int` as a JSON number (docs/interop-bridge.md), JSON.parse
// makes that a JS `number`, and the ts tier's `Int` is a `bigint`: `serve`
// handed the number straight to the provider method, whose `n + 1n` threw
// `Cannot mix BigInt and other types`. A value past 2^53 lost digits in
// JSON.parse, and the encoder refused to send one at all.
//
// `serve` and a proxy's reply now decode by the declared types the conductor
// puts in a node process's spec (`placement.seam_typing`). The end-to-end
// placements (py -> ts, ts -> py, ts -> ts, probe -> ts) are
// tests/test_ts_seam_int_1566.py; these are the codec and the served side.
import { afterEach, describe, expect, it } from 'vitest'
import net from 'node:net'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { type SeamTyping, decodeAs, encodeValue, parseWire, serve } from '../bridge.ts'

const BIG = 9007199254740993n // 2^53 + 1

const TYPES = {
  Pair: { params: [], kind: 'record', fields: { a: 'Int', b: 'Str' } },
  Box: { params: ['T'], kind: 'record', fields: { v: 'T' } },
  Shape: {
    params: [], kind: 'variant',
    cases: [{ name: 'Circle', payload: 'Int' }, { name: 'Empty', payload: null }],
  },
}

const TYPING: SeamTyping = {
  signatures: {
    s: {
      bump: { params: ['Int'], returns: 'Int' },
      pair: { params: ['Pair'], returns: 'Pair' },
      sum: { params: ['List[Int]'], returns: 'Int' },
    },
  },
  types: TYPES,
}

describe('decodeAs: a wire value by its declared type', () => {
  it('an Int becomes a bigint, a Float stays a number', () => {
    expect(decodeAs(41, 'Int', TYPES)).toBe(41n)
    expect(decodeAs(BIG, 'Int', TYPES)).toBe(BIG)
    expect(decodeAs(1.5, 'Float', TYPES)).toBe(1.5)
    expect(decodeAs('x', 'Str', TYPES)).toBe('x')
  })

  it('walks lists, records, generics, variants, Results and Opt', () => {
    expect(decodeAs([1, 2], 'List[Int]', TYPES)).toEqual([1n, 2n])
    expect(decodeAs({ a: 1, b: 'x' }, 'Pair', TYPES)).toEqual({ a: 1n, b: 'x' })
    expect(decodeAs({ v: [3] }, 'Box[List[Int]]', TYPES)).toEqual({ v: [3n] })
    expect(decodeAs({ $kind: 'Circle', $value: 2 }, 'Shape', TYPES))
      .toEqual({ kind: 'Circle', value: 2n })
    expect(decodeAs({ $kind: 'Ok', $value: 5 }, 'Result[Int, Str]', TYPES))
      .toEqual({ kind: 'Ok', value: 5n })
    expect(decodeAs({ $kind: 'Err', $value: 'no' }, 'Result[Int, Str]', TYPES))
      .toEqual({ kind: 'Err', value: 'no' })
    expect(decodeAs(7, 'Opt[Int]', TYPES)).toBe(7n)
    expect(decodeAs(null, 'Opt[Int]', TYPES)).toBe(null)
  })

  it('an undeclared type decodes exactly as before', () => {
    expect(decodeAs({ $kind: 'Ok', $value: 1 }, null, TYPES)).toEqual({ kind: 'Ok', value: 1 })
    expect(decodeAs(3, 'Mystery', TYPES)).toBe(3)
  })
})

describe('the wire keeps an Int past 2^53 exact', () => {
  it('parseWire reads the literal digits, not the rounded double', () => {
    expect(parseWire('{"v": 9007199254740993}').v).toBe(BIG)
    expect(parseWire('{"v": 41, "f": 1.5}')).toEqual({ v: 41, f: 1.5 })
  })

  it('encodeValue writes the digits instead of refusing', () => {
    expect(JSON.stringify(encodeValue({ a: BIG, b: [2n] }))).toBe('{"a":9007199254740993,"b":[2]}')
    expect(JSON.stringify(encodeValue(-BIG))).toBe('-9007199254740993')
  })
})

// --- the served side, over a real socket ------------------------------------

const dirs: string[] = []
const servers: net.Server[] = []

afterEach(() => {
  for (const server of servers.splice(0)) server.close()
  for (const dir of dirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true })
})

function socketPath(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'revl_seam_int_'))
  dirs.push(dir)
  return path.join(dir, 'provider.sock')
}

/** Send one raw request LINE (so a literal past 2^53 is on the wire as
 *  digits), return the raw reply line. */
function callRaw(sock: string, line: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const client = net.connect(sock)
    let buf = ''
    client.on('connect', () => client.write(line + '\n'))
    client.on('data', (chunk) => {
      buf += chunk
      const nl = buf.indexOf('\n')
      if (nl >= 0) { client.end(); resolve(buf.slice(0, nl)) }
    })
    client.on('error', reject)
  })
}

/** A provider that does bigint arithmetic, as an emitted ts provider does. */
function providerCtx(): any {
  return {
    s: {
      bump: (n: bigint) => n + 1n,
      pair: (p: { a: bigint; b: string }) => ({ a: p.a + 1n, b: p.b }),
      sum: (xs: bigint[]) => xs[0] + xs[1],
    },
  }
}

describe('serve: arguments decoded by the declared types', () => {
  it('an Int argument reaches the method as a bigint', async () => {
    const sock = socketPath()
    servers.push(await serve(providerCtx(), { s: ['bump', 'pair', 'sum'] }, sock, TYPING))
    expect(await callRaw(sock, '{"key":"s","method":"bump","args":[41]}'))
      .toBe('{"ok":true,"value":42}')
    expect(await callRaw(sock, '{"key":"s","method":"pair","args":[{"a":41,"b":"x"}]}'))
      .toBe('{"ok":true,"value":{"a":42,"b":"x"}}')
    expect(await callRaw(sock, '{"key":"s","method":"sum","args":[[41,42]]}'))
      .toBe('{"ok":true,"value":83}')
  })

  it('an Int past 2^53 crosses both ways exact', async () => {
    const sock = socketPath()
    servers.push(await serve(providerCtx(), { s: ['bump'] }, sock, TYPING))
    expect(await callRaw(sock, '{"key":"s","method":"bump","args":[9007199254740993]}'))
      .toBe('{"ok":true,"value":9007199254740994}')
  })

  it('without declared types the argument arrives as it did before', async () => {
    const sock = socketPath()
    servers.push(await serve(providerCtx(), { s: ['bump'] }, sock))
    const reply = JSON.parse(await callRaw(sock, '{"key":"s","method":"bump","args":[41]}'))
    expect(reply.ok).toBe(false)
    expect(reply.error).toMatch(/Cannot mix BigInt/)
  })
})
