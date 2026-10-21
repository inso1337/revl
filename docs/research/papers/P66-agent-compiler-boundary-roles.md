---
id: P66
title: "The Compiler Is the Agent Interface: Four Roles of an Agent at the Language Boundary"
theme: agents
kind: position
maturity: partial
status: The four roles and the check/admit surface are shipped with tests and a measured admission latency; the lighthouse admission loop is a roadmap proposal and no agent-outcome study exists.
fused_with:
---

## Pitch

Give an agent the compiler's check and admit verbs instead of the filesystem, and the agent's four roles at a system's edge (reader of its schema, caller of it, operator of it, dependency inside it) all fall out of the boundary with no new language feature.

## Abstract

Tools for agents that write code usually wrap a shell and a filesystem, and then try to make the agent careful. We take the opposite position: the compiler, not the filesystem, should be the agent's interface. In revl, a language whose compiler also gates what may enter a running composition, an agent meets a system in exactly four roles: a consumer of its schema, a caller of the live composition, an operator of the compiler, or a dependency inside it. The project's own bridge document states that all four are boundary phenomena and that none required a language feature. We describe each role and its mechanism: services projected to MCP tool definitions; the booted composition's own operations served as tools with annotations derived from the checked emission classification rather than asserted by an author; the compiler served as a session with `revl_check` and `revl_admit` instead of file access, where drafts are held server-side and a rejection inside G1-G9 names the guarantee violated and the rewrite that fixes it; and a model as an ordinary service whose calls are emissions. An in-memory admission round trip measures about 0.165 ms median on one machine (4000 samples), which makes the gate cheap enough to sit inside a generation loop. A proposed lighthouse workload would have an agent author a component, receive a guarantee-naming refusal, and have the admitted component emitted to the TypeScript tier; this loop is specified but not yet evidenced. We state what is shipped and what is not, and we do not claim that agents do better under this interface.

## Problem and claim

Agent code tools mostly give the model files and a shell, then recover from bad output with tests and retries. The failure cost is paid at runtime. The project's own account of why its authoring loop exists is telling: a team building on the compiler spent weeks reinventing a generate-whole, refuse, regenerate cycle before finding that scaffold, fill-spec, fmt and explain already did the job (`docs/authoring-for-agents.md:3-9`).

The claim is that when the same checker that guards human commits is the agent's only write path, the agent's relationship to a system reduces to four boundary roles, and that this taxonomy is complete in the sense the bridge document asserts: each role is served by a projection of the compiler's existing knowledge (services, emission classes, guarantees G1-G9) and none needs new syntax (`docs/mcp-bridge.md:15-27`). The paper is a position paper: it argues the taxonomy and the interface, reports what is shipped, and is explicit that outcome evidence for agents is absent.

## Contributions

1. A four-role taxonomy of an agent at a language boundary (consumer, caller, operator, internal dependency), each tied to a shipped mechanism (`docs/mcp-bridge.md:15-27`).
2. The argument that replacing filesystem access with `check` and `admit` makes refusals first-class feedback: a rejection inside G1-G9 carries a stable code, the guarantee, and the mechanical fix, and a refusal outside G1-G9 is listed by code beside them (`tests/test_mcp_authoring_loop_1704.py::test_a_refusal_fails_its_guarantee_with_the_code_and_fix`, `::test_a_refusal_outside_g1_g9_is_listed_beside_the_rows`; `README.md:186-197`).
3. The authoring loop as an interface contract: reuse, scaffold, fill, preflight, check, admit, with holed skeletons that admission refuses until filled (`docs/authoring-for-agents.md:17-37, 91-113`).
4. A measured per-candidate admission cost, with its methodology (`bench/results/admission-latency.md`; guard test `tests/test_admission_latency.py:40`).
5. A specified (not yet evidenced) lighthouse loop: agent authors, admission refuses with a guarantee-naming diagnostic fed back, admitted component emitted to the TypeScript tier (`docs/v2.0-roadmap.md:1168-1217`).

## Related work

| Prior system or line of work | What it does | What revl does differently |
|---|---|---|
| Language Server Protocol (citation to be checked) | Editor-facing diagnostics and edits | The agent protocol also carries admission against a running composition and a hot swap, not only diagnostics |
| SWE-agent agent-computer interface (Yang et al.) (citation to be checked) | Designs shell-level commands for agents | revl removes the shell and file writes: changes to a composition go through gated verbs (`revl_admit`, `revl_swap`, `revl_edit`; `docs/mcp-bridge.md:3-9`), not file access |
| Aider repo-map (citation to be checked) | Summarizes code for context | revl's fill-spec supplies the checker's own knowledge at each hole: expected type, allowed crossings, reachable services (`docs/authoring-for-agents.md:44-60`) |
| Copilot Workspace (citation to be checked) | Plan, edit, test loop in a hosted workspace | The gate is a compiler guarantee set, not a test run |
| Model Context Protocol (citation to be checked) | Uniform tool wire | revl derives the tool safety hints from the method body instead of trusting author assertions (`README.md:194-197`; `docs/mcp-bridge.md` section 1) |
| Compiler-as-a-service (Roslyn) (citation to be checked) | Exposes compiler APIs to tools | The service is session-scoped, holds drafts server-side, and its verdict gates entry into a live composition |

## Evidence on main

Tree: revl main at commit 45bec431f.

| Claim | path:line or test | Status | Note |
|---|---|---|---|
| Four boundary roles, none needing a language feature | `docs/mcp-bridge.md:15-27` | shipped (doc) | Doc header says implemented; tests listed there: `tests/test_mcp.py` (47 tests), `tests/test_mcp_serve.py` (14), `tests/test_mcp_session.py` |
| Role 1, consumer: services projected to tool definitions | `docs/mcp-bridge.md:50-60` | shipped | |
| Role 2, caller: tools served from a booted composition | `docs/mcp-bridge.md:3-9`; `tests/test_mcp_serve.py` | shipped | Doc notes live-call tests skip without the cordis-py runtime |
| Role 3, operator: compiler as session | `README.md:186-197`; `docs/guide-ai-agents.md:314-315` | shipped | |
| Role 4, dependency inside: model as a service with emission operations | `docs/mcp-bridge.md:34-39` | shipped as a pattern | Uses ordinary language features; not run for this draft |
| Annotations derived from emission class, not asserted | `README.md:194-197`; `docs/mcp-bridge.md:55-60` | shipped | |
| Rejection carries code, guarantee, fix; nine guarantees as pass/fail/unchecked | `tests/test_mcp_authoring_loop_1704.py::test_a_refusal_fails_its_guarantee_with_the_code_and_fix` | shipped | |
| Initialize instructions name the loop in order | `tests/test_mcp_authoring_loop_1704.py` (module docstring, lines 1-15) | shipped | |
| Authoring loop: scaffold, fillSpec, fmt, explain, admit | `docs/authoring-for-agents.md:17-132` | shipped | `fmt` is IR-equivalence gated |
| Admission latency, 0.165 ms median | `README.md:198-204`; `bench/results/admission-latency.md:12-14` | shipped | One tracked run of the Python gate, in-process `compile_source(..., manifest=running)` round-trip for a small candidate against a two-component running composition, Apple M1 Max, Python 3.14.3: 0.165 ms median, 0.179 p90, 0.659 p99, 4000 samples. Not MCP transport latency. The README cites this figure (after #2764) |
| Guard test ceiling | `tests/test_admission_latency.py:40` (`CEILING_MS = 20.0`) | shipped | The test comment says the tight figure lives in the tracked result, CI only catches catastrophe |
| Lighthouse: agent authors .rvl, refusal fed back, admitted component emitted to ts | `docs/v2.0-roadmap.md:1168-1217` | designed | Roadmap text says "Shape to validate"; no test or example runs the loop |

## Numbers that exist

- Admission round trip: median 0.165 ms, p90 0.179 ms, p99 0.659 ms, min 0.158 ms, n = 4000 timed iterations after 100 warm-ups; compile-only baseline median 0.149 ms. Source: `bench/results/admission-latency.md`. One machine, one scenario (a Store provider plus an App consumer, candidate requires Store and provides Cache). The gate adds about 0.015 ms over compile alone, but the doc itself says to treat that split as a guide.
- Guard test ceiling 20 ms (`tests/test_admission_latency.py:40`).
- Test counts for the surface: 47 (`test_mcp.py`), 14 (`test_mcp_serve.py`), 18 (`test_mcp_authoring_loop_1704.py`). Counts of tests, not experiments.
- The module docstring of `tests/test_mcp_authoring_loop_1704.py` says an agent benchmark motivated the work but gives no figure; no number is cited here.

## Paper outline

1. Introduction: files and shell versus compiler as interface; the four roles in one table.
2. The four roles, one subsection each, mechanism and status.
3. The authoring loop and refusal as feedback: codes, guarantees, fixes, holes.
4. Cost: admission latency and what it means for a generation loop.
5. The lighthouse loop as a specified demonstration; status.
6. Related work.
7. Limits and what would falsify the position.

## Threats to validity and limits

- Taxonomy completeness is asserted by the project's own doc, not tested; a fifth role (for example the agent as a peer across a federation boundary) might exist.
- The latency number is one scenario on one machine: one tracked run of the Python gate, in-process, not MCP transport latency. The README states the tracked 0.165 ms median with that scope.
- No evidence that agents perform better; the interface may shift effort from runtime to refusal loops.
- The check only enforces what the language can express (G1-G9); extern host bodies sit outside it. The authoring doc says an untrusted author may not write an extern at all (`docs/authoring-for-agents.md:56-60`).
- Role 2 live-call tests skip without the cordis-py runtime (`docs/mcp-bridge.md:9-11`).
- The lighthouse loop is proposed; this draft does not claim it runs.

## Fact-check

Claims in this draft were checked against revl main at commit 45bec431f: 12 checked, 4 corrected.
