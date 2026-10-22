---
id: P57
title: "Derived MCP Hints and a Gating Proxy: Annotations Derived From Checked Emission Classification"
theme: agents
kind: tool-demo
maturity: partial
status: Hint derivation and the gating proxy ship on main with 26 proxy tests and 8 adversarial hint tests; no corpus evaluation, no overhead measurement and no mechanized proof of the hint projection yet.
fused_with:
---

## Pitch
An MCP tool's `readOnlyHint` can be a compiler-derived fact instead of an author's assertion, and the same classifier can run as a proxy that refuses to believe the hints of servers it did not compile.

## Abstract
The Model Context Protocol lets a server annotate each tool with `readOnlyHint` and `destructiveHint`. These are assertions by the server author. Nothing checks them, and tool-poisoning attacks exploit that gap. We describe a design in which the hints are derived rather than declared. In revl, a service operation carries a checked effect class (plain, witnessed, emission); the checker refuses any provider that reaches a boundary the declaration did not admit, and the emission analysis is propagated transitively, including through first-class function values. The service-to-tool projection reads the class and emits `readOnlyHint` and `destructiveHint` from it, together with provenance (`x-revl`). For servers we did not compile, `revl mcp proxy` sits between an unmodified MCP server and its client, classifies the server's tool list with the same classifier used by `revl mcp import`, and gates every `tools/call`. The proxy treats an uncontradicted `readOnlyHint: true` as unchecked and holds it behind an approval ticket by default; trust is opt-in per process (`--trust-read-only-hints`). Declared contradictions are caught at startup and a resource-change notification observed during a "read-only" call withdraws the claim at run time. We list what the proxy guarantees and, as the docs do, what it cannot: the truth of an opaque upstream's claim. The implementation is in the revl tree with 26 proxy tests and 8 adversarial hint tests. We report no user study or attack benchmark yet.

## Problem and claim
MCP clients use `readOnlyHint` and `destructiveHint` to decide what to show a human and what to auto-approve. The annotations are written by the same party whose tool is being trusted. A poisoned or buggy server can mark a mutating tool read-only, and the client has no way to tell.

The claim has two halves. First, for tools that revl itself compiles, the hints are consequences of the checker: a tool is read-only because the compiler refused to compile its provider otherwise (`src/revl/mcp/composed.py:9-16`, `src/revl/mcp/schema.py:159-168`). Second, for tools revl did not compile, a gateway can be sound in one specific direction: no tool is more permissive under the proxy than the importer would make it, and the one thing it cannot check (the upstream's read-only claim) is gated by default rather than trusted (`docs/mcp-proxy.md:195-222`).

The formal development proves the checker's G4 classification (`formal/STATUS.md:260-266`), but no mechanized theorem covers the projection from that class to `readOnlyHint`. The "derived" claim is design-level until one does.

## Contributions
1. A projection from a revl `service` declaration to an MCP tool definition in which `readOnlyHint` and `destructiveHint` are computed from the checked effect class, `openWorldHint` from extern reachability and `idempotentHint` from a `commutative` declaration, with an `x-revl` provenance block (`src/revl/mcp/schema.py:40`, `:156-190`).
2. A soundness repair story for that projection: an emission hidden behind a first-class function value used to compile with `readOnlyHint: true` and fire at run time; the fix treats a bare reference to an emitting callable as reaching an unnameable boundary, with adversarial tests (`tests/test_mcp_hint_adversarial.py:1-20`, `docs/mcp-bridge.md:99-120`).
3. A single classifier, `classify_imported_tools`, shared by `revl mcp import` and `revl mcp proxy`, with an explicit trust direction (MCP to revl: nothing vouched for, only an uncontradicted read-only claim avoids `emission`) (`src/revl/mcp/schema.py:530-560`).
4. A gating proxy for an unmodified stdio MCP server: no `.rvl` written; every `tools/call` routed through `Session.call`; unchecked read-only claims gated by default; declared and observed contradiction; operator-declared `--undo` making a tool witnessed (`docs/mcp-proxy.md:58-153`).
5. An explicit guarantee and non-guarantee list for the proxy, including the advisory status of the gate without an operator profile (`docs/mcp-proxy.md:86-92`, `:195-256`).

## Related work
| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| MCP specification tool annotations | Author-asserted hints, explicitly untrusted by the spec for unknown servers (citation to be checked) | Hints derived from a checked effect class for compiled tools; for foreign tools the proxy re-publishes the annotations it enforces, not the ones claimed (`docs/mcp-proxy.md:151-152`) |
| MCP-Scan and tool-poisoning work (Invariant Labs) (citation to be checked) | Scans tool descriptions for injected instructions, pins tool hashes | Different axis: classifies the effect of a call and gates it; does not read descriptions for intent. Complementary |
| OpenAPI and gRPC reflection | Describe a surface; safety of GET vs POST is convention | The effect class is a checked property of the implementation, not of the verb name |
| Envoy filters, OPA / Gatekeeper | Policy over requests at a proxy | Policy input is a classified effect with a trust direction and a read-only-honesty protocol, plus witnessed undo and abort |
| LangChain and similar tool annotations | Free-form metadata | Annotations are outputs of the type and effect checker |
| Effect systems (Koka, Eff; see the [related-work table](../related-work.md)) | Effects tracked in types | Applied to a wire protocol's safety hints, with a fail-closed import for code that is not tracked |

## Evidence on main
| Claim | path:line or test | Status | Note |
|---|---|---|---|
| Hints are derived from the checked class, not asserted | `src/revl/mcp/schema.py:156-168`; `src/revl/mcp/composed.py:9-16` | shipped | `readOnlyHint` is false for witnessed and for `local` writes; `destructiveHint` true for emission or local writes |
| Provenance block with classification and capabilities | `src/revl/mcp/schema.py:170-185`; `docs/mcp-bridge.md:122-133` | shipped | |
| First-class emission smuggling was a real soundness bug and is fixed | `tests/test_mcp_hint_adversarial.py:1-20, 54-178`; `docs/mcp-bridge.md:99-120` | shipped | 8 tests; the doc admits the cost: a dispatcher that ever receives an emitting function is may-emit for every caller |
| Service declaration is an upper bound on providers | `docs/mcp-bridge.md:82-95` | shipped | G4 diagnostic quoted in the doc |
| Verb-level annotations on the compiler's own MCP server are "derived from what the handler does" | `docs/mcp-reference.md:57-60`, table at `:215-232` | shipped (documented); derivation method not checked | The doc says derived, not how |
| One classifier for import and proxy | `src/revl/mcp/schema.py:530`; `src/revl/mcp/proxy.py:369`; `tests/test_mcp_proxy.py:79` | shipped | |
| Unchecked read-only claim gated by default; opt-in flag | `docs/mcp-proxy.md:125-146`; `src/revl/cli/interop.py:478`; `tests/test_mcp_proxy.py:68, 257, 277, 480` | shipped | |
| Declared contradiction | `src/revl/mcp/schema.py:515-525`; `tests/test_mcp_proxy.py:118-135` | shipped | `destructiveHint: true`, revl provenance saying emission, or an operator-declared undo |
| Observed contradiction (resource-change notification during a read-only call) | `docs/mcp-proxy.md:107-116`; `tests/test_mcp_proxy.py:155, 312, 338, 416` | shipped | |
| Unclassifiable tool takes the most restrictive class | `docs/mcp-proxy.md:75-84`; `tests/test_mcp_proxy.py:136-153, 407` | shipped | |
| Witnessed undo on abort, commit keeps | `docs/mcp-proxy.md:155-178`; `tests/test_mcp_proxy.py:353, 377` | shipped | |
| E-Stop refuses later calls before the upstream | `tests/test_mcp_proxy.py:388` | shipped | |
| CLI stdio round trip and abort on disconnect | `tests/test_mcp_proxy.py:437` | shipped | |
| Upstream text is never compiled as code | `docs/mcp-proxy.md:217-221` | shipped (documented) | The renderer was not read to confirm |
| Mechanized theorems for the checker's G4 classification | `formal/RevL/Theorems/G4_ClassifiedInverseOrEmit.lean`; `formal/STATUS.md:260-266` (`RevL.G4Classified.inverse_or_emit_classified`, `reached_crossing_carries_inverse_or_marker`, `fn_wrapper_still_crosses`) | partial | Proves the inverse-or-emit classification of the checker. It does not cover the projection to `readOnlyHint`/`destructiveHint` |
| Mechanized theorem for the hint projection (`readOnlyHint` implies no emission reach) | none found under `formal/` | designed | The projection rests on construction in `tools_from_ir`, not on a proof |
| Proxy gate is advisory without an operator profile | `docs/mcp-proxy.md:86-92, 238-239` | shipped | Stated by the docs |

## Numbers that exist
- 26 test functions in `tests/test_mcp_proxy.py`, 8 in `tests/test_mcp_hint_adversarial.py` (counted with `rg -c "def test"`; some are parametrized, so executed case counts are higher and have not been run).
- No attack corpus, no false-positive rate, no prompt-burden measurement, no latency overhead. None yet.

## Paper outline
1. Introduction: hints as assertions, the poisoning gap.
2. Background: MCP annotations; revl effect classes (plain, witnessed, emission) and G4.
3. Deriving hints: projection, provenance, the first-class-value hole and its fix.
4. The proxy: classifier, trust direction, read-only honesty (declared, observed, unchecked), default gating.
5. Undo, commit, abort and E-Stop through a foreign server.
6. Guarantees and non-guarantees.
7. Evaluation (to be run).
8. Related work and discussion.

## Threats to validity and limits
- The docs state what the proxy cannot guarantee: the truth of a read-only claim; the first call of a lying tool under `--trust-read-only-hints` (it has already run); that an undo undoes; anything outside `tools/call` (resources, prompts, completions pass through ungated; the upstream process is not sandboxed); a real gate without an operator profile (the caller can approve its own tickets); recovery after the proxy dies; plaintext witnesses in the WAL; server-initiated requests and cancellation (`docs/mcp-proxy.md:223-256`).
- Transport: stdio to the upstream only; "An HTTP upstream is not implemented yet" (`docs/mcp-proxy.md:253-256`).
- Derived hints are only as strong as the checker. The `local` extern class is declared by the extern author and the checker "does not inspect the host body" (`src/revl/mcp/schema.py:127-130`). The static guarantee "bounds what the compiled program's own bodies can reach, not what host-provided values do" (`docs/mcp-bridge.md:105-106`).
- The bug history (hint compiled as read-only while emitting) shows the derivation depends on analysis completeness; the first-class-value fix is deliberately conservative.
- `revl mcp import` has a weaker default than the proxy by design (human review replaces the check); a reader may find two defaults confusing.
- One known exception is documented: after a reclassified surface is swapped in, a failing undo raises out of the abort instead of being logged (issue #1473, `docs/mcp-proxy.md:212-216`).
- No threat-model evaluation, overhead measurement or false-gate cost has been run; those are the next results needed.

## Fact-check
Claims in this draft were checked against revl main at commit 45bec431f: 23 checked, 9 corrected.
