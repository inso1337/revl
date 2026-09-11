# 456 — first-class HTTP contracts

**Roadmap:** item 456 (driven by 462, docs/design/525-exemplary-web-app.md) · **Issue:** #719 · **Status:** SLICE 1 LANDED · **Vocabulary:** aligned to Cordis (`@cordisjs/server`, `@cordisjs/http`)

## Ecosystem anchor: reuse Cordis's vocabulary, don't invent a dialect

revl runs inside the Cordis ecosystem, so the HTTP contract deliberately mirrors
Cordis's HTTP shapes where they overlap rather than deriving a parallel one from
first principles. Two Cordis packages are the anchors:

- **`@cordisjs/server`** — "HTTP/WebSocket server for Cordis", and the closer
  reference for item 456 (a server routing requests to handlers). A handler sees
  a `Request` and a `Response` (`packages/core/src/body.ts`):
  - `Request`: `url: string`, `method: string`, `path: string`,
    `query: URLSearchParams`, `headers: Headers` (a WHATWG multimap), plus body
    readers `json()/text()/bytes()/…` (body.ts:26-88).
  - `Response`: `status` (get/set over node's `statusCode`, body.ts:115-122),
    `statusText` (over `statusMessage`, body.ts:124-130), `ok`
    (body.ts:132), `headers: Headers` (body.ts:106), `body` (get/set,
    body.ts:140-152), and body writers `json(x)/text(x)/…` where `json()` sets
    `content-type: application/json` (body.ts:181-190).
  - Method set: `Server.methods = ['all', 'get', 'delete', 'head', 'post',
    'put', 'patch']` plus a separate `ctx.server.ws(...)` route
    (`packages/core/src/index.ts:125`, `:314`). A route is a middleware
    `(req, res, next) => Promise<Response | void>` (index.ts:98).
- **`@cordisjs/http`** — the client fetch SERVICE, orthogonal to the server but
  the same vocabulary (`packages/core/src/index.ts`):
  - `Http.Response<T>` = `{ url: string, data: T, status: number,
    statusText: string, headers: Headers }` (index.ts:135-141).
  - `Http.RequestConfig` = `{ method?, params?, data?, … }` over
    `{ baseUrl?, headers?, timeout?, proxyAgent? }` (index.ts:115-133).
  - Methods `get/post/put/patch/delete/head` and `ws` (index.ts:164-172, :449,
    :454); `Http.Error` with code `'TIMEOUT' | 'STATUS_ERROR'` (index.ts:153-157).

### What revl aligned to

- **`Response.status_text: Str`** — added, mirroring Cordis's `statusText`
  (server `res.statusText` / node `statusMessage`, client `Response.statusText`).
  The numeric `status` stays `Int`, exactly as Cordis carries both.
- **`Method` variant** (`Get | Post | Put | Patch | Delete | Head`) — models
  Cordis's `Server.methods` set (minus the `all` wildcard) as a type instead of a
  free `Str`, so a route table matches it exhaustively. `Request.method` is now
  `Method`. `method_str`/`parse_method` map to and from the wire spelling.
- **Headers as `List[Header]`** — kept, as the closest revl match to Cordis's
  WHATWG `Headers` multimap: ordered and repeatable (`Set-Cookie`, `Accept`),
  which a `Map[Str, Str]` cannot represent. Names are normalised to lower-case
  before storing, as WHATWG `Headers` does.

### Where revl deliberately DIVERGES (named, not oversight)

- **`Outcome = Handled(Response) | NotHandled`** — Cordis has no typed "did a
  route handle this". A handler mutates `res` (setting `res.status`/`res.body`
  flips an internal `res.claimed` flag) and the server FALLS BACK to
  `res.status = 404`/`405` by convention when nothing claimed it
  (`packages/core/src/index.ts:202-223`). That convention — status recovered from
  a mutation side effect — is the untyped, refactor-fragile pattern item 456
  exists to replace, so revl keeps `Outcome` as its core contribution.
- **Typed `Body = Empty | Text(Str) | Json(Str)`** — Cordis's body slot is
  untyped (`res.body: BodyInit`, `Http.Response.data: T`). revl keeps the body's
  KIND in the type, so emptiness is the `Empty` variant rather than a
  `""`/`"::empty::"` sentinel. This is revl's value-add over an untyped `data`;
  the field NAME still corresponds to Cordis's `res.body`/`data`.

**Roadmap:** item 456 (driven by 462, docs/design/525-exemplary-web-app.md) · **Issue:** #719 · **Status (original slice-1 note below):** SLICE 1 LANDED

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

- `pub type Method = Get | Post | Put | Patch | Delete | Head` — the Cordis
  method set as a type, not a free `Str`.
- `pub type Header = { name: Str, value: Str }` — a `List[Header]` mirrors
  Cordis's WHATWG `Headers` multimap.
- `pub type Body = Empty | Text(Str) | Json(Str)` — emptiness is the `Empty`
  variant, the typed replacement for `""` / `"::empty::"`.
- `pub type Request = { method: Method, path, headers: List[Header], body: Body }`
- `pub type Response = { status: Int, status_text: Str, headers: List[Header],
  body: Body }` — `status_text` mirrors Cordis's `statusText`.
- `pub type Outcome = Handled(Response) | NotHandled` — a mishandled route is a
  typed `NotHandled`, never an empty string.
- constructors: `header`, `request`, `response`, `ok_text`, `ok_json`,
  `not_found`, `handled`, `not_handled` (the `ok_*`/`not_found` helpers carry the
  standard reason phrases Cordis/node use)
- readers (status/outcome read THROUGH the type, never from prose): `is_handled`,
  `status_of -> Opt[Int]`, `status_text_of -> Opt[Str]`,
  `response_of -> Opt[Response]`, `is_ok` (mirrors Cordis `Response.ok`),
  `is_empty_body`, `body_text`, `header_value -> Opt[Str]` (field names compare
  case-insensitively, RFC 9110 5.1, through `header_name_eq`, so a reader finds a
  header whatever case the request head stored it in),
  `method_str`/`parse_method` (method <-> wire string)

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
2. **WebSocket + `all`.** Cordis has a separate `ctx.server.ws(...)` route kind
   and an `all` method wildcard; neither is modelled here yet (a websocket
   surface would be its own contract). `Method` covers the six request verbs.
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
