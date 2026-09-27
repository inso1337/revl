# MCP over HTTP: one operator per request

Issue #1463, slice 1 · source: `src/revl/mcp/http_transport.py`,
`src/revl/mcp/http_guard.py` · companion to
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
  (`Content-Type: application/json`), and a notification is answered `202`;
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
  `_meta`.

The revision has no protocol session, so there is nothing to confuse with
identity: `GET` and `DELETE` are `405`, an `Mcp-Session-Id` header is ignored
and never echoed.

**Not served in this slice:** SSE responses, `subscriptions/listen` (so no
`notifications/tools/list_changed` or resource updates reach an HTTP client),
MRTR input requests, and the legacy `initialize`-era HTTP of revisions
2025-03-26 to 2025-11-25. A legacy client sending `initialize` gets `400` with
`-32022` and the supported version. The legacy era will be added only if a real
client needs it.

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

**Profile changes take effect on the next request.** The server checks the
profile file on every request with one `stat`, and re-reads it when its stat
signature (mtime, ctime, size, inode) changed, or when its mtime is within two
seconds of the last read, where two writes in one timestamp tick could leave the
signature unchanged. A new content digest is parsed. So adding `operator bob
revoked`, removing an operator, or narrowing a grant applies to the very next
request, with no restart.

A profile that can no longer be read or parsed **fails closed**: every request
is answered `503` with an error naming the profile problem, until the file is
fixed. The server never keeps serving under the previous profile, because the
edit that broke it may have been the revocation. Write the file atomically
(write a temporary file, then rename it over the profile) so no request sees a
half-written one.

Over stdio the profile is still read once, at start.

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

## The one lock, a known limit

Every other request waits while one is dispatched (`http_guard.DispatchLock`,
the same lock `revl serve --http` takes, issue #1488). A proxied call that blocks for
its full `--upstream-timeout` (120 s by default) blocks every other caller for
that long, except `revl_estop`. This is slice 1's bound; per-request concurrency
needs a session that can run more than one call at a time.

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

## What slice 1 guarantees

1. No request without a valid identity is dispatched, and nothing runs as a
   default operator.
2. Each request's grants, leases, tickets and votes are its own operator's, and
   identity does not carry over from one request to the next, including when a
   request fails.
3. Over HTTP, no operator votes for another.
4. An E-Stop lands while another request holds the session.
5. A non-loopback listener is TLS or does not start, and a request with a
   foreign `Host` or `Origin` is refused before it is read.
6. An edit to the operator profile file applies to the next request, and a
   profile that no longer parses refuses every request rather than serving
   under the old one.

## What it does not guarantee

1. That an operator is one person. A shared or stolen secret is that operator.
2. Per-caller transactions. Commit and abort are session-wide: one operator's
   `revl_abort` reverts another's witnessed calls unless a lease fences it.
3. Revocation of a request already being dispatched. A profile change applies
   from the next request on.
4. Concurrency. See the one lock above.
5. Delivery of notifications or progress. There is no stream in this slice.
6. The OAuth 2.1 profile of the MCP spec (slice 3).
