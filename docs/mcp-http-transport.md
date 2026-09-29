# MCP over HTTP: one operator per request

Issue #1463, slices 1 and 2 · source: `src/revl/mcp/http_transport.py`,
`src/revl/mcp/http_stream.py`, `src/revl/mcp/http_guard.py` · companion to
[operator-capabilities.md](operator-capabilities.md) (item 55),
[component-leases.md](component-leases.md) (item 61),
[mcp-proxy.md](mcp-proxy.md) and [mcp-reference.md](mcp-reference.md).

Over stdio, one `revl mcp serve` or `revl mcp proxy` process has one client and
runs as one operator. Over HTTP, several clients reach the same session. What
makes leases, approval tickets and quorum votes mean anything then is knowing
which operator each request comes from. This transport binds every request to
one operator of the operator profile, and refuses a request that has no
identity.

```bash
revl mcp serve --http 127.0.0.1:8765 --operator-profile ops.profile \
    --approval-policy auto
revl mcp proxy --http 127.0.0.1:8766 --operator-profile ops.profile \
    -- node notes-server.js
```

The endpoint is `http://HOST:PORT/mcp` (`https://` with TLS).

## Protocol

MCP revision **2026-07-28**, Streamable HTTP, and only that revision:

- every JSON-RPC message is its own `POST /mcp`; the reply is one JSON object
  (`Content-Type: application/json`) or an SSE stream (see [Streams](#streams)),
  and a notification is answered `202`;
- every request carries `_meta` with `io.modelcontextprotocol/protocolVersion`
  and `io.modelcontextprotocol/clientCapabilities` (missing: `400`, `-32602`),
  and the headers `MCP-Protocol-Version`, `Mcp-Method`, and `Mcp-Name` for
  `tools/call`, `prompts/get` and `resources/read`. A header that is missing or
  disagrees with the body is `400` with `HeaderMismatch` (`-32020`); a tool that
  declares `x-mcp-header` parameters has its `Mcp-Param-*` headers checked the
  same way;
- an unsupported version is `400` with `UnsupportedProtocolVersion` (`-32022`),
  naming `2026-07-28` as supported;
- `server/discover` answers with the supported version, the capabilities, the
  server's name and its instructions;
- every result carries `resultType: "complete"` and the server's name in
  `_meta`. No result is ever an `InputRequiredResult` (see [MRTR](#mrtr)).

The revision has no protocol session, so there is nothing to confuse with
identity: `GET` and `DELETE` are `405`, an `Mcp-Session-Id` header is ignored
and never echoed, and so is `Last-Event-ID`: streams are not resumable.

**Not served:** the legacy `initialize`-era HTTP of revisions 2025-03-26 to
2025-11-25. A legacy client sending `initialize` gets `400` with `-32022` and
the supported version; the legacy era will be added only if a real client
needs it. `resources/subscribe`, `resources/unsubscribe` and `logging/setLevel`,
which 2026-07-28 removed, are `404` (`-32601`) naming `subscriptions/listen`.

## Identity

HTTP mode refuses to start without `--operator-profile`, and refuses
`--operator`: the process runs as no operator, so there is none for a request
to fall back to.

Every request is authenticated before its body is read. A request with no valid
identity is answered `401` with `WWW-Authenticate: Bearer` and is not
dispatched: not `tools/list`, not `server/discover`, not a read-only verb.

Two ways to authenticate, one per server (`--auth`):

- **`bearer`** (default). `Authorization: Bearer <secret>`, where the SHA-256
  of the secret is the operator's `key sha256:<digest>` line in the profile.
  This is the same secret as the operator's **vote credential**
  ([operator-capabilities.md](operator-capabilities.md#vote-credentials-multi-party-approval)):
  one secret authenticates the operator to the transport and proves its votes,
  so revoking one revokes both. An operator declared with `sign p256:` instead
  of `key` has no bearer secret and cannot use this mode. A secret that matches
  no operator, or more than one, is refused.
- **`mtls`**. The client certificate's commonName is the operator token, as for
  network placement (item 56). Needs `--tls-cert`, `--tls-key` and
  `--tls-client-ca`. A request that also sends an `Authorization` header is
  refused: two identities are none.

An operator's `until` is checked on every request, so an expired credential is
refused the moment it lapses.

**Profile changes take effect without a restart** (`src/revl/mcp/live_profile.py`,
one mechanism for HTTP and stdio). The server checks the profile file on every
request with one `stat`, and re-reads it when its stat signature (mtime, ctime,
size, inode) changed, or when its mtime is within two seconds of the last read,
where two writes in one timestamp tick could leave the signature unchanged.

**New content is adopted only once it has settled:** it must read identical
twice, at least `--profile-settle-ms` apart (default 1000 ms). A profile caught
mid-write can still parse, and one cut off just before a `may not` line would
widen a grant, so an unsettled profile is never used. While the content is
settling, every request is answered `503` ("the operator profile is changing").
The previous profile is not used in that window either: the edit in progress may
be a revocation, and serving the old grants would delay it. So adding `operator
bob revoked`, removing an operator, or narrowing a grant is refused, then applied,
about one second after the file stops changing, and never served under a
half-written file.

**Write the profile atomically:** write a temporary file in the same directory,
then rename it over the profile. The settle window makes a partial file hard to
adopt; it cannot make it impossible, because a writer that pauses mid-file for
longer than the window leaves settled, partial content. A rename never exposes a
partial file at all. `--profile-settle-ms 0` removes the settle protection
entirely.

A profile that can no longer be read or parsed **fails closed**: every request
is answered `503` naming the problem, until the file is fixed.

**E-Stop is never fenced by a profile edit.** While the profile is settling or
broken, `revl_estop` is still accepted, from a caller authorized for it under the
last profile that was adopted. An E-Stop only stops things, so honouring it under
the prior profile cannot widen anyone's authority. Every other verb in that
window gets the refusal above. The server also prints its E-Stop latch path at
start (`revl estop --latch <path>`), which halts it with no request at all; that
is the only path when no profile has ever been adopted.

Over stdio (`revl mcp serve`, `revl mcp proxy`) the same file is re-read before
each message, with the same settling rule and the same E-Stop exception: the
session's own operator and the registry quorum casts are checked against are the
file as it is now. If that serve-time operator is revoked, past its `until`, or
no longer declared, every message except `revl_estop` is refused with a JSON-RPC
error. A revocation that left management verbs working would not be one.

## One identity at a time

The session is single-threaded, so requests are dispatched one at a time under a
lock. Inside the lock, the session's operator is the request's operator. Outside
it, the operator is a placeholder with no grants and the token
`<no authenticated caller>`, which no profile can declare. It is never "no
operator" (which means ungated) and never an empty token (which the lease book
reads as its default holder).

Everything that already reads the session's operator therefore sees the caller:

- **the operator gate** (item 55): each request is authorized against its own
  operator's grants. A proxied upstream tool is gated as the `call` verb, like
  `revl_call`;
- **leases** (item 61): a lease is held by the operator that claimed it. Under
  `leases enforced`, another operator's swap, unload, cold load, restore,
  commit confirm and abort of a leased component are refused (the #1450
  fences), and only the holder can release it;
- **approval tickets** (item 246): the proposer is the operator that made the
  call, and `revl_approve` is authorized against the approver's own grants. Give
  `approve` only to the human's operator and the agent cannot answer its own
  tickets. At start, the server warns about any operator that may both `call`
  and `approve`;
- **quorum votes** (item 471): each vote is the caller's own. A cast whose
  `asToken` names another operator is refused before dispatch, even with that
  operator's secret. A cast bound this way is recorded `boundBy: "transport"` in
  the decision graph (stdio records `"session"`); its principal and everything
  else about the vote are unchanged.

**What a quorum count means over HTTP.** It is a count of distinct authenticated
credentials, one per request. It is not a count of people: someone holding two
operators' secrets can still send two requests.

## E-Stop

`revl_estop` does not wait for a busy session. If another request holds the lock
(a proxied call can block for up to `--upstream-timeout`), the E-Stop is
authorized against the caller's grants without the lock, and then arms the
runtime's E-Stop latch file (item 443). Every crossing seam reads that latch, and
the next request completes the halt on the session before anything else is
dispatched. The request already in flight is not interrupted; every later call
is refused. The reply says `latched: true`.

The latch is used because `runtime.estop` is not safe to call from a second
thread: measured, walking the live-frame set raised `RuntimeError: Set changed
size during iteration` 189 times in about 200,000 halts while another thread
created frames. If the operator already armed a latch (`REVL_ESTOP_LATCH`), the
transport uses that one; otherwise it arms a private one for its lifetime.

## Streams

Slice 2, `src/revl/mcp/http_stream.py`. A stream is a `200` reply with
`Content-Type: text/event-stream` and `X-Accel-Buffering: no`; each event is one
`data:` line holding one JSON-RPC message. There are two kinds.

### An SSE reply to a request

A request whose `Accept` lists `text/event-stream` may be answered with SSE. It
is, only when a notification for that request arrives while it is dispatched;
otherwise the reply is one JSON object, as in slice 1. The one request-scoped
notification relayed is `notifications/progress` from a proxied upstream:

- the caller's `_meta.progressToken` is never sent upstream. The transport
  mints a random token for that one request, sends that instead, and maps it
  back when progress arrives. Progress under any other token reaches no one:
  not a second caller who picked the same token, and not the next request when
  the upstream reports late for a finished one;
- without `text/event-stream` in `Accept`, the token is removed before the
  upstream sees it, and the reply is JSON;
- the final JSON-RPC response is the last event, then the connection closes.
  Nothing more is sent for the request after it;
- `notifications/message` is never sent. The revision allows it only for a
  request that set `io.modelcontextprotocol/logLevel`, and the proxy does not
  relay upstream logs, so `server/discover` omits the `logging` capability;
- closing the stream is the revision's cancellation signal. The transport stops
  writing to it, but the call in flight is not interrupted and the upstream is
  not told, as on stdio, where the proxy does not forward
  `notifications/cancelled` either.

### `subscriptions/listen`

A `subscriptions/listen` request with a `notifications` filter opens a stream
that stays open until the client or the server closes it.

- It needs `Accept: text/event-stream` (otherwise `406`), a well-formed filter
  (otherwise `400`, `-32602`), and room: at most 4 open streams per operator and
  32 in all (otherwise `429`).
- The first event is `notifications/subscriptions/acknowledged`, naming the
  part of the filter the server honors. Every event on the stream carries
  `io.modelcontextprotocol/subscriptionId`, the listen request's id.
- What is honored follows the capabilities `server/discover` reports:

| filter | `revl mcp serve` | `revl mcp proxy` |
| --- | --- | --- |
| `toolsListChanged` | no: its tool list never changes | yes |
| `promptsListChanged` | no | when the upstream declares `prompts.listChanged` |
| `resourcesListChanged` | no | when the upstream declares `resources.listChanged` |
| `resourceSubscriptions` | no | when the upstream declares `resources.subscribe`, each URI it accepts |

- The proxy announces `notifications/tools/list_changed` when it withdraws a
  read-only claim ([mcp-proxy.md](mcp-proxy.md)), and after the upstream
  announced a change of its own: the proxy lists the tools again at the next
  request, from any caller, and announces it then.
- For `resourceSubscriptions`, the proxy sends the upstream
  `resources/subscribe` for a URI when the first stream asks for it, and
  `resources/unsubscribe` when the last stream holding it ends. A URI the
  upstream refuses is left out of the acknowledgment.
- A quiet stream gets a keep-alive comment (`:`) every 15 seconds.
- When the server stops, each stream gets `notifications/cancelled` naming its
  subscription, then the completion result, then the connection closes. The
  revision's cancellation page says the server MUST send the first; its
  subscriptions page says it SHOULD send the second. Sending both meets both.

### Who receives what

Every rule of slice 1 holds on a stream:

1. **Authentication first.** A stream starts only after its request has
   authenticated and its body has been read. A listen request with no valid
   identity gets `401` as JSON, with no stream headers.
2. **One operator per stream, checked again.** A listen stream belongs to the
   operator that opened it. Before every event and every keep-alive, the
   stream's credential is authenticated again against the profile as it is
   now. If it no longer authenticates as that operator (revoked, past its
   `until`, removed, or re-keyed), the stream is closed with nothing more sent,
   and a new listen gets `401`. While the profile is settling or broken, events
   are held and keep-alives stop; held events go out only if the caller still
   authenticates once the profile settles.
3. **No event reaches a caller that did not ask for it.** A list-change
   notification goes only to streams whose filter asked for that list;
   `notifications/resources/updated` only to streams that named that URI;
   progress only to the request it belongs to. Everything else an upstream
   sends (log lines, unknown notifications, its own cancellations) is dropped.
   Listing and reading are not gated by operator grants (the proxy forwards
   `resources/*` and `prompts/*` ungated, [mcp-proxy.md](mcp-proxy.md)), so
   any authenticated operator may ask for any of these events. The isolation
   is between streams: nothing reaches a stream that its own caller did not
   ask for.
4. **E-Stop is never fenced.** An open stream never holds the dispatch lock,
   so `revl_estop` is dispatched, or latched while another call holds the
   session, exactly as without streams. Its reply is JSON.
5. **A slow reader stalls no one.** Listen events are queued per stream (at
   most 256); a stream that falls that far behind is closed, and its client may
   listen again. A write to any stream that stalls for 10 seconds ends that
   stream.

## MRTR

In 2026-07-28 a server that needs input from the client (sampling, elicitation,
roots) returns an `InputRequiredResult` and the client retries. This server
never does. The compiler server needs no client input, and the proxy relays no
server-to-client request: it declares no client capability to its upstream, so
a conforming upstream sends none, and it answers any it does send with
`-32601` itself (mcp-proxy.md). No HTTP client therefore ever receives an input
request.

Because no `requestState` or input request is ever issued, a `tools/call`,
`prompts/get` or `resources/read` that carries `inputResponses` or
`requestState` is refused `400` (`-32602`) before dispatch, and nothing of it
reaches the upstream. The spec asks a server to treat `requestState` as
attacker-controlled; one this server never minted can only be forged.

## The one lock, a known limit

Every other request waits while one is dispatched (`http_guard.DispatchLock`,
the same lock `revl serve --http` takes, issue #1488). A proxied call that blocks for
its full `--upstream-timeout` (120 s by default) blocks every other caller for
that long, except `revl_estop`. This is slice 1's bound; per-request concurrency
needs a session that can run more than one call at a time.

Streams do not lift it. An SSE reply is a request still being dispatched: it
holds the lock until its final response, like a JSON reply, and its progress
shows the call is alive, not that another may run. A listen stream holds the
lock only while it subscribes or unsubscribes its URIs upstream, at open and
at close, so opening one that names resources waits for a busy session. Events
are delivered without the lock. Reading the capabilities, for `server/discover`
and for a listen acknowledgment, touches neither the session nor the upstream,
so it needs no lock either; an upstream's tool-list change is acted on by the
next dispatched request, under the lock.

## Where it may listen

These rules are shared with `revl serve --http` (`http_guard`):

- loopback (`127.0.0.1`, `localhost`, `::1`) by default. Any other address needs
  `--tls-cert` and `--tls-key`, or the server refuses to start. A wildcard bind
  (`0.0.0.0`, `::`) also needs `--allow-host` for the names clients use;
- a request whose `Host` is not one of the server's names is `403`, before its
  credential is read. A web page that re-points its own name at 127.0.0.1 (DNS
  rebinding) still sends its own name as `Host`;
- a request with an `Origin` header is `403` unless that exact origin is named
  with `--allow-origin`. A request without `Origin` is not from a browser page
  and passes this check. `*` and `null` are refused as allowed origins.

## Authorization and the MCP spec

The spec makes authorization optional, and says an HTTP server that implements it
SHOULD follow its OAuth 2.1 profile: a resource server that publishes protected
resource metadata and checks each token's audience. This transport does not do
that yet. Its bearer secrets and client certificates are static credentials from
the operator profile, which **deviates from that SHOULD**. OAuth 2.1 is planned
as slice 3. A client that only discovers credentials through OAuth metadata
cannot connect until then; one that can send a configured `Authorization` header
can.

## What slices 1 and 2 guarantee

1. No request without a valid identity is dispatched, and nothing runs as a
   default operator.
2. Each request's grants, leases, tickets and votes are its own operator's, and
   identity does not carry over from one request to the next, including when a
   request fails.
3. Over HTTP, no operator votes for another.
4. An E-Stop lands while another request holds the session.
5. A non-loopback listener is TLS or does not start, and a request with a
   foreign `Host` or `Origin` is refused before it is read.
6. An edit to the operator profile file applies without a restart, and until
   it has settled every request but `revl_estop` is refused rather than served
   under either the old or the new version. A profile that no longer parses
   refuses every request but `revl_estop`. A partial file that stays unchanged
   for the whole settle window can still be adopted: write atomically.
7. A stream carries only what its own caller asked for: a listen stream, the
   events its filter named; an SSE reply, progress for that request alone. A
   listen stream sends nothing after its caller stops authenticating.
8. No open stream holds the dispatch lock or delays an E-Stop.
9. No server-to-client request reaches an HTTP client, no result is an
   `InputRequiredResult`, and a request carrying `inputResponses` or
   `requestState` is refused before dispatch.

## What they do not guarantee

1. That an operator is one person. A shared or stolen secret is that operator.
2. Per-caller transactions. Commit and abort are session-wide: one operator's
   `revl_abort` reverts another's witnessed calls unless a lease fences it.
3. Revocation of a request already being dispatched. A profile change applies
   from the first request after it has settled (about one second by default),
   and requests in between are refused, except `revl_estop`.
4. Concurrency. See the one lock above.
5. Delivery. Streams are not resumable; a listen stream that falls 256 events
   behind is closed; closing a reply stream does not cancel the call in
   flight; and an upstream's own tool-list change is announced at the next
   request, not when it happens.
6. Revocation between the check and the write. The credential is checked just
   before each event is written; an event whose check passed before a
   revocation settled is still written, and nothing after it is.
7. The OAuth 2.1 profile of the MCP spec (slice 3).
