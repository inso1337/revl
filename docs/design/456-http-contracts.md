# 456 — first-class HTTP contracts

**Roadmap:** item 456 (driven by 462, docs/design/525-exemplary-web-app.md) · **Issue:** #719 · **Status:** SLICE 1 LANDED

## The problem

Routing passes bare `Str` and reconstructs status and outcome from body prose.
The harness distinguishes unhandled routes, empty responses, errors and
application output through conventions carried inside a string that cannot
express them:

- `""` means "nothing / not handled";
- `"::empty::"` means "handled, but no body";
- the status code is parsed back out of the text.

The meaning lives in a convention no compiler can check, and any refactor can
silently break it.

## The decision: a library, not a language feature

`Request`/`Response`/`Header` are ordinary records and `Body`/`Outcome` are
ordinary variant types. The revl language already has records, variant types
(`type T = A(X) | B`), `match`, and `Opt` — the exact surface an HTTP contract
needs — so **first-class HTTP contracts are a standard-library module, not new
language syntax.** This matches 525's non-goal ("where a capability belongs in a
standard web library rather than the language, say so") and means the item ships
without inventing an unreviewed language surface.

The contract lives in `stdlib/http.rvl` and is pure revl (no `@py` externs), so
it compiles and runs identically on every backend the moment that backend emits
variants and the base builtins — no per-tier work, exactly like
`stdlib/render.rvl`.

## Shipped (slice 1)

`stdlib/http.rvl`:

- `pub type Header = { name: Str, value: Str }`
- `pub type Body = Empty | Text(Str) | Json(Str)` — emptiness is the `Empty`
  variant, the typed replacement for `""` / `"::empty::"`.
- `pub type Request = { method, path, headers: List[Header], body: Body }`
- `pub type Response = { status: Int, headers: List[Header], body: Body }`
- `pub type Outcome = Handled(Response) | NotHandled` — a mishandled route is a
  typed `NotHandled`, never an empty string.
- constructors: `header`, `request`, `response`, `ok_text`, `ok_json`,
  `not_found`, `handled`, `not_handled`
- readers (status/outcome read THROUGH the type, never from prose): `is_handled`,
  `status_of -> Opt[Int]`, `response_of -> Opt[Response]`, `is_empty_body`,
  `body_text`, `header_value -> Opt[Str]`

`tests/test_http_stdlib.py` pins the exit criterion on the py tier: a
route-shaped consumer maps a `Request` to an `Outcome` and reads status,
handled-ness, body and emptiness through the contract with **zero**
`""`/`"::empty::"` sentinels on any code line; an unrouted path is `NotHandled`
(status `None`), and a handled 404 with an `Empty` body is distinguished from a
handled text route.

## Remaining scope (open, not in this slice)

1. **Wire binding.** `revl serve --http` (`src/revl/mcp/http_face.py`) speaks its
   own request/response shape today. Threading this typed contract through the
   serve face, or a serialization pair (`Body`/`Response` <-> the canonical wire
   encoding), is a separate slice.
2. **Method as a type.** `method` is `Str`; a `Method` variant (`Get`/`Post`/…)
   is a possible follow-up if route tables want exhaustiveness on it.
3. **Binary bodies.** `Body` has no `Bytes` case yet (revl `Bytes` exists);
   add when the exemplary app (462) needs a non-text payload.
4. **Adoption in 462.** The exemplary app must actually route through this
   contract to retire the harness's real sentinels — the forcing function that
   proves the surface is right. Any friction found there is a gap filed back
   against this item, per 525's sequencing.

## Why this is the right first cut

It is the smallest change that makes the two things the string convention
encoded — *did a route handle this* and *what is its status/body* — readable from
the type, on existing language surface, with no emitter or tier work. The wire
binding and the exemplary-app adoption build on it rather than being blocked by a
language decision.
