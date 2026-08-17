// Verifies the TS bridge's canonical ADT/Result codec (docs/interop-bridge.md
// "Canonical value encoding"): the wire carries {"$kind","$value"} and the
// native `{ kind, value }` form round-trips.
//
// It does NOT emit examples/outcome.rvl to TS: the TS emitter's provide-method
// expression path (emit.py `_expr`) does not yet handle the `adt` node (only v3
// `fn` bodies do), so an ADT-returning provide method cannot emit to TS today.
// That is a separate emitter gap, out of the bridge's scope. Instead this drives
// the real `serve` over a socket with a service returning native ADT values,
// reads the raw wire, and checks encode + decode directly.
//
// Run: node demo/bridge_ts_adt.mts

import net from 'node:net'
import fs from 'node:fs'

import { serve, encodeValue, decodeValue } from '../backends/typescript/bridge.ts'

// the emitter's runtime ADT form is `{ kind, value? }`; records are plain objects
const hit = { kind: 'Hit', value: { id: 1, name: 'ada' } }
const missing = { kind: 'Missing' }
const ok = { kind: 'Ok', value: { id: 1, name: 'ada' } }

let failures = 0
const check = (name: string, ok_: boolean, detail = '') => {
  console.log(`  ${ok_ ? 'PASS' : 'FAIL'}  ${name.padEnd(22)} ${detail}`)
  if (!ok_) failures++
}
const eq = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

// unit: encode / decode / round-trip / passthrough
check('encode-adt', eq(encodeValue(hit), { $kind: 'Hit', $value: { id: 1, name: 'ada' } }))
check('encode-nullary', eq(encodeValue(missing), { $kind: 'Missing' }))
check('decode-adt', eq(decodeValue({ $kind: 'Hit', $value: { id: 1, name: 'ada' } }), hit))
check('roundtrip-adt', eq(decodeValue(encodeValue(hit)), hit))
check('scalars-passthrough', encodeValue(1) === 1 && decodeValue('x') === 'x' && eq(encodeValue([1, 2]), [1, 2]))
check('record-passthrough', eq(encodeValue({ id: 1, name: 'ada' }), { id: 1, name: 'ada' }))
check('nested-adt-in-record', eq(encodeValue({ best: hit }), { best: { $kind: 'Hit', $value: { id: 1, name: 'ada' } } }))

// real socket: serve a service returning native ADTs, read the raw wire
const sock = `/tmp/revl_tsadt_${process.pid}.sock`
const fakeCtx: any = { dir: { lookup: () => hit, check: () => ok } }
const server = await serve(fakeCtx, ['dir'], sock)

const call = (method: string): Promise<string> =>
  new Promise((resolve, reject) => {
    const s = net.connect(sock)
    let buf = ''
    s.on('connect', () => s.write(JSON.stringify({ key: 'dir', method, args: [1] }) + '\n'))
    s.on('data', (d) => {
      buf += d
      const i = buf.indexOf('\n')
      if (i >= 0) {
        s.end()
        resolve(buf.slice(0, i))
      }
    })
    s.on('error', reject)
  })

const lookupWire = await call('lookup')
console.log('  lookup wire:', lookupWire)
check('serve-encodes', lookupWire.includes('"$kind":"Hit"') && lookupWire.includes('"name":"ada"'),
  'serve put {$kind,$value} on the wire')
check('wire-decodes', eq(decodeValue(JSON.parse(lookupWire).value), hit), 'decode(wire) -> native Hit(Row)')
const checkWire = await call('check')
check('result-encodes', checkWire.includes('"$kind":"Ok"'), 'Result Ok crosses as {$kind:"Ok"}')

server.close()
try {
  fs.unlinkSync(sock)
} catch {
  /* gone */
}
console.log(failures ? `\n${failures} check(s) FAILED` : '\nall checks passed')
process.exit(failures ? 1 : 0)
