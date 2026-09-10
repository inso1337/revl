// A declared `Secret[T]` whose VALUE contains a quote, a backslash or a control
// character must be scrubbed like any other (roadmap item 421 F5/F6, the
// typescript half; issue #815 item 3 — "no escaped-secret canary").
//
// The registry matches EXACTLY, against host text that has already been
// RENDERED. That is what keeps ordinary trace byte-identical, but it means the
// needle has to be the bytes the renderer wrote, not the bytes the caller held.
// A value holding `"` comes back from `JSON.stringify` as `\"`, one holding `'`
// comes back from an `inspect`-shaped rendering as `\'`, and a value holding a
// tab comes back as `\t` from both — so the raw needle matched nothing at any
// sink, and a password with a quote in it crossed the host trace, the WAL, the
// seam reply and the operator console verbatim while the same password without
// the quote was redacted. The gap was total for such a value, not partial.
//
// `renderings()` registers every face, and this suite pins all three because
// the py tier registers the same three (`confidential._renderings`): a polyglot
// composition has to redact the same bytes whichever tier wrote the line, and a
// tier that scrubbed fewer faces would leak exactly the values the other tier
// covered.
//
// Every assertion is PAIRED — the canary absent AND the placeholder present —
// so none can pass on an empty string; and the last test is the other half of
// the claim: an ordinary value that merely LOOKS escaped is still recorded
// verbatim, so a diagnostic stays worth reading.
import { afterEach, describe, expect, it } from 'vitest'

import { forgetSecrets, markSecret, redactText } from '../runtime.ts'

const REDACTED = '<redacted:secret>'

// Quote, backslash, tab, newline: one value that exercises every escape an
// encoder above rewrites, so a single registration has to cover all of them.
const ESCAPED = 'CAN"ARY\\815\tTAB\nLINE'

afterEach(() => {
  // The registry is process-wide by design, so each test starts and ends empty;
  // otherwise one test's canary would redact another's.
  forgetSecrets()
})

describe('a registered secret in an escaped rendering', () => {
  /** The body an `inspect`-shaped renderer writes inside a `'...'` literal:
   *  the js string escapes, which is what a printed record or an
   *  `util.inspect` line actually contains. Written out here rather than
   *  imported, so the suite states the bytes it expects. */
  const singleQuoted = (value: string): string =>
    value
      .replace(/\\/g, '\\\\')
      .replace(/'/g, "\\'")
      .replace(/\n/g, '\\n')
      .replace(/\t/g, '\\t')

  it('is scrubbed from every face an encoder can write', () => {
    markSecret(ESCAPED)

    // The renders a sink actually produces: the raw interpolation, a JS string
    // literal (what a `JSON.stringify` of a container puts in the WAL and on
    // the seam wire), and an `inspect`-shaped one (what the console prints).
    const json = JSON.stringify({ api_key: ESCAPED })
    const sinks: Record<string, string> = {
      raw: `api_key=${ESCAPED} @ pg://main`,
      json,
      inspected: `{ api_key: '${singleQuoted(ESCAPED)}' }`,
    }

    for (const [name, text] of Object.entries(sinks)) {
      const out = redactText(text)
      expect(out, `${name} still carried the value`).not.toContain(ESCAPED)
      expect(out, `${name} was not redacted at all`).toContain(REDACTED)
    }

    // The raw face is the one a plain canary always had; the two escaped faces
    // are the ones this file exists for, and they are the encoders' own output.
    expect(redactText(json)).toBe('{"api_key":"' + REDACTED + '"}')
    expect(redactText(`api_key=${ESCAPED} @ pg://main`)).toBe(`api_key=${REDACTED} @ pg://main`)
  })

  it('scrubs an escaped face of a value whose RAW form would not match', () => {
    // The leak, stated as its own assertion: the text below contains the value
    // ONLY in a form the raw registration cannot match, so it fails if the
    // escaped faces are ever dropped from the registry.
    markSecret('CAN"ARY-815')
    const json = JSON.stringify({ api_key: 'CAN"ARY-815' })

    expect(json).toContain('CAN\\"ARY-815') // the escaped bytes, not the raw ones
    expect(json).not.toContain('CAN"ARY-815')
    expect(redactText(json)).toBe('{"api_key":"' + REDACTED + '"}')
  })

  it('keeps the sentence around the redaction (the diagnostic stays readable)', () => {
    markSecret(ESCAPED)
    expect(redactText(`KeyError: '${singleQuoted(ESCAPED)}' @ pg://main`)).toBe(
      "KeyError: '" + REDACTED + "' @ pg://main",
    )
  })

  it('leaves an ordinary value that merely LOOKS escaped alone', () => {
    // The false-positive control: the extra faces are derived from a REGISTERED
    // value, never from a pattern, so an unregistered escaped string survives
    // byte-for-byte — including the escape sequences themselves.
    markSecret(ESCAPED)
    const ordinary = JSON.stringify({ note: 'a\\"b\\tplain' })
    expect(redactText(ordinary)).toBe(ordinary)
    expect(ordinary).toContain('\\"')

    const inspected = "{ note: 'a\\'b plain' }"
    expect(redactText(inspected)).toBe(inspected)
  })

  it('leaves the trace byte-identical when nothing is registered', () => {
    // A secret-free composition pays nothing: the registry is empty, so the
    // funnel returns its input untouched rather than normalising it.
    const text = JSON.stringify({ api_key: ESCAPED })
    expect(redactText(text)).toBe(text)
  })
})
