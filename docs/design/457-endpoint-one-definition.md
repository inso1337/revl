# 457: one endpoint definition, five derived artifacts, authorization stays a step

**Roadmap:** item 457 · **Issue:** #720 · **Drives:** 462 (docs/design/525-exemplary-web-app.md) · **Builds on:** 456 (stdlib/http.rvl, PR #745), 528/529/530 (Cordis alignment notes) · **Status:** DECISION, 2026-09-08

## The decision in one paragraph

An endpoint is a service operation with a `route` clause. The clause is the one
new piece of language surface (one contextual production, no lexer change, no
new keyword outside that position). Everything else is tooling and stdlib that
already exists or is a projection of the IR the clause lands in: routing is
`revl serve --http` honouring the clause (py) and an emitted `ctx.server`
registration (ts); request validation is item 257's schema derivation applied at
the boundary; response serialization is the canonical value encoding the four
bridges already speak; the OpenAPI document is `revl export openapi`, the
inverse of the importer that exists; the TypeScript client is `revl export
client` made route-aware. Authorization is not in the clause and cannot be put
there. It is a call the handler makes on a required `Auth` service, and the
value it returns (`Principal`) is the only way to reach user data, so a handler
that skips the call cannot be admitted, and the derived router, the OpenAPI
document and the client carry nothing that could stand in for it.

## Why a clause and not a stdlib value

Two designs were on the table. A stdlib-only design spells an endpoint as a
value, `endpoints([endpoint(Get, "/notes/{id}", get_note)])`, and derives the
five artifacts from it. It fails for a reason that has nothing to do with
taste: every derivation revl ships today is a pure projection of the IR (`revl
serve --http`, `revl export client`, `revl export wit`, the MCP face). None of
them evaluates revl. A value-level endpoint table would need either compile-time
evaluation of a list literal, which no tool has and which turns fragile the
moment the list is built by a function, or a second runtime-only registry that
the exporters cannot see. The declaration has to be structural.

A full construct (`endpoint NAME { ... }` as a new declaration kind) was the
other extreme. It duplicates the thing revl already has as its IDL: the
`service` declaration (docs/interop-bridge.md §2), which carries the typed
signature, the emission classification and the async colour every derivation
needs. An endpoint is a service operation that happens to be reachable over
HTTP, so the route belongs on the operation.

The chosen shape costs the grammar one production and reuses two precedents:
`isolate ... in realm("...")` for a clause that carries a string, and `remote` /
`through` for a contextual keyword recognised only in one position.

## The declaration

```revl sketch
use "stdlib/http.rvl" { ApiError }
use "stdlib/auth.rvl" { Auth, Bearer, Principal }

type Note    = { id: Str, owner: Str, title: Str, body: Str }
type NewNote = { title: Str, body: Str }

service NotesApi {
  route get "/notes/{id}"
  fn get_note(auth: Bearer, id: Str) -> Result[Note, ApiError]

  route get "/notes"
  fn list_notes(auth: Bearer, limit: Opt[Int]) -> Result[List[Note], ApiError]

  route post "/notes"
  emission fn create_note(auth: Bearer, note: NewNote) -> Result[Note, ApiError]

  route delete "/notes/{id}"
  emission fn delete_note(auth: Bearer, id: Str) -> Result[Unit, ApiError]
}
```

Grammar delta (docs/syntax-2.0.md §8 is amended by this line):

```
methoddecl := ['route' METHOD STRING] modifier* 'fn' IDENT '(' tparams? ')' ['->' type]
METHOD     := 'get' | 'post' | 'put' | 'patch' | 'delete' | 'head'
```

`route` is contextual: it heads a method declaration only in the shape
`route <method> "<path>"` inside a `service` body, so a program using `route` as
an ordinary name is unaffected, and the self-host lexer needs no sync. The
method set is exactly `stdlib/http.rvl`'s `Method` variant, so the route table
and the typed `Request.method` cannot disagree.

### Binding rules (checked at compile time, by parameter TYPE and NAME)

| parameter | bound from | rule |
|---|---|---|
| named in the path template `{name}` | the path segment | must be `Str`, `Int` or `Bool`; every template name must be a parameter and every such parameter must be scalar |
| of type `Bearer` (stdlib/auth.rvl) | the `Authorization` header | at most one; carries the credential as an UNTRUSTED claim, decides nothing |
| of type `Request` (stdlib/http.rvl) | the whole typed request | at most one; the escape hatch for a handler that needs headers beyond the bearer |
| remaining, on `get` / `head` / `delete` | the query string | each must be a scalar or `Opt[scalar]`; a missing `Opt` is `None` |
| remaining, on `post` / `put` / `patch` | the JSON body | exactly one, and it must be a record type; the body IS that record |

Anything else is a compile refusal naming the operation and the parameter
("`create_note` has two body candidates `note` and `tags`; give the body one
record type"). The rules are deterministic in both directions so `revl import
openapi` and `revl export openapi` are inverses on the subset both express.

The `Bearer` row's "at most one" is the arity of the *parameter*, not a claim
about the wire: a request may present more than one `Authorization` field.
When it does, `http_face` makes no claim at all rather than picking one — the
same answer a missing credential gets, so the handler's `validate` denies and
the router still grants nothing. Picking the first field instead would let
`auth.token` and the `Request` escape hatch, which carries every header,
disagree about what one request presented.

### Return rules

| declared return | on the wire |
|---|---|
| `T` | `200`, body `T` in the canonical encoding; `Unit` is `204` with an empty body |
| `Result[T, ApiError]` | `Ok(t)` as above; `Err(e)` is `e.status` with body `{"code": e.code, "message": e.message}` |
| `Response` (stdlib/http.rvl) | sent as-is; the handler owns status, headers and body |
| anything else in `Err` | compile refusal: "an error a client can act on needs a status; return `Result[T, ApiError]` or a `Response`" |

`stdlib/http.rvl` gains `pub type ApiError = { status: Int, code: Str, message:
Str }` and the constructors `bad_request`, `unauthorized`, `forbidden`,
`not_found_error`, `conflict`. Streaming and server-sent events are not routed
operations; they are 456's remaining scope (2) and get their own contract.

## The five derived artifacts, exactly

1. **Routing.** The IR gains `services[S].ops[op].route = {"method": "get",
   "path": "/notes/{id}", "bind": {...}}`, the bind table above resolved per
   parameter. `revl serve --http` matches routed operations by method and
   template before falling back to today's `POST /<composition>/<key>/<op>` for
   unrouted ones; nothing changes for a service with no `route` clause. On the
   ts tier the emitter registers each routed operation on the Cordis server
   (`ctx.server.get(path, handler)`, docs/design/456-http-contracts.md) through
   the `host @server` row below; the handler decodes into the typed `Request`,
   binds parameters, calls the provide method and encodes the `Outcome`. A
   request no route matches is the typed `NotHandled` and the server answers
   `404`; a matched path with the wrong method is `405`.
2. **Request validation.** For every bound parameter the compiler derives a JSON
   Schema with item 257's `fully_expressible` predicate and `json_schema_for`
   (`src/revl/mcp/schema.py`). A type the predicate refuses (an untagged
   `Result`, a `Map` with a non-`Str` key, a cyclic type) is a compile refusal
   naming the operation, never a vacuous schema. At runtime the body, query and
   path values are validated against the derived schema BEFORE the provide
   method runs; a failure is `400` with an `ApiError` whose `message` names the
   field, and the handler is never invoked. Validation is about shape, not
   trust: every routed input is an `Untrusted` source, the inbound twin of
   D-424c.9, so a request value reaching a G9 sink (a command, an authority
   selection, an instruction channel) is refused without an `endorse`. The
   static lattice's origin class is `input` — the class `_origin_of` gives an
   inbound crossing that declares no capability scope, which is what a routed
   request is; the compiler is declaring `Untrusted[...]` on the author's
   behalf, keyed by the operation name so the provide method implementing it
   sees the origin in its own body. Ordinary CRUD is not a sink, so the
   exemplary app pays nothing for this.
3. **Response serialization.** The return rules above over the canonical
   encoding (docs/interop-bridge.md, "Canonical value encoding"): records as
   objects, `Opt` as the bare value or `null`, ADTs and `Result` payloads as
   `{"$kind", "$value"}`. This is the encoding `revl serve --http` and `revl
   export client` already share, so the client's TypeScript types ARE the wire.
4. **OpenAPI.** `revl export openapi FILES --service NAME [-o spec.json]`
   renders an OpenAPI 3.1 document: one path item per routed operation, the bind
   table as `parameters` (path/query) and `requestBody`, the derived schemas
   under `components/schemas`, `200`/`204` and the `ApiError` responses, and the
   compiler's `x-revl-emission` hint. The document carries NO `securitySchemes`
   and no `security` requirement derived from anything, because there is nothing
   in the declaration to derive one from. An `Auth`-bearing operation gets a
   documentation-only `x-revl-auth: bearer` marker whose text says the check
   happens in the handler. `revl import openapi` of the exported document
   round-trips the service on the subset both express (pinned by test).
5. **TypeScript client.** `revl export client --lang ts` reads the same `route`
   entry: a routed operation becomes a typed method that builds the path from
   its scalar arguments, puts the remaining arguments in the query or the JSON
   body per the bind table, sends a `Bearer` as the `Authorization` header when
   the operation declares one, and decodes the response into the same
   canonical-encoding types the client already emits. Unrouted operations keep
   the canonical `POST`. The client carries the gate frontier and no safety
   claim, exactly as today (D-424c.7/.8).

## Authorization: explicit, and not derivable

The requirement is that generated metadata must never grant access, and that
removing the explicit auth step denies the request. Both are met structurally,
not by convention.

`stdlib/auth.rvl` declares, and it is the only module that may: the same
declaration written anywhere else is a second door to user data, so the
compiler refuses it.

```revl reject G4
pub type Bearer = { token: Opt[Str] }          // the credential as presented; a claim

service Auth {
  fn validate(b: Bearer) -> Result[Principal, ApiError]   // the only producer of Principal
}
```

`Principal` is an opaque host type: it is the return type of an extern in the
auth provider and has no revl declaration, so no revl body can construct one
(no literal, no record syntax), and a turn admitted under the untrusted-author
profile (item 329) cannot declare a new extern that would mint one. The
exemplary app's provider binds `Auth.validate` to Cordis SSO's
`validateSession` (docs/design/529-sso-auth-alignment.md) on the ts tier and to
a stub on py for tests.

A user-scoped store takes the principal:

```revl sketch
service NoteStore {
  fn get(who: Principal, id: Str) -> Result[Note, ApiError]
  emission fn create(who: Principal, note: NewNote) -> Result[Note, ApiError]
}

component NotesHttp requires auth: Auth, store: NoteStore provides notes_api: NotesApi {
  provide notes_api {
    fn get_note(bearer, id) {
      return match auth.validate(bearer) {       // the explicit authorization step
        Err(e) => Err(e),                          // 401 or 403, from the auth service
        Ok(who) => store.get(who, id),
      }
    }
    ...
  }
}
```

Three consequences, each checkable:

- **The router grants nothing.** The derived router binds `Bearer` from the
  header and passes it through untouched. It has no auth vocabulary: no
  `auth` keyword on the route, no `security` in the export, no token logic in
  the client. Deleting the router (serving the operation over the MCP face
  instead) changes nothing about who may read a note.
- **Removing the step is refused at admission.** A handler that drops
  `auth.validate` has no `Principal` in scope and cannot call `store.get`;
  the refusal names the missing producer. This is G1 read at the value level:
  the only path to user data runs through a value only the auth service
  produces. It is stronger than a runtime denial and it is the property the
  roadmap's exit test is really after.
- **A failed step denies at runtime.** With the step present, a missing or
  invalid credential is the `Err` from `validate`, rendered as `401`; a valid
  credential for the wrong resource is the store's `403`. The per-resource
  decision over runtime values (who may see note `42`) is the runtime
  capability service's job (docs/design/528-capability-reach-alignment.md); it
  composes with this design and is not derived either.

What is deliberately NOT provided: a way to mark an operation "public" in the
clause. An operation with no `Bearer` parameter is public because its handler
reaches no principal-taking service, which the checker can see; a marker would
be a second, unchecked way to say the same thing.

## The Cordis `ctx.server` binding: a `host` row

Design note 530 asked how a revl component reaches an ambient, host-provided
service such as `ctx.server`, `ctx.sso` or `ctx.webui` without a `globalThis`
bridge. The answer is the row table, not a new checker mode. A `host` row is the
sibling of a `remote` row: a row whose provider is not revl source but the host
runtime's own service, reached through the reviewed `@ts ref` door:

```revl
composition Notes {
  use "notes.rvl"
  host   @server provides server: Server        // Cordis ctx.server, ts tier
  host   @auth   provides auth: Auth            // Cordis ctx.sso, ts tier
  row    @store  from "store.rvl" provides store
  row    @api    from "http.rvl"  provides notes_api
}
```

The row claims the `(key, realm)` like any other, so G2 holds; the consumer
writes `requires auth: Auth` and does not change between the host provider and
a revl one (the same rule as `remote`: hostness is an admission fact). The
resolver synthesizes the provider from the service declaration and a stdlib
binding module (`stdlib/server.rvl`, `stdlib/auth.rvl`) whose externs are `@ts
ref ... from` the shipped host shims. A `host` row on a tier that has no shim
for that service is refused at resolution naming the tier. This answers 530's
Decision A in favour of option 1's semantics (a declared coeffect) by way of
the row table rather than a checker special case; 530's Decision B (the webui
reactive-state/RPC contract) is folded into this item as a second projection of
a service declaration, `revl export client --lang ts --face webui`, in a later
slice.

## What is in scope for the first slice, and what waits

| slice | delivers | tier | depends on |
|---|---|---|---|
| **S1** | `route` clause: parse, bind-table check, IR entry; `revl serve --http` honours routes, validates, maps `ApiError`; `revl export openapi`; `revl export client` route-aware; `ApiError` in stdlib/http.rvl | py + tooling | PR #745 merged; 257's schema predicate as landed in `src/revl/mcp/schema.py` |
| **S2** | `stdlib/auth.rvl`, opaque `Principal`, the admission refusal for a missing step, the exemplary app's routed half | py | S1 |
| **S3** | `host` rows; emitted `ctx.server` registration; `stdlib/server.rvl` + `stdlib/auth.rvl` ts shims over `@cordisjs/server` and `@cordisjs/plugin-sso` | ts | S1, 426 S1 row table (landed), 530's host-ref door (landed) |
| **S4** | webui face projection (530 Decision B) | ts | S3, 459 |

S1 is landable alone and proves four of the five artifacts on the reference
tier; S2 proves the authorization property; S3 is where the exemplary app's
frontend meets it.

## Exit test (item 457, app-shaped)

One declaration, `NotesApi` above, in the exemplary app (462):

1. `revl serve --http` answers `GET /notes/42` with the note, `GET /notes?limit=x`
   with `400` naming `limit`, `POST /notes` with a body missing `title` with
   `400` naming `title` and the handler never invoked, `PUT /notes` with `405`,
   `GET /nothing` with `404`.
2. `revl export openapi` produces a document that `revl import openapi`
   round-trips to the same service signature, that carries no `security`
   requirement, and that a third-party validator accepts as OpenAPI 3.1.
3. `revl export client --lang ts` compiles under `tsc --strict` and, pointed at
   the face in (1), round-trips a `Note`, an `Opt[Int]` query and a
   `Result[Note, ApiError]` byte-identically to the placement bridge encoding.
4. The mutant that removes `auth.validate` from `get_note` is REFUSED at
   admission with a diagnostic naming `Principal` and its sole producer; with
   the step present, no `Authorization` header is `401`, an invalid token is
   `401`, a valid token is `200`.
5. Zero `""` / `"::empty::"` sentinels and zero emitter workarounds in the app's
   routed half (456/458), counted by the same line scan
   `tests/test_http_stdlib.py` already uses.

## What this does not decide

- Server-sent events and websockets (456 scope (2)): their own contract.
- The per-resource capability check over runtime values: item 528's runtime
  service, explicit in the handler, not derived.
- Binary bodies (`Body` has no `Bytes` case yet): add with the first
  non-text payload the app needs (456 scope (3)).
- A route on a `provides`-side stream (130): out of scope until 130's required
  stream capability lands.
