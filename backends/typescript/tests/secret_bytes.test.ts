// A declared `Secret[Bytes]` must be scrubbed like any other (roadmap item 421
// F6(e), the typescript half).
//
// `Bytes` is a first-class declared type and this tier maps it to `Uint8Array`
// (`emit.py`'s `_TS_TYPE`), so a `Secret[Bytes]` config field, service
// parameter or extern return reaches the registry as a byte view. The registry
// walks an object for its members — but a byte view's members are its byte
// NUMBERS, and `String(byte)` is at most three characters, under `MIN_MARKABLE`.
// So every face of the payload was under the bound and a `Secret[Bytes]`
// registered NOTHING: its content crossed the host trace, the probe channel and
// the seam reply verbatim, and no value of that type was ever redacted, not
// even a long one.
//
// The payload wears a different face in each renderer and no one of them is
// derivable from another:
//
//   * a template literal writes the COMMA-JOINED decimals — `${payload}` is
//     `Array.prototype.join(',')` on a typed array, and it is what every
//     `record(...)` sink in `runtime.ts` interpolates (`stream.emit ${item}`,
//     `insert(${key}, ${value})`, `${sql}`, `job.run ${name}`);
//   * a host that prints the payload writes the DECODED text, which is what a
//     `Buffer` interpolation produces (`${Buffer.from(…)}` is `toString()`);
//   * a sink that marshals the value writes a JSON BODY — `{"0":67,…}` for a
//     `Uint8Array`, `{"type":"Buffer","data":[…]}` for a `Buffer`.
//
// The go tier registers the same three faces off a `[]byte` (`revlRegisterValue`
// decodes the slice and adds the value's own json body beside the `%v` display
// form), and the py tier's `confidential._needles` walks the same way: a
// polyglot composition has to redact the same bytes whichever tier wrote the
// line.
//
// Every assertion is PAIRED — the face absent AND the placeholder present — so
// none can pass on an empty string, and the payload's faces are asserted to be
// free of the decoded text first, so a registration that covered only the
// decoded form cannot satisfy this file.
import { afterEach, describe, expect, it } from 'vitest'

import { forgetSecrets, markSecret, redactText } from '../runtime.ts'

const REDACTED = '<redacted:secret>'

// Long enough to clear `MIN_MARKABLE` in every face it wears, and distinctive
// enough that an accidental match against ordinary trace text is impossible.
const TEXT = 'CANARY-421-F6E'
const PAYLOAD = new TextEncoder().encode(TEXT)
const JOINED = Array.from(PAYLOAD).join(',')
const JSON_BODY = JSON.stringify(PAYLOAD)

afterEach(() => {
  // The registry is process-wide by design, so each test starts and ends empty;
  // otherwise one test's payload would redact another's.
  forgetSecrets()
})

describe('a registered bytes secret', () => {
  it('is scrubbed from every face a renderer can write', () => {
    markSecret(PAYLOAD)

    // [name, the line a sink actually writes, the payload as THAT renderer
    // writes it]. Each line is asserted to contain its own face first, so the
    // scrub below cannot pass because the face was never there.
    const faces: Array<[string, string, string]> = [
      ['joined', `stream.emit ${PAYLOAD}`, JOINED],
      ['json', `insert(k, ${JSON_BODY})`, JSON_BODY],
      ['decoded', `blob=${TEXT}`, TEXT],
    ]

    for (const [name, line, face] of faces) {
      expect(line, `${name} does not carry its own face`).toContain(face)
      const out = redactText(line)
      expect(out, `${name} still carried the payload`).not.toContain(face)
      expect(out, `${name} was not redacted at all`).toContain(REDACTED)
    }
  })

  it('registers faces the decoded form cannot cover', () => {
    // The leak, stated as its own assertion. The joined and json faces carry no
    // decoded text at all, so a registration that reached only the decoded form
    // passes the test above and fails here — and these two are the faces every
    // `record(...)` sink in this tier writes.
    expect(JOINED).not.toContain(TEXT)
    expect(JSON_BODY).not.toContain(TEXT)

    markSecret(PAYLOAD)

    expect(redactText(`stream.emit ${PAYLOAD}`)).toBe(`stream.emit ${REDACTED}`)
    expect(redactText(`insert(k, ${JSON_BODY})`)).toBe(`insert(k, ${REDACTED})`)
  })

  it('covers a Buffer, whose own faces differ from a Uint8Array', () => {
    // A `Buffer` IS a `Uint8Array`, but it is not rendered like one: a template
    // literal writes its DECODED text and `JSON.stringify` writes a tagged
    // object. Registering the view's json face alone would miss both.
    const buf = Buffer.from(PAYLOAD)
    markSecret(buf)

    expect(`${buf}`).toBe(TEXT)
    expect(JSON.stringify(buf)).toContain('"type":"Buffer"')
    expect(JSON.stringify(buf)).not.toBe(JSON_BODY)

    expect(redactText(`blob=${buf}`)).toBe(`blob=${REDACTED}`)
    expect(redactText(`insert(k, ${JSON.stringify(buf)})`)).toBe(`insert(k, ${REDACTED})`)
    expect(redactText(`stream.emit ${buf}`)).toBe(`stream.emit ${REDACTED}`)
  })

  it('leaves an ordinary byte payload alone', () => {
    // The false-positive control: the faces are derived from a REGISTERED
    // value, never from a pattern, so an unregistered payload survives
    // byte-for-byte — including one whose joined form is all short numbers.
    markSecret(PAYLOAD)

    const ordinary = new Uint8Array([1, 2, 3, 4])
    const line = `stream.emit ${ordinary}`
    expect(redactText(line)).toBe(line)

    const other = new TextEncoder().encode('not-the-canary')
    expect(redactText(`blob=${new TextDecoder().decode(other)}`)).toBe(
      `blob=${new TextDecoder().decode(other)}`,
    )
  })

  it('leaves the trace byte-identical when nothing is registered', () => {
    // A secret-free composition pays nothing: the registry is empty, so the
    // funnel returns its input untouched rather than normalising it.
    const line = `stream.emit ${PAYLOAD}`
    expect(redactText(line)).toBe(line)
  })
})
