# `revl mcp proxy`: gate an existing MCP server

Issue #1463 · companion to [mcp-bridge.md](mcp-bridge.md) (`revl mcp import`)
and [mcp-reference.md](mcp-reference.md) (the verbs the proxy re-serves).

`revl mcp import` turns an MCP server's tool list into revl source, which you
then write a provider for. `revl mcp proxy` skips that step. It sits between an
MCP client and a server you already run, speaks MCP over stdio to both, and
gates every tool call with the same surface `revl mcp import` derives. You
write no `.rvl`.

```bash
revl mcp proxy [OPTIONS] -- COMMAND [ARG ...]
```

`COMMAND` is the upstream server, started by the proxy and spoken to over its
stdin and stdout. Its stderr passes through to the proxy's stderr.

## Putting it in front of a server

Replace the server's command in your client's MCP configuration with the proxy,
and give the old command after `--`:

```json
{
  "mcpServers": {
    "notes": {
      "command": "revl",
      "args": ["mcp", "proxy",
               "--operator-profile", "operators.json",
               "--undo", "delete_note=restore_note",
               "--", "node", "notes-server.js"]
    }
  }
}
```

At startup the proxy prints one line per tool to stderr: the class it enforces
and the state of the tool's read-only claim. By default a tool that claims to
be read-only shows `gated: unchecked read-only claim`
([below](#unchecked-read-only-claims-are-gated-by-default)). Check that list
before you rely on it; `revl_proxy_verdicts` returns the same thing, with
reasons, over MCP.

Options:

| option | meaning |
|---|---|
| `--undo TOOL=INVERSE` | `TOOL` is revertible by calling `INVERSE` with `TOOL`'s arguments (repeatable). |
| `--undo TOOL=INVERSE:result` | the same, but `INVERSE` receives `TOOL`'s `structuredContent` object. |
| `--trust-read-only-hints` | admit a tool with an uncontradicted `readOnlyHint: true` without a prompt, as `plain`. Off by default. |
| `--operator-profile PROFILE`, `--operator TOKEN` | bind the session to an operator identity (item 55), as `revl mcp serve` does. |
| `--policy POLICY` | bind a boundary policy (item 33), as `revl mcp serve` does. |
| `--approval-record-values {withheld,bound}` | whether an approved crossing's caller-supplied resource value reaches the approval log (default `withheld`). |
| `--wal PATH` | the session write-ahead log (default: the per-user state directory). |
| `--upstream-timeout SECONDS` | how long to wait for one upstream answer (default 120). |

## How a tool is classified

There is one classifier, `revl.mcp.schema.classify_imported_tools`, and both
`revl mcp import` and the proxy call it. The proxy never computes a class of its
own. It renders the classified tools with the import's renderer, compiles the
result, loads it into a live session, and routes each `tools/call` through
`Session.call`, the same chokepoint a `revl_call` passes.

| the upstream tool | class | what a call does |
|---|---|---|
| claims `readOnlyHint: true`, nothing contradicts it (default) | `emission` (class c), `gated: unchecked read-only claim` | returns a ticket; nothing reaches the upstream until a human says yes |
| the same, under `--trust-read-only-hints` | `plain`, claim reported `unchecked` | proceeds |
| the operator declared an undo for it (`--undo`) | `witnessed` (class a) | proceeds; its undo runs on abort |
| anything else, including no annotations | `emission` (class c) | returns a ticket |
| cannot be classified | `emission` (class c) | returns a ticket |

A tool cannot be classified when its `annotations` is not an object, when its
`readOnlyHint` is present but not a boolean, or when the server lists two tools
under one name. Those get the most restrictive class. A tool with no usable
name, or whose name is one of the verbs the proxy serves itself, is not proxied
at all and is listed under `excluded`.

The approval policy is always on in the proxy. A class (c) call returns the
same `approvalRequired` result `revl mcp serve` returns, with a `ticket` whose
`hash` binds the tool and its exact arguments. `revl_approve` with that hash
lets the identical call fire once. `revl_approve` with `capability` and
`uses`/`ttlMs` mints a standing grant instead (item 344).

**Without an operator profile the gate is advisory.** The client that makes the
call can also call `revl_approve`, so an agent can answer its own tickets. The
proxy prints a startup warning saying so. Bind a profile in which only the
human's identity holds `approve`
([operator-capabilities.md](operator-capabilities.md)). `revl mcp serve` closes
this hole differently since issue #1706: it refuses an approval from the
identity that raised the ticket.

## Read-only honesty

A `readOnlyHint` is the server author's assertion. The proxy checks it where it
can and says so where it cannot.

**Declared.** At startup, a read-only claim is contradicted, and the tool is
classified `emission`, when:

- the tool also declares `destructiveHint: true` (MCP gives that hint meaning
  only for a tool that is not read-only);
- the tool carries revl provenance (`x-revl.classification`) saying `emission`;
- the operator declared an undo for it, or named it as another tool's undo.

**Observed.** While a tool that claims to be read-only is running, the proxy
watches the upstream for `notifications/resources/updated` and
`notifications/resources/list_changed`. If one arrives before the call's
response, the claim is withdrawn: the tool is reclassified through the same
classifier (as `emission`), the new surface is swapped into the session, the
client receives `notifications/tools/list_changed`, and every later call needs
a yes. If the new surface cannot be put live (a compile failure, a lease the
policy enforces, a refused swap), the flagged tool is refused by name until the
proxy restarts.

**Unchecked.** A claim with no declared contradiction and no observation is
reported as `readOnlyClaim: "unchecked"`, and is never reported as verified.
What happens next depends on the default below.

Observation still runs for a gated tool. If a human approves a call to it and
the upstream announces a change during that call, the claim becomes `observed`
(refuted). Its class does not change, because it was already gated.

### Unchecked read-only claims are gated by default

The proxy does not trust an unchecked `readOnlyHint: true`. Such a tool is held
at `emission`, needs a yes per call like any other, and its verdict carries
`gated: unchecked read-only claim`. `tools/list` advertises it with
`readOnlyHint: false`, because that is what the proxy enforces.

The reason: the proxy's whole claim is that it checks tool safety claims, and a
claim it cannot check has not been checked. Letting it through by default would
be a check that trusts exactly the half it cannot verify. The default costs a
prompt; the other choice costs an unseen mutation.

An operator who knows the upstream can opt in with `--trust-read-only-hints`.
The tool is then `plain` and proceeds, its verdict still says
`readOnlyClaim: "unchecked"` together with a `trusted` field naming the flag,
and the proxy prints at startup that every unchecked claim is TRUSTED, not
verified. Declared contradictions and runtime observation apply under the flag
as well.

`revl mcp import` keeps its own rule: an uncontradicted `readOnlyHint: true`
becomes a plain `fn`. That is not a contradiction. The importer writes source
for a human to review before anything runs, and the review is the check. The
proxy enforces at run time with no review step, so it takes the stricter
default. Both read the same classifier; the proxy passes
`trust_read_only=False` unless the flag is given.

Every proxied result carries the proxy's verdict under `_meta["revl/proxy"]`,
and `tools/list` advertises the annotations the proxy enforces, not the ones
the server claimed, with the verdict under `x-revl`.

## Undo, commit and abort

`--undo delete_note=restore_note` makes `delete_note` witnessed. When a call
succeeds (the upstream result is not `isError`), the proxy registers the undo
with its witness: the call's arguments, or with `:result` its
`structuredContent`. A call that fails registers nothing.

The session then behaves as any revl session does (item 245):

- `revl_commit` lists what the session did, and `revl_commit_confirm` with the
  manifest's `hash` keeps it. The registered undos are discharged.
- `revl_abort` calls every registered undo, most recent first. An undo that
  fails is reported as `restore-residue`, not hidden.

Either verdict ends the generation; the proxy boots the next one so the client
can carry on. **If the session ends without a verdict, the proxy aborts**,
which runs the undos while the upstream is still there to receive them: over
stdio when the client disconnects, over HTTP when the proxy shuts down (one HTTP
request ending is not the session ending). Commit first if you want to keep the
work.

With an operator profile bound, a proxied tool call answers to the `call` verb,
like `revl_call`: an operator without it gets nothing from any upstream tool.

## Other verbs and methods

The proxy also serves these `revl mcp serve` verbs, dispatched by that server's
own handler so the operator gate applies: `revl_approve`, `revl_revoke`,
`revl_commit`, `revl_commit_confirm`, `revl_abort`, `revl_estop`,
`revl_estop_report`, `revl_state`, `revl_lease`. It adds `revl_proxy_verdicts`.
The client gets no verb that loads, swaps or edits code.

`resources/*`, `prompts/*`, `completion/complete` and `logging/setLevel` are
forwarded to the upstream unchanged. Upstream notifications are relayed to the
client, except `notifications/tools/list_changed`, which the proxy acts on: it
lists the tools again, reclassifies them, and sends its own
`notifications/tools/list_changed`. Over HTTP, which caller receives each
relayed notification is decided per stream: see
[mcp-http-transport.md](mcp-http-transport.md#streams).

## What the proxy guarantees

These hold as long as the proxy is the only way the client reaches the server.

1. **No emission-class call reaches the upstream without a yes.** A tool
   classified `emission` is called only after a ticket for that tool and those
   exact arguments was approved, or a standing grant covers it. The ticket, the
   approval and its spend are the session's own (item 246).
2. **The class comes from the import's classifier.** No tool is more
   permissive under the proxy than `revl mcp import` would make it; by default
   the proxy is stricter, gating unchecked read-only claims too. An
   unclassifiable tool is `emission`.
3. **A contradicted read-only claim is not believed.** Declared contradictions
   are caught at startup; an observed resource change during a read-only call
   withdraws the claim for every later call, or refuses the tool.
4. **A witnessed call is undone on abort.** Every successful witnessed call
   registers its undo, abort calls the undos in reverse order, and a failed undo
   is reported as residue. One known exception (issue #1473): after the
   proxy has swapped a reclassified surface in, an undo that fails raises out
   of the abort instead of being recorded.
5. **The session is recorded.** Approval spends and the undo descriptors go to
   the write-ahead log, as in any recorded session.
6. **E-Stop stops it.** After `revl_estop`, no proxied tool call reaches the
   upstream.
7. **Upstream text is never compiled as code.** The generated host code names
   tools by generated identifiers only; upstream names and descriptions appear
   only inside `//` comments, JSON-quoted where they could carry a line break.

## What the proxy cannot guarantee

1. **That a read-only claim is true.** The upstream is opaque. By default this
   costs a prompt per call. Under `--trust-read-only-hints`, a tool that
   mutates without announcing it stays `plain` for as long as it stays quiet.
   "Unchecked" means exactly that.
2. **The first call of a lying tool, under `--trust-read-only-hints`.**
   Observation happens during the call, so the call that exposes the lie has
   already run. By default that first call needed a yes.
3. **That an undo undoes.** `--undo` is your statement. The proxy calls the
   inverse and reports an error result as residue, but an inverse that answers
   success and does nothing is invisible to it.
4. **Anything outside `tools/call`.** Resource reads, prompts and completions
   pass through ungated, and the upstream process itself is not sandboxed. What
   it does on its own is outside revl's view (G8).
5. **A real gate without an operator profile.** See above: the caller can
   approve its own tickets.
6. **Recovery after the proxy dies.** The undos need a live upstream. After a
   crash, `revl recover` over the proxy's WAL cannot call them; they surface as
   residue, and the upstream keeps whatever state the crash left.
7. **Privacy of the witness.** An undo's witness (the call's arguments, or its
   `structuredContent`) is written to the WAL in plaintext.
8. **Server-initiated requests and cancellation.** Sampling, roots and
   elicitation requests from the upstream are answered with an error, not
   relayed, on stdio and over HTTP alike, so no HTTP client ever receives an
   `InputRequiredResult` from the proxy. The proxy declares no client
   capability to the upstream, so a conforming upstream sends none of these.
   A client's `notifications/cancelled`, and a closed HTTP response stream, are
   not forwarded.

Transport: stdio to the upstream. To the client, stdio by default, or MCP
2026-07-28 Streamable HTTP with `--http HOST:PORT`, where each request is bound
to its own operator ([mcp-http-transport.md](mcp-http-transport.md)). An HTTP
upstream is not implemented yet.
