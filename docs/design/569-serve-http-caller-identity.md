# 569: Who calls `revl serve --http`: operators or the application's users

Status: Accepted: B + C2. Decided by the owner in issue #1553: callers of
`revl serve --http` are the application's users (option B), and operators use a
separate operator-only listener on the same session (option C2). B1 landed in
PR #1505; B2, B3 and C2 landed for issue #1553. Section 8 records what was built
and where it differs from the proposal below, which is kept as it was argued.

Source read: `src/revl/mcp/http_face.py` (item 424 gap (c), item 457 routes),
`src/revl/mcp/composed.py`, `src/revl/mcp/schema.py`, `src/revl/mcp/session.py`,
`src/revl/mcp/operator.py`, `stdlib/auth.rvl`, `stdlib/admit.rvl`,
`src/revl/mcp/admit_bridge.py`, `backends/python/runtime.py`, at main
`54a0aecb`, and the open MCP HTTP transport stack: PR #1486 (`feat/1463-mcp-http`)
and PR #1491 (`feat/1488-http-lock-revocation`, head `343cc7cb`), which add
`src/revl/mcp/http_guard.py`, `src/revl/mcp/http_transport.py`,
`src/revl/mcp/live_profile.py` and `docs/mcp-http-transport.md`.

## Recommendation

**Option B: callers of `revl serve --http` are the application's users.** The
server authenticates nobody. It guards Host, Origin, TLS and the dispatch lock
(`http_guard`), and the composition authenticates its own users through
`stdlib/auth.rvl`. Operators never use this listener; they reach the same session
through a second, operator-only listener (Option C2 below) or out of band (the
E-Stop latch).

B is only sound once the face stops serving operations the program never meant
to be public. Today it serves **every provided key of every component**. That
makes two things reachable by an anonymous caller, both reproduced below:

1. a user-scoped store that takes a `Principal` can be called directly with a
   `Principal` written into the request body, so the "only `Auth.validate` mints
   a `Principal`" guarantee does not hold on the wire;
2. a composition that composes `stdlib/admit.rvl` exposes `admission.admit`,
   so an anonymous caller admits new code into the running composition with a
   `granted` list of its own choosing.

Slice B1 (below) closes both and is the precondition for everything else. It is
needed under every option, because Option A does not close them either: it only
narrows who can exploit them to "anyone holding an operator credential", and
under A every application user holds one.

## 1. What the face is

`revl serve --http FILES` boots one composition (`serve_http`,
`http_face.py:1094`) and puts its provided operations on HTTP. It is the only
revl surface a program's end users reach: a browser, a mobile client, or the
TypeScript client `revl export client` generates. The compiler surface
(`revl_swap`, `revl_load`, `revl_approve` and the rest,
`docs/operator-capabilities.md`) is a different process, `revl mcp serve`, and
is not served here.

What the face routes today (`HttpComposedServer`, `http_face.py:315`):

| Route | Handler | What it reaches |
| --- | --- | --- |
| `GET /` | `_manifest` | the operation list, the route table, the gate frontier |
| `POST /<composition>/<key>/<op>` | `dispatch` (`:424`) | `Session.call(key, op, args)` for every op `tools_from_ir` projects (`schema.py:331`): **every provided key of every component**, internal ones included |
| a declared `route <method> "<path>"` | `_serve_route` (`:515`) | `Session.call` on the routed op, with path, query, body, `Bearer` and `Request` bound per the item-457 bind table |
| (a class-(c) crossing under an approval policy) | the `ApprovalRequired` arms (`:458`, `:589`) | a 403 carrying the ticket; there is no approve verb on this wire |

There is no route to `swap`, `load`, `unload`, `edit`, `restore`, `commit`,
`abort`, `approve`, `lease`, `estop` or any other operator verb. `Session.call`
is the only session method the face invokes. So no management-plane verb is
reachable **by name**. Two are reachable **through a provided operation**, which
is the finding in section 3.

The session the face runs is ungated in every sense the management plane
defines: `Session.operator` is `None` (`session.py:620`, "every verb is
ungated"), `approval_policy` is `None` (`session.py:645`, "every call proceeds"),
the lease book is empty, and `revl serve` accepts neither `--operator-profile`
nor `--approval-policy` (`cli/parser.py`, the `serve` parser, on main and on the
#1491 head). The 403 ticket path in the face is therefore unreachable today:
with no policy loaded, `_approval_decide_call` returns before it can raise
(`session.py:5507`), and a class-(c) crossing fires without a human yes.

Which operations each option gates:

| Operation | A: operators | B: application users | C2: B plus an operator listener |
| --- | --- | --- | --- |
| `GET /` manifest | operator credential | public (Host/Origin only) | public |
| routed op with a `Bearer` param | operator credential, then the handler's `auth.validate` | the handler's `auth.validate` | same as B |
| routed op without `Bearer` | operator credential | public, by the author's choice | same as B |
| canonical `POST /<c>/<key>/<op>` | operator credential + `may call on <component>` | only the ops B1 leaves exposed | same as B |
| class-(c) crossing | ticket; the caller is the proposer | ticket relayed to an operator | ticket approved on the operator listener |
| `approve`, `estop`, `state` | would need new routes on the face | not on the face | operator listener only |

## 2. Option A: callers are operators

Every request authenticates as one operator of an operator profile, the model
PRs #1486/#1491 built for `revl mcp serve --http`: `Authenticator.authenticate`
(`http_transport.py:142`) resolves a bearer secret against the profile's
`key sha256:` lines or an mTLS commonName against the tokens, and
`CallerBinding.as_caller` (`:227`) binds `session.operator` for exactly one
request under the dispatch lock, with the `NO_CALLER` placeholder (`:58`)
between requests.

**Coexisting with item 457's untrusted `Authorization`.** The face binds the
`Authorization` header, untouched, into any routed parameter of type `Bearer`
(`http_face.py:573`, `_bearer_token` at `:286`), as the credential the
application validates. A-with-bearer consumes the same header as the operator
secret. That hands the operator's secret to application code as a claim, and the
operator secret is also that operator's **vote credential**
(`docs/mcp-http-transport.md`, "Identity": "one secret authenticates the operator
to the transport and proves its votes"). With the shipped py stub,
`host_validate` (`stdlib/auth.rvl`) turns any token into `{"subject": token}`, so
the operator secret would become the application user's subject name. A-with-
bearer is therefore not viable. A would have to use:

* **mTLS** (`--auth mtls`): the certificate is the operator, `Authorization`
  stays the application's. The transport refuses a request carrying both
  (`authenticate`, mtls branch); the face would have to drop that rule, because
  here both are expected. Browsers attach client certificates ambiently, so any
  page on an `--allow-origin` origin makes requests as the user's operator
  without the page knowing a credential. That is a CSRF shape the bearer design
  never had.
* or **a separate header** (`Revl-Operator: Bearer <secret>`): workable, but it
  is a second credential every client must carry, and `revl export client`
  would have to learn it.

**What an application user would hold.** An operator entry of their own in the
profile: `operator <user> key sha256:<digest>` plus `operator <user> may call on
<component>`. The profile is a hand-maintained file re-read on `stat` with a
settle window (`live_profile.py`); it is not a user database. Signup, password
reset and session expiry have nowhere to live.

**Consequences for the management plane.**

* **The `call` gate is not wired on the face.** `operator.decide`
  (`operator.py:1395`) is invoked only from the MCP verb dispatch
  (`server.py:3730`); `Session.call` does not consult the operator. A needs the
  face to call `decide(session, "revl_call", {"key": key})` per request.
* **Leases** (item 61, #1450) fence swap, unload, restore, commit confirm and
  abort. None of those is on the face, so leases would mean nothing here, yet
  every app user would appear in the lease book's vocabulary of holders.
* **Approval** (items 246, 471): the proposer of a ticket is the calling
  operator. Separation of duties needs `approve` held by nobody who holds
  `call`. Every app user holds `call`, so every approver must be kept out of the
  user population by profile discipline alone.
* **Quorum** (item 471): a cast is bound to the request's operator (`boundBy:
  "transport"`, `quorum.py` on #1486). If app users are operators, a quorum is a
  count of app accounts, and an attacker who registers N accounts is N voters.
* **E-Stop** would become an HTTP route on the application's own listener,
  reachable from the public network by anyone with `estop`.

A does not fix section 3: an authenticated operator can still forge a
`Principal` or call `admission.admit`, and under A that is every user.

## 3. Option B: callers are application users

The server authenticates nobody and guards only what `http_guard` guards: Host
(DNS rebinding), Origin (`--allow-origin`), TLS off loopback, and one dispatch
lock (#1488). The composition decides who its users are: a routed handler takes
a `Bearer` and runs `auth.validate`, and only a `Principal` reaches user data
(`docs/design/457-endpoint-one-definition.md`, "Authorization";
`tests/test_auth_admission_457.py`).

The brief's test for B was: no operator-class verb is reachable from the face.
**It fails today, in two ways.**

### Finding 1: a `Principal` can be written into a request body

`dispatch` decodes the canonical body with `_decode_args` (`http_face.py:700`),
which checks only that it is a JSON array no longer than the parameter list. No
per-parameter type is checked, and every provided key is served. A store whose
`get(who: Principal, id: Str)` is meant to be reachable only after
`auth.validate` is served at `POST /<composition>/store/get`, and the caller
writes `who` itself. The routed bind table refuses a `Principal` parameter
(`test_route_cannot_bind_a_principal_from_transport`), but the canonical path
next to it does not, and `revl serve --mcp` has the same hole
(`ComposedServer._call_tool`, `composed.py`). The 457 design's claim that
"serving the operation over the MCP face instead changes nothing about who may
read a note" (`457-endpoint-one-definition.md:225`) is false on both faces.

Reproducer: the design's own shape, `stdlib/auth.rvl` plus a `NoteStore` whose
host body returns a note only to its owner (one note, owner `alice`), served
with `revl serve --http app.rvl stdlib/auth.rvl`:

```
GET  /notes/n1                     (no Authorization)
  -> 401 {"code": "unauthorized", ...}
POST /revl/store/get  [{"subject": "alice"}, "n1"]   (no Authorization)
  -> 200 {"ok": true, "value": {"$kind": "Ok",
          "$value": {"id": "n1", "owner": "alice", "body": "alice's private note"}}}
```

Measured on main `54a0aecb` and on the #1491 head `343cc7cb`, py 3.12, real
cordis-py at the pinned `1c5e6f17`. The same forge over `revl serve --mcp`
(`tools/call revl.store.get`) returns `Ok` for subject `alice` and `Err` for
`mallory`.

### Finding 2: `admission.admit` is an operator-class verb on the face

`stdlib/admit.rvl` provides `admission: Admission`, whose `admit(source,
granted)` runs `Session.admit` (`admit_bridge.py`, `session.py:4331`): it
compiles a source and wires it into the running composition. Its `granted`
parameter is declared `Trusted[List[Str]]` because it is authority selection
("untrusted data must never reach it"). Composed as a root module, as the file
instructs, it is served at `POST /<composition>/admission/admit`, and the body
supplies both arguments:

```
POST /revl/admission/admit  ["service Turn { ... } component TurnComp requires ops: Ops ...", ["Ops"]]
  -> 200 {"ok": true, "value": "{\"admitted\": true, \"keys\": [\"turn\"], ...}"}
```

Measured on the same two commits. Admitting code into a running composition is
the `load` family of operator authority; the item-329 profile still applies (no
host code, granted-only reach, additive only), but the caller picked the grant.

### What B needs to be sound

* The face serves the program's **public surface**, not its wiring (slice B1).
* A parameter whose type is an authority (`Principal`, anything `Trusted[...]`)
  is never decoded from the wire, on either path or either face (slice B1).
* The operator keeps a working E-Stop and a way to answer tickets without being
  an HTTP caller of the face (slices B2 and C2).
* `auth.validate` is backed by a real validator. The py stub accepts any
  non-blank token as that subject; that is documented as a test stub
  (`stdlib/auth.rvl`) and the real binding is item 457 Slice 3 / design 529.
  Since issue #1554 the py body is an HS256 JWT validator configured from the
  environment, the stub runs only behind `REVL_AUTH_INSECURE_DEV_STUB=1`, and
  an unconfigured deployment refuses every call with `503`
  `auth_not_configured` (see `revl serve` in `docs/commands-reference.md`).

## 4. Option C: hybrids

**C1: one listener, application users by default, mTLS-authenticated operator
routes on it.** Rejected. Browsers send client certificates ambiently, so an
operator whose browser holds their certificate can be driven by any page the
application allows with `--allow-origin`: a cross-site request to the operator
routes succeeds as that operator. It also forces the listener to accept both
`Authorization` (the application's) and a client certificate (the operator's)
on one request, which is the "two identities are none" case the transport
refuses today for good reason. And a single `Exposure` means one Origin policy
for two populations with opposite needs.

**C2: two listeners, one process, one session.** Sound, and recommended as the
follow-up to B. `revl serve --http` keeps the application face as in B and may
additionally open an operator listener on a second address, running the MCP
HTTP transport of #1486 (`HttpTransport`, `ServerDispatcher`) against the
**same** `Session` and the **same** `DispatchLock`, with `--operator-profile`
required and `--allow-origin` refused on it. The two populations never share a
socket, a header or an Origin policy. The operator gets `revl_approve` for the
tickets the face hands out, `revl_state`, and a `revl_estop` that is never
fenced (#1491's rule). This is the only way a ticket the face returns can ever
be answered, since today nothing reaches the face's session but the face.

## 5. Threat model

| Threat | A: operators | B: app users (after B1) | C2 |
| --- | --- | --- | --- |
| DNS rebinding | fixed by `http_guard.request_refusal` (Host), #1486 | same | same, on both listeners |
| credential theft | a stolen user credential is an operator secret and a vote credential: management authority and quorum weight | a stolen app credential is that user inside the app's own authz; no operator secret is on this listener | as B for the face; operator secrets live only on the operator listener |
| confused deputy, app identity vs operator identity | the `Authorization` header is both the operator secret and the handler's `Bearer`; mTLS instead brings ambient-certificate CSRF | none by construction: the server holds no identity to lend | none, if the operator listener refuses `--allow-origin` and is on a separate socket |
| confused deputy, wire value vs authority value | Findings 1 and 2 remain, reachable by every operator | closed by B1 | closed by B1 |
| E-Stop reachability | an HTTP route on the public listener | out of band only: the runtime latch (see below) | `revl_estop` on the operator listener plus the latch |
| one slow call blocks all | yes (one lock) | yes | yes; the operator listener's E-Stop takes the latch path when the lock is held (#1486 design) |

**E-Stop on the face today.** The face prints no latch and arms none. The py
runtime does read an ambient `REVL_ESTOP_LATCH` (`estop_latch_path`,
`runtime.py:2572`), so `REVL_ESTOP_LATCH=F revl serve --http ...` followed by
`revl estop --latch F` reaches the process. Measured on main: after arming, a
witnessed effect was refused (`EstopHalted`, at registration, after the host
body ran, as `transactional` documents at `runtime.py:3317`), but a **direct
extern emission** (`emit announce(...)` in a provided op) still fired, because
`extern_emit` (`runtime.py:1282`) has no `_estop_check`. That gap is in the py
runtime, not in the face, and it affects `revl run` equally; it is reported
separately. `Session.call`'s own `_refuse_if_halted` (`session.py:3734`) reads
only `Session._halted`, which the latch does not set.

## 6. What to build for B, sliced

**B1. The face serves a declared public surface, and no authority is decoded
from the wire.** (Precondition for B, and needed under A or C too.)

* Default surface: routed operations (item 457), plus canonical ops of keys that
  no component in the composition `requires`. `--expose KEY` adds a key;
  `--expose-all` restores today's behaviour and prints a warning naming every
  internal key it exposes.
* Refuse to serve, at start, any exposed op with a parameter of type
  `Principal` or `Trusted[...]` (or containing one), naming the op and the
  type. The routed bind table already refuses `Principal`; this applies the same
  rule to the canonical path, in `HttpComposedServer.__init__` and in
  `ComposedServer._project`, so `revl serve --mcp` is fixed by the same change.
* `stdlib/admit.rvl`'s `Admission` is never exposed, even with `--expose-all`:
  its `granted` is `Trusted`, so the parameter rule above already refuses it,
  and the refusal names the service. (`stdlib/reflect.rvl`'s `Reflection` is a
  read-only list of resolved keys; whether it may be public is open question 1.)
* The manifest (`GET /`) lists only the exposed surface.
* Tests (new file `tests/test_serve_http_public_surface.py`):
  `test_principal_param_is_not_served_on_the_canonical_path` (the Finding 1
  reproducer, expecting 404 and a start-time notice),
  `test_admission_admit_is_never_served` (Finding 2, 404),
  `test_required_key_is_internal_by_default`,
  `test_expose_adds_a_key_and_refuses_a_principal_one`,
  `test_serve_mcp_projection_matches_the_http_surface`,
  `test_manifest_lists_only_the_exposed_surface`. The first two fail on main.
* Docs: `docs/interop-bridge.md` / the `revl serve` entry in
  `docs/commands-reference.md`; correct the sentence at
  `457-endpoint-one-definition.md:225`.

**B2. The operator's E-Stop reaches the face.**

* `serve_http` arms a private latch when `REVL_ESTOP_LATCH` is unset and prints
  `revl estop --latch <path>` at start, as the MCP transport does.
* `HttpComposedServer.dispatch`/`_serve_route` refuse with 503 once the latch is
  engaged (read before taking the lock), without calling `Session.call`.
* Depends on the runtime fix for `extern_emit` for the "nothing new fires"
  half; B2 alone guarantees only that no new request is dispatched.
* Tests: `test_face_prints_its_estop_latch`,
  `test_armed_latch_refuses_the_next_request_before_dispatch`,
  `test_armed_latch_during_a_held_lock_refuses_the_queued_request`.

**B3. An approval policy on the face.**

* `revl serve --http --approval-policy auto` loads the policy on the session, so
  class-(c) crossings stop firing ungated and return the ticket.
* Optional `--refuse-ungated-emissions`: refuse to start a composition whose
  exposed surface reaches a class-(c) crossing with no policy loaded.
* Tests: `test_class_c_crossing_returns_a_ticket_under_auto_policy` (the dormant
  403 arm, made reachable), `test_no_policy_start_warns_about_class_c_ops`.

**C2. The operator listener** (after B1 to B3).

* `revl serve --http HOST:PORT --operator-http HOST2:PORT2 --operator-profile P`
  opens the #1486 transport on the same `Session` and `DispatchLock`;
  `--allow-origin` is refused for the operator listener; the two addresses must
  differ.
* Tests: `test_ticket_from_the_face_is_approved_on_the_operator_listener`
  (re-issue fires once), `test_operator_listener_refuses_any_origin`,
  `test_face_request_cannot_reach_mcp_path`,
  `test_estop_on_operator_listener_halts_the_face`,
  `test_both_listeners_share_one_dispatch_lock`.

## 7. Open questions for the owner

1. **Default surface.** Is "routed ops plus keys nobody requires" the right
   default, or should only routed ops and explicit `--expose` keys be served?
   The second is safer and breaks clients that call the canonical path for an
   unrouted op today.
2. **Canonical path next to routes.** When a composition declares routes,
   should the canonical `POST /<c>/<key>/<op>` path stay on at all? Today a
   routed op is reachable both ways, and the canonical path takes the `Bearer`
   from the body instead of the header.
3. **`revl serve --mcp`.** Same projection, same Finding 1. Fix both in B1, as
   proposed, or treat the MCP face as operator-facing and fix only HTTP?
4. **Ungated class-(c) crossings.** Should `revl serve --http` refuse to start,
   or only warn, when the exposed surface can emit and no approval policy is
   loaded?
5. **The py auth stub.** Should `revl serve --http` refuse to serve a
   composition whose `Auth` provider is the stub `AuthProvider` on a non-loopback
   address, until the item 457 Slice 3 binding exists?
6. **C2 at all.** Is an in-process operator listener wanted, or is "E-Stop by
   latch, approvals not supported on the face" an acceptable permanent answer?

## 8. Decision and what was built

The owner accepted **B + C2** (issue #1553). What shipped, and where it departs
from sections 6 and 7:

**B1** (PR #1505): as proposed, with open question 1 answered the safer way.
The face serves routed operations plus an explicit public set, and the language
has no public marking yet, so the canonical path serves nothing today.

**C2, the operator listener** (`src/revl/mcp/operator_listener.py`). The flag is
`--operator-listen HOST:PORT` (not `--operator-http`), with `--operator-profile`,
`--operator-auth`, `--operator-tls-cert`, `--operator-tls-key`,
`--operator-tls-client-ca`, `--operator-allow-host` and `--profile-settle-ms`.
It is `HttpTransport` over `ServerDispatcher`, reused rather than copied: the
compiler server's `SESSION` is the face's session while it runs, and the face
dispatches through the transport's `CallerBinding`, so both listeners take one
lock. Each app request is bound, for that request only, to the operator token
`<app caller>`, which no profile can declare. It takes no `--allow-origin`, so
`http_guard` refuses every request carrying an `Origin`; it refuses the app
face's port; and like every revl listener it refuses a non-loopback address
without TLS.
Both listeners hold one session reference: the one the binding yields under
the shared lock. A `revl_fork_confirm` on the operator listener freezes the
parent and makes the branch the only live continuation (item 250), and the face
follows it before its next dispatch rather than serving the frozen parent. That
was chosen over refusing `revl_fork_confirm` there, because a fork's point is to
continue on the branch, and the parent is non-callable after it.

**B2, E-Stop.** `revl_estop` on the operator listener, never fenced and never
queued: while an app request holds the session it arms the transport's latch.
The face reads that latch before it waits for the lock and again once it holds
it, so every later request, including one already queued, is refused `503`
(`"code": "halted"`) without dispatch. The request in flight is refused at its
next crossing seam that reads the latch, and that seam coverage is the runtime's
(item 443, and issue #1504 / PR #1518 for every crossing before its host body).
The face itself never serves an E-Stop. The latch path is printed at start.

**B3, approval routing.** `revl serve --http --approval-policy auto` loads the
policy on the session (with recording, as item 246 requires). A class-(c)
crossing an app request reaches raises the ticket; the app caller gets `403`
with `"code": "pending_approval"` and the ticket id only, no ticket body, no
approve instruction and no operator identity. The two-step is the existing one,
unchanged: an operator answers on the operator listener with `revl_approve` or
`revl_revoke` and the `hash`, and the app's identical re-issue then fires once,
or is refused once with `"code": "approval_refused"`. Asking after either is a
new question. The one addition to the ticket machinery is the revoke of a
single-party ticket: `revl_revoke` with a `hash` used to refuse any ticket that
did not demand a quorum, so a plain ticket could only be approved or left
pending. `Session.revoke_ticket` now answers it NO (withdrawing a yes that was
minted and not yet spent, and closing the round to a later approve), and
`Session.call` refuses the re-issue it held with `ApprovalRefused`. There is no second approval path.

`--refuse-ungated-emissions` (section 6, B3) is built as a per-request refusal
rather than a refusal to start, and it is opt-in (open question 4 stays open
for the default). With it and no policy, an app request that reaches a class-(c)
crossing (or one whose class cannot be resolved) is refused `403`
`ungated_emission`, naming the operation, and nothing fires; the operations it
will refuse are listed at start. Without it, and without a policy, a class-(c)
crossing still fires unapproved, as before. With a policy and no operator
listener, `revl serve` warns that nothing can answer its tickets.

Tests: `tests/test_serve_http_operator_listener_1553.py`.

