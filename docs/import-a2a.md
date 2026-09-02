# `revl import a2a`: an A2A 1.0.0 Agent Card as a checked revl surface

    revl import a2a agent-card.json [--backend ts|py] [--service NAME]
                                    [--allow-plaintext] [-o out.rvl]

The fifth member of the import codegen family, after `revl mcp import`,
`revl import wit`, `revl import openapi` and `revl import cordis`. It reads an
**A2A 1.0.0 Agent Card** and emits revl: a `service` for the agent's skills, one
`extern` per skill, and a provider component, so a revl composition can consume
an external agent as an ordinary coeffect.

This is **slice 1 of roadmap item 439**. It binds the Agent Card and the
single-crossing `message/send` call. Read "What this slice does not do" before
you plan around it.

---

## 1. Why this member is stricter than its siblings

WIT and OpenAPI describe *specified* interfaces: a WIT `func` has a signature,
an OpenAPI operation has a schema. A Cordis plugin is untyped TypeScript, but it
is at least source you hold. An A2A agent is none of those. It is a process
someone else runs, and its Agent Card is a document that process serves about
itself.

> **An Agent Card is a CLAIM, not a specification.** Every fact this importer
> transcribes (the skills, the modalities, the protocol version) is the remote
> agent's own assertion about itself. The importer's job is to turn that
> assertion into a *bounded local declaration*, never into a guarantee.

That makes every A2A provider item 329's untrusted-author case by construction
rather than by policy, and it drives three properties that are enforced by the
compiler rather than promised in prose.

**Every result is `Untrusted[Str]`** (item 424 D-424c.9). A generated client
looks exactly like a local provider at every call site, which is what would
otherwise make it the largest hole in revl's taint story. With the qualifier in
place the checker refuses a card result that reaches an authority-granting sink
(G9) until a `verified fn` parses it or an `endorse[...]` declassifies it at a
declared, auditable point:

```
error: untrusted value (net) flows into a shell command at argument 1 of
       `run_cmd` — untrusted input cannot directly create authority (G9)
```

**The reach bound comes from the endpoint's HOST alone** (D-424c.10, item 421
F4): never the port, never the userinfo. A credential riding in a card's `url`
is a live secret, not an identifier, so it is dropped from the comment, from the
host bodies and from the capability token, and the generated header says that it
was. A bare IP host folds to a prefixed token (`127.0.0.1` becomes
`net.h_127_0_0_1`), because an unprefixed fold would lex as a number with digit
separators rather than as an identifier.

**The version claim is exact.** The card must say `"1.0.0"`; anything else is
refused naming the version it claimed, and the generated header says
"A2A 1.0.0", never "A2A" (item 439 decision (3)). The protocol moves, and a
binding that follows it silently is asserting a compatibility nobody checked.

**There is no "verified remote" badge.** Importing a card does not admit,
verify or re-admit the agent. Item 337 requires the *receiver* to re-compile
from its own independently held source, and a client is the sender (D-424c.8).
What is bounded here is local and only local: the reach, the capability, the
taint and the failure mode. If both sides are revl and both want a mutual
guarantee, that is `revl contract export` / `revl contract check`, or item 337's
seam, not this.

---

## 2. Teardown: a remote A2A call cannot participate in G7

This is the decision the slice exists to make, so it is stated plainly rather
than left to be discovered.

revl's teardown stack has exactly three entry kinds
([teardown-contract.md](design/teardown-contract.md)):

| kind | grade | inverse it needs |
|---|---|---|
| `bracket` (an acquire's release) | proof (G4/G7) | infallible by contract (G5) |
| `transactional` (item 243) | proof | **host-local**, with a witness captured on the `Ok` branch |
| `compensation` (item 247) | audit (G8 intent) | best-effort, abort-only, may fail into residue |

An A2A skill can be neither of the first two:

* **not `bracket`**: an inverse that travels over a network is fallible by
  construction. The peer may be unreachable, restarted, or simply gone by the
  time teardown runs, so it cannot carry G5's infallibility.
* **not `transactional`**: item 243 wants a host-local inverse and a witness.
  An A2A agent's state is not host-local, and any witness it hands back is one
  more claim from the same unchecked peer.

A2A 1.0.0's `tasks/cancel` is **not an inverse** either. It asks a *running*
task to stop, the agent is permitted to refuse it, and it says nothing whatever
about a task that already reached a terminal state. So the generated externs
never call it at teardown, and this importer never synthesizes an inverse of any
grade.

**Therefore every generated operation is `emission` with no inverse.** That is
G4's other branch, the one that means *declared irreversible*. The consequences,
stated so nobody has to infer them:

* When the local composition unwinds cleanly, **the remote effect stays**.
  Nothing here replays.
* When it aborts, nothing here replays either, unless you attached a
  `compensate` **by hand**.
* A `compensation` entry is the only kind an A2A call can ever carry. It is
  audit-grade and best-effort, it may fail into `compensation-residue`, and it
  is yours to write, because only you know which remote operation undoes which.
  The Agent Card does not say, and this importer will not guess.

The generated file carries this reasoning in its header, so a reader of the
`.rvl` gets it without reading this page.

---

## 3. What is generated

For a card declaring a `Billing Agent` at `https://billing.internal:8443/a2a`
with an `invoice-lookup` skill:

```revl
service BillingAgent {
  // skill `invoice-lookup` (Invoice lookup)
  // tags (the agent's own claim): billing, read
  // `emission` with NO inverse: a remote effect has no local undo (see the header).
  // The result is `Untrusted[Str]`: the peer is not this composition's trust domain.
  emission fn invoice_lookup(message: Str) -> Untrusted[Str]
}

extern emission[net.billing_internal] fn a2a_billing_agent_invoice_lookup(
    message: Str) -> Untrusted[Str]
  = @ts { /* JSON-RPC 2.0 `message/send`, one crossing */ }

component BillingAgentProvider provides billing_agent: BillingAgent {
  provide billing_agent {
    fn invoice_lookup(message) = a2a_billing_agent_invoice_lookup(message)
  }
}
```

A skill is transcribed as **text in, text out**. That is not a simplification:
an Agent Card carries no parameter or result schema for a skill, so text is all
there is to transcribe, and nothing richer is invented.

Unlike its siblings the extern bodies are **real, not stubs**: `--backend ts`
and `--backend py` each emit a working JSON-RPC 2.0 `message/send` crossing that
posts the message, checks the reply, and raises on anything it cannot honour.

---

## 4. What this slice does *not* do

| not projected | why, and where it goes instead |
|---|---|
| the `remote` row, `on_failure`, per-realm peers | item 424(c)'s slice C2, which is not built. A card imports as an ordinary provider component. Turning a transport failure into provider **withdrawal** (D-424c.3) arrives with that row; here it is a fault raised out of the host body. |
| the A2A Task lifecycle | item 439's load-bearing open question (does an A2A Task map to one emission, to a stream (item 130), or to a session (item 250)?) is **not answered here and not pre-empted**. This slice binds only the subset where the question does not arise. |
| `message/stream`, `tasks/resubscribe`, push notifications | need the lifecycle answer first. A card's `capabilities.streaming` / `pushNotifications` are recorded in the generated header as NOT PROJECTED rather than silently dropped. |
| gRPC and HTTP+JSON transports | refused naming the transport. They are a transport each, not an approximation of JSON-RPC. `additionalInterfaces` are recorded, not projected. |
| non-text modalities | a `FilePart` or `DataPart` has no transcription this slice defines. |
| `capabilities.extensions` | recorded, not projected. An extension is one more unchecked claim by the same peer; no extension can grant this boundary a property revl would otherwise have had to check. |

The Task-lifecycle boundary is enforced, not just documented. A `message/send`
is bound only where the task reaches a **terminal** state in that one crossing.
A reply still in `working`, `input-required`, `auth-required` or any other
non-terminal state is a fault at the boundary: the generated body refuses to
poll, refuses to resubscribe, and refuses to return a plausible-looking empty
answer.

---

## 5. Refusals

Nothing is guessed. The importer refuses, naming the JSON pointer:

| refused | reason |
|---|---|
| a missing `protocolVersion`, or any value other than `"1.0.0"` | version honesty (decision (3)) |
| a `preferredTransport` other than `JSONRPC` | slice 1 speaks JSON-RPC 2.0 only |
| a missing `url`, or one outside a strict absolute-http(s) character class | the endpoint is interpolated into a generated comment **and** a generated host body, so it is validated up front rather than escaped afterwards. Quotes, braces, backslashes, whitespace and control characters are refused, never repaired. This is the same class of hole as item 416f in the sibling importer, closed by refusing. |
| a plaintext `http` endpoint | an A2A peer sits outside the trust boundary and everything crossing to it is authority leaving the process. `--allow-plaintext` imports it anyway (a loopback development agent) and the generated header records that you did. |
| a card with no `skills` | there is no callable surface to generate and none to invent |
| a skill with no `id`, or an `id` that does not fold to `[a-z][a-z0-9_]*` | an operation silently renamed is a boundary nobody can match back to the card |
| two skills folding onto one operation name | both cannot be called |
| a skill whose effective `inputModes`/`outputModes` are not all `text/*` | no transcription for it in this slice |

Every card-derived string that reaches a `//` comment goes through the audited
`_comment_safe` sanitizer, so a newline in the agent's own text cannot end the
comment and drop the rest into compiled source. A credential in the `url` never
reaches the generated file, the request, or the capability spelling.

---

## 6. Related

* [import-openapi.md](import-openapi.md), [import-cordis.md](import-cordis.md),
  [import-wit.md](import-wit.md): the rest of the family
* [design/424-dsh-language-gaps.md](design/424-dsh-language-gaps.md) §3: the
  semantics this binding makes concrete (D-424c.1 through D-424c.10)
* [design/teardown-contract.md](design/teardown-contract.md): the three entry
  kinds §2 reasons from
* [design/329-untrusted-author-profile.md](design/329-untrusted-author-profile.md)
* [federation.md](federation.md), [interop-bridge.md](interop-bridge.md): the
  revl-to-revl answers, for when both sides *are* revl
