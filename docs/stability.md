# Stability & compatibility

A version number on a package index is a promise. This document makes revl's
promise explicit — what you can build on, what is versioned, and what may move
without notice — so that `pip install revl` is a contract and not a surprise.

The short version: **the v1 IR is frozen and byte-identical; the language and
the tooling around it are still moving; the six runtime tiers are experimental
to varying degrees.** Read on for exactly which is which.

## What is stable

**The v1 backend IR is frozen.** `ir_version` is `1`, and the v1 contract
([docs/backend-ir-v1.md](backend-ir-v1.md)) does not change. The frontend is
the single producer of IR and emitters accept v1 only — there is no
compatibility window to maintain because there is only one producer. If your
tool consumes revl's v1 IR, it will keep parsing.

**v1 goldens are snapshots, not a freeze.** Every emitter has a checked-in
golden for the reference composition (`backends/<tier>/golden/user_cache.<ext>`),
and the suites assert the emitter reproduces it **byte for byte**. The invariant
that enforces is "emitter output never changes *unreviewed*", never "output
never changes": regenerating a golden plus reviewing its diff is always an
acceptable resolution (docs/conformance.md, "Golden policy"). This used to be a
hard freeze — "a change that alters emitted v1 output by a single byte fails
the suite and does not land" — which read like a downstream promise while
protecting nobody (the emitters had no external consumers) and bending emitter
design toward byte-stability instead of correctness. If you have pinned a revl
version, re-emitting the same source with that release produces the same bytes,
which is what lets a downstream system cache, diff, or reproduce emitted
artifacts *within a pinned release*; across releases, emitter output may change
reviewed. Byte-freezing may return — deliberately, as a versioned promise —
when a real external consumer appears.

**The guarantees are the stable contract of the language.** G1–G9, the `Secret`
families (`G-SECRET`, `G-SECRET-FLOW`), the typing rules T1–T3 and the lifecycle
rules A1, A2, A3, A5, A6, A8 and A9 define what "it compiled" means. The set is
generated from `revl.diagnostics.GUARANTEES` into [DESIGN.md](../DESIGN.md) §4
and [rejections.md](rejections.md). A program that compiles satisfies them; that is the promise the whole
project exists to keep, and it does not weaken across releases. Where a
guarantee is known to be incompletely enforced, it is named in
[docs/contract-errata.md](contract-errata.md) rather than silently trusted —
the errata is part of this contract, and "sound where declared, loud in the
errata where not" is the standard.

## What is versioned

**The IR is versioned, and the version is the compatibility boundary.**
`ir_version` tiers separate what a backend must handle:

- **`ir_version` 1** — the frozen v1 component core (effect/inverse, config +
  defaults, emissions, the boundary surface). Described in
  [docs/backend-ir-v1.md](backend-ir-v1.md).
- **`ir_version` 2** — component extensions on top of v1 (realms, async
  services, and the rest of the 2.0 component surface).
- **`ir_version` 3** — the pure/typed-core tier (functions, types, ADTs,
  `match`, loops, host blocks). Described in
  [docs/backend-ir-v3.md](backend-ir-v3.md).

Emitters gate on `ir_version` and refuse a document whose version they do not
implement, with a clear error rather than a silent misemit — a backend that
sees an unsupported version raises, it does not guess. New language capability
arrives as a new IR version or as additive fields within one, never as a
silent change to the meaning of an existing v1 construct.

**The package version.** revl is published as `revl` on PyPI. Releases are cut
from tags and gated on the same CI that gates `main` (see
[.github/workflows/](../.github/workflows)) — a version on the index is a claim
that the full suite passed. Until the API surface below stabilizes, treat the
package as pre-1.0 in spirit even though the language core it ships is "v1":
pin an exact version if you depend on tooling behavior, not just on emitted
bytes.

## What may break without notice

These are **not** part of the compatibility promise. If you build on them, pin a
version and read the release notes:

- **The `.rvl` surface syntax beyond the frozen core.** The v1 component
  language is stable; the 2.0 language on top of it
  ([docs/syntax-2.0.md](syntax-2.0.md)) is still converging — the typing
  frontier, stdlib surface, and newer forms tracked in
  [docs/v2.0-roadmap.md](v2.0-roadmap.md) can change. A construct may become
  stricter as a known typing hole is closed (that is soundness work, and it is
  expected to reject programs a looser checker accepted).
- **Diagnostic text.** Error messages are a deliverable and their *content*
  matters (each names its guarantee and the fix), but the exact wording is not
  a stable API. Do not regex a human-readable message. For structured
  consumption use `revl compile --json-diagnostics`, whose diagnostic *codes*
  and *guarantee* fields are the intended machine surface — though those are
  still stabilizing too.
- **The CLI flags and subcommands.** `revl compile`/`audit`/`run`/`test`/`mcp`
  and their options are useful today but not frozen; new subcommands and
  renamed flags are expected while the tooling matures.
- **The MCP tool surface.** `revl mcp serve`'s tool names, schemas, and hint
  derivation ([docs/mcp-bridge.md](mcp-bridge.md)) are evolving with the
  agent-facing design.
- **Runtime-tier maturity.** The tiers are not equally hardened.
  [cordis-py](https://github.com/geohotstan/cordis-py) is the reference tier
  every construct is checked against first; the TypeScript, Rust, Java, Go, and
  wasm tiers each carry their own gaps, documented per tier in
  [docs/conformance.md](conformance.md) and
  [docs/contract-errata.md](contract-errata.md). A tier's *runtime* behavior can
  change as its upstream Cordis port moves — revl pins the versions it targets,
  but "runs on tier X" is a research claim backed by tests, not a support
  commitment.
- **Anything under `bench/`, `dogfood/`, `demo/`, `playground/`, `tck/`, and
  `selfhost/`.** These are harnesses and experiments, not a public API.

## How compatibility is enforced

None of the above is trust-me. The invariants are mechanically checked on every
change:

- the goldens are asserted byte-identical in the default suite — a change
  that alters emitter output without regenerating and reviewing the golden
  fails, and a regenerated golden carries its reviewed diff (snapshot policy,
  docs/conformance.md);
- the rejection suite (`examples/rejections/` + `tests/test_frontend.py`
  `REJECTIONS`) is the executable, *exhaustive-by-test* definition of what the
  checker refuses;
- the conformance matrix hands each tier's emitted output to that tier's real
  compiler, so "it emits" and "it compiles" are separately gated;
- and a release only publishes if that whole suite is green.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the pre-commit contract and the
full suite, and [SECURITY.md](../SECURITY.md) for reporting a soundness escape.
