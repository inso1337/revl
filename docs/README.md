# revl documentation

Start with **[../DESIGN.md](../DESIGN.md)** for the guarantees and the checked
table, or **[vision.md](vision.md)** for what this is *for*. The
[project README](../README.md) is the front page; this is the full map.

## Language

- [syntax-2.0.md](syntax-2.0.md) — the full 2.0 language reference
- [stdlib-2.0.md](stdlib-2.0.md) — the specified stdlib surface
- [function-types.md](function-types.md) · [holes.md](holes.md) · [capabilities.md](capabilities.md) — the newer type-system surface
- [generics.md](generics.md) — implicit and explicit `[T]` type parameters
- [strings.md](strings.md) — a `Str`'s code-point unit and `Float` rendering · [arithmetic.md](arithmetic.md) — `/`, `%`, named integer ops and `Int32`
- [wasm-capabilities.md](wasm-capabilities.md) — the substrate tier's capability matrix and its hard refusals
- [collections.md](collections.md) — deterministic (sorted) `Map` iteration · [records.md](records.md) — functional record update and block-bodied match arms
- [namespacing.md](namespacing.md) — namespaced provision keys
- [design-v2-realms.md](design-v2-realms.md) · [design-v2-instances.md](design-v2-instances.md) — realms, interception, instances

## Command and MCP reference

- [commands-reference.md](commands-reference.md) — every subcommand, with flags
- [mcp-reference.md](mcp-reference.md) — the MCP verbs, per-verb inputs and outputs
- [mcp-bridge.md](mcp-bridge.md) — the compiler as an MCP server, full shapes
- [guide-ai-agents.md](guide-ai-agents.md) — the agent-facing workflow guide
- [authoring-for-agents.md](authoring-for-agents.md) — the authoring loop for an agent that writes revl: scaffold → fillSpec → fmt → explain → admit
- [harness-gate-guide.md](harness-gate-guide.md) — driving the 245/246 approval gate from a harness: the three action classes, the MCP verbs, the commit two-step, and the `session.state()` metrics

## Testing and guarantees

- [conformance.md](conformance.md) — every construct against every tier, the generated matrix, and how emitted code is validated
- [fault-tests.md](fault-tests.md) — L-Raise / no-residue as a language form
- [verified-effect.md](verified-effect.md) — inverse round-trip testing · [prop-test.md](prop-test.md) — property tests with type-derived generators
- [replay.md](replay.md) — backwards replay over the accumulator
- [contract-errata.md](contract-errata.md) — known runtime divergences, per tier
- [selfhost-findings.md](selfhost-findings.md) — the self-hosted front end as a differential oracle

## Working in a live system

- [plan.md](plan.md) — a dry run for admission · [apply.md](apply.md) — execute a plan artifact · [swap.md](swap.md) — migrate a live component across tiers
- [deploy.md](deploy.md) — attested admission with re-hash-on-receive, peer-authenticated seam correlation, and the coordinated cross-process commit/abort
- [composition-bootstrap.md](composition-bootstrap.md) — a composition manifest that declares its own file list, and the two-stage host bootstrap that gets it running
- [environment-binding.md](environment-binding.md) — the `boot` component: the declared, bounded, audited contract for the values (port, token, data dir, model provider) the host must inject before the composition exists
- [queries.md](queries.md) — ask the composition questions
- [why-traces.md](why-traces.md) — derivations behind a rejection · [why-runtime.md](why-runtime.md) — cause chains for a recorded run
- [crash-recovery.md](crash-recovery.md) — WAL roll-forward/back · [persistence.md](persistence.md) — snapshot/restore an evolved session · [erase-report.md](erase-report.md) — right-to-erasure evidence

## Agents and interop

- [gauntlet.md](gauntlet.md) — graded admission · [registry.md](registry.md) — find a component to import
- [import-openapi.md](import-openapi.md) · [import-wit.md](import-wit.md) · [import-cordis.md](import-cordis.md) · [wit-bridge.md](wit-bridge.md) — importers and the WIT bridge
- [interchange-format.md](interchange-format.md) — the manifest + G8 audit format · [interop-bridge.md](interop-bridge.md) — cross-tier interop

## Internals and project

- [backend-ir-v1.md](backend-ir-v1.md) · [backend-ir-v3.md](backend-ir-v3.md) — the IR contract
- [v2.0-roadmap.md](v2.0-roadmap.md) — what is done and what is in flight
- [stability.md](stability.md) — what a version number promises
- [gate-dependency-contract.md](gate-dependency-contract.md) — the security contract for a host that `pip install`s revl and calls `revl.gate` directly, and the promised `revl.gate.__all__` surface
- [../CONTRIBUTING.md](../CONTRIBUTING.md) — the wave/worktree workflow and the pre-commit contract · [../SECURITY.md](../SECURITY.md) — reporting a soundness escape
