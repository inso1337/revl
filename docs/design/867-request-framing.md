# 867 — canonical HTTP request framing

**Roadmap:** no item (filed as issue #867; searched `docs/v2.0-roadmap.md` for
smuggling / `Content-Length` / `Transfer-Encoding` / framing and found no
matching item, so nothing here renumbers or claims one) · **Issue:** #867 ·
**Status:** LANDED · **Module:** `stdlib/framing.rvl` · **Kin:** 456
(`stdlib/http.rvl`, `docs/design/456-http-contracts.md`), `stdlib/fs.rvl`'s
`resolve_within`

## The problem: N readers, N framings, one byte-identical request

revl gives a component a path-confinement primitive (`stdlib.resolve_within`)
precisely so that nobody hand-rolls containment. It gave components no
equivalent for HTTP request framing, so an HTTP server written as a revl
component parsed `Content-Length` itself, in four places, and the four readers
disagreed about the same bytes.

The measured before-state, from the motivating server (a component in the
`inso1337/revl-harness` repository, `src/components/transport.rvl`): a
byte-identical request head carrying `Content-Length: 5` and then
`Content-Length: 7` got **two different answers on the same server**.

| reader | answer to the doubled field | consequence |
|---|---|---|
| A | `400`, close | correct (RFC 7230 3.3.3) |
| B | `400`, close | correct |
| C | honour the first value (`5`), body ends early, the 2 leftover bytes are parsed as the start of the next pipelined request | `['400 Bad Request', '200 OK']` |
| D | honour the first value, leftover parsed as the next request | `['404 Not Found', '200 OK']` |

C and D are the smuggling shape: the reader that frames the body and the reader
that decides where the next request begins are two different code paths with two
different answers, so bytes one path calls "leftover" are bytes the other path
calls "a request". The same server answered a 4300-digit `Content-Length` with
**no response at all** and unauthenticated: a validator accepted the value and
the very next statement could not convert it. As the issue's fourth comment puts
it, "a validator that accepts a value the very next statement cannot process is
not validation".

The defect is not that one of the four readers is wrong. It is that there are
four. Any fix that corrects one of them leaves the class in place, and the next
reader added to that server re-opens it.

## The decision: the `resolve_within` shape, as a stdlib module

`stdlib/fs.rvl` answers the identical question for paths: instead of every
component deciding what "inside the workspace" means, one `pub` primitive decides
it once and returns a typed refusal that names **which** rule failed
(`docs/witnessed-fs.md`; declaration at `stdlib/fs.rvl:188`, refusal record at
`stdlib/fs.rvl:142`). Request framing gets the same shape:

```revl sketch
// A `sketch` because the doc-example gate compiles snippets in memory, where a
// `use` of a real file path has nothing to resolve against; the same consumer
// shape is compiled and executed for real in tests/test_framing_stdlib.py.
use "stdlib/framing.rvl" { body_length, status_for, reason_of, message_of, is_too_large }

fn body_bytes(headers: List[Header], ceiling: Int) -> Result[Int, FramingRefusal] {
  return body_length(headers, ceiling)
}
```

- `pub fn body_length(headers: List[Header], ceiling: Int) -> Result[Int, FramingRefusal]`
  (`stdlib/framing.rvl:262`) is the decision: how many body bytes to read, or the
  rule that refuses the request.
- The refusal is a **closed variant with one case per rule**, not a `Str`, so a
  caller's `match` is exhaustive and a new rule cannot be added without every
  caller seeing it and deciding what to do about it.
- The refusal carries **which** rule failed, not just that something failed.
  `status_for` maps the case to the wire status, `reason_of` to a stable machine
  token, `message_of` to the sentence, and `is_too_large` is the O(1) 413 test.
  That is the issue's secondary ask: a caller maps framing-refused to `400` and
  over-ceiling to `413` **without re-deriving the mapping**, so two components
  cannot drift on which refusal is a 400 and which is a 413 either.

Everything here is pure revl (no `@py`, no `@ts`, no externs; the consumer IR in
`tests/test_framing_stdlib.py` asserts `externs == []`), so it is a library
decision, not a language feature, exactly as 456 argued for the HTTP contract
itself: revl already has records, variant types, `match`, `Result` and `Opt`,
which is the whole surface framing needs.

## The rules, each its own named refusal

Applied in this order. The order is part of the contract: a request that breaks
two rules reports the first, so two components agree on *which* refusal a given
header earns, not merely that it is refused.

| # | condition | case | `reason_of` | `status_for` |
|---|---|---|---|---|
| 1 | `Transfer-Encoding` present, any coding | `UnsupportedTransferEncoding` | `unsupported_transfer_encoding` | 400 |
| 2 | `Content-Length` repeated, or a comma list in one field | `DuplicateContentLength` | `duplicate_content_length` | 400 |
| 3 | value is not one plain non-negative decimal integer | `MalformedContentLength` | `malformed_content_length` | 400 |
| 4 | value above the caller's ceiling | `OverCeiling` | `over_ceiling` | 413 |
| 5 | no `Content-Length`, no `Transfer-Encoding` | `Ok(0)` | | |
| 6 | otherwise | `Ok(length)` | | |

1. **`Transfer-Encoding` is refused, not ignored.** revl's stdlib has no
   transfer-coding decoder, so a body framed by `Transfer-Encoding: chunked` has
   no length this module can hand back. Returning a `Content-Length` beside it is
   exactly how a smuggled request is built, so the presence of the field alone is
   refused (RFC 9112 6.1). `chunked` earns no exception.
2. **A repeated `Content-Length` is refused even when the values are
   byte-identical** (RFC 7230 3.3.2, RFC 9110 8.6, RFC 9112 6.3 item 5; argument
   below). A comma-separated list in ONE field (`Content-Length: 5, 5`) is the
   same defect spelled differently and earns the same case; the grammar has no
   comma (`Content-Length = 1*DIGIT`, RFC 9110 8.6).
3. **`MalformedContentLength`** covers everything the grammar excludes: an empty
   value, a sign or a leading `+`, whitespace inside the value, a non-digit, and a
   leading zero beyond the single `0` that is the value zero. Leading and trailing
   OWS is stripped first, as the grammar requires (RFC 9110 5.5); the strip is SP
   and HTAB only, NOT the wider set `stdlib/str.rvl`'s `is_space` carries, so a
   value padded with a form feed is malformed rather than trimmed. Leading zeros
   are refused even though `0000000001` is legal ABNF, because accepting them
   forces every reader to compare two spellings of one length by text.
4. **`OverCeiling` is never a `MalformedContentLength`.** The ceiling is the
   caller's policy (413 Content Too Large); a malformed value is the sender's
   fault (400). The value is bounded **before** it is converted: the digit loop
   stops as soon as the digits read exceed the ceiling, so a 4300-digit value is
   refused on its first few digits and is never handed to an integer conversion.
   There is no separate digit limit, because the bound is the ceiling, exact. And
   the bound holds at the extreme: a caller may name `Int`'s own maximum as its
   ceiling, and a value one digit past that is still refused rather than
   overflowing the conversion.

## The identical-duplicate decision, and why

RFC 7230 3.3.2 and RFC 9110 8.6 allow a recipient to either reject a repeated
same-value `Content-Length` list or replace it with the single value, and RFC
9112 6.3 item 5 keeps that narrow exception for an all-identical list. The RFC
therefore permits accepting `Content-Length: 5` twice. **This module refuses it**,
and says so out loud rather than silently:

- The divergence is the defect. If one reader accepts a doubled field and its
  sibling on the same server rejects it, the two disagree about where the body
  ends, and an intermediary between them can disagree with both. That is the
  measured smuggling shape above, not a hypothetical.
- "The values are identical" is a judgement the second reader has to make
  anyway, on values an intermediary may have rewritten. Refusing needs no
  judgement and cannot be got subtly different, which is the whole point of
  routing the decision through one module.

So `body_length(headers_with_two_identical_content_lengths, ceiling)` returns
`Err(DuplicateContentLength)`, and
`tests/test_framing_stdlib.py::test_an_identical_duplicate_content_length_is_refused_too`
pins that decision with the RFC citations in the test body.

## What a component author must now do differently

- **Do not read `Content-Length` or `Transfer-Encoding` yourself.** Ask
  `body_length(headers, ceiling)` for the number of body bytes and read exactly
  that many; anything else is the defect this module exists to remove. There is
  now one place in the whole ecosystem where the rule lives.
- **Map the refusal with `status_for`, not by hand.** `400` for the three
  framing refusals, `413` for `OverCeiling`, and `is_too_large(r)` when the
  caller only needs the 413 test. Refuse and **close the connection** (RFC 7230
  3.3.3): a refused framing means the byte stream is no longer trustworthy, so a
  pipelined request behind it must not be parsed.
- **Do not reach for `header_value` to read a length.** `stdlib/http.rvl`'s
  `header_value` answers with the first match only, which cannot see a duplicate
  at all. `header_values` / `header_count` are the duplicate-visible readers, and
  `header_values(headers, "content-length")` with `.length() > 1` is exactly the
  test a reviewer should look for when auditing a reader.
- **Vendors of the stdlib must re-vendor.** The stdlib stamp moves 5 to 6
  (`stdlib/version.rvl`), because a component that vendored the stdlib under the
  old stamp predates `body_length` and is still hand-rolling framing.
  `revl doctor`'s `stdlib version stamp` check flags the drift.

## The residue, stated plainly

- **A component that reads headers itself can still bypass the primitive.** This
  is a library, not a language feature: nothing stops a component from doing the
  string work by hand, exactly as nothing stops a component from re-implementing
  `resolve_within`. What changes is that doing so is now a visible, reviewable
  choice against a named primitive, and the drift class has a single place to be
  fixed instead of N.
- **The harness is a different repository and is out of scope here.** The four
  drifting readers are in `inso1337/revl-harness`, `src/components/transport.rvl`,
  and the change that retires them belongs there, against that repo's own review.
  This module is the primitive that change will call; this PR does not touch the
  harness, does not vendor it, and does not depend on it.
- **No chunked decoding.** `Transfer-Encoding` is refused rather than decoded. A
  caller that genuinely needs chunked support writes a decoder and does not ask
  `body_length` for that request; the module will never hand back a
  `Content-Length` for a body it cannot frame.
- **Wire binding.** Like 456's `Outcome`, this decides *framing* (how many bytes
  belong to this body) and not *parsing* (header folding, obs-fold, HTTP/2
  framing, what the request line means). A `revl serve --http` face that threads
  it through is a separate slice.

## Evidence

`tests/test_framing_stdlib.py` (34 tests) pins every rule and every positive case,
including the exact doubled `Content-Length: 5` / `Content-Length: 7` shape from
the issue, the identical-duplicate decision, the 400/413 distinguishability of
the caller's mapping, the 4300-digit value the issue's fourth comment measured,
`Int` maximum as a ceiling, the no-echo property of the refusal sentence, and a
consumer that imports `stdlib/http.rvl` alongside the module (the realistic
layout). RED-before, with `stdlib/framing.rvl` backed out and the tests kept, and
GREEN-after are recorded on the pull request.
