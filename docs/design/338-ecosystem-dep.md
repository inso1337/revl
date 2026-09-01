# 338: revl as a dependency for other agent infrastructure (the ecosystem step)

Design note for roadmap item 338 (`docs/v2.0-roadmap.md`, grep `^338\.`), the
last step of the embeddable-compiler arc (items 332 to 338). The ask: publish
the gate (item 332) as a real package so that OTHER agent infrastructure (MCP
servers, CI systems, agent frameworks) pulls in revl's admission gate as a
LIBRARY and admits agent-generated code in-process, instead of orchestrating the
`revl` CLI as a subprocess. The banner: revl as the safety kernel other tools
build on.

This is design-first and doc-only. It changes no compiler code. It records what
a downstream consumer imports, the stability contract they depend on, the
external-consumer example that is the exit test, the honest boundary (admission
is a compile-time decision, not a runtime sandbox), an adversarial self-review,
and a sliced plan that separates the achievable py-library path from the blocked
rust-crate path.

## Where 338 sits in the arc, and what is actually shippable

- **332 (partial, py landed)** ships the embeddable gate API and its packaging
  contract. `src/revl/gate.py` is the landed py surface: `admit`, `admit_into`,
  `compile_to`, `gate_version`, plus the layer-2 `Gate` session facade. The
  rust `revl-gate` crate, npm/wasm, and async multi-gate are deferred (332's own
  status line; the wasm cut is item 335, blocked on cordis-rs building on
  wasm32; the native single-binary is item 336).
- **333 (in-process gate)** proves an agent loop EMBEDS 332 and admits a batch
  of candidates in-process matching CLI verdicts. 333 is the in-process consumer
  in the SAME repo.
- **338 (this item)** is 333 taken one step outward: not a consumer we ship
  inside revl, but a THIRD-PARTY tool that declares `revl` as a dependency,
  installs it from a package index, imports `revl.gate`, and admits code. The
  deliverable is the published package plus a standalone external example that
  depends only on the installed package.

The dependency reality, stated up front so the plan is honest:

- **The py library path is achievable NOW.** `revl` is already a pip-installable
  distribution (`pyproject.toml`, `name = "revl"`, `version = "2.0.0"`,
  hatchling wheel over `src/revl`, with `backends/` and `stdlib/` force-included
  so an installed consumer has the full engine). `revl.gate` ships inside that
  wheel today. A third-party py tool can `pip install revl` and
  `from revl.gate import admit` this release. Slice 1 makes that path documented,
  versioned, and proven by an external example.
- **The `cargo add revl` rust-crate path is BLOCKED.** The native gate is the
  `revl-gate` crate, which does not exist: it is deferred in 332 and gated on
  the rust self-host frontier (items 336/337, and the native-run coverage
  frontier). `compile_to` on py can EMIT rust source, but there is no published
  rust CRATE that a rust `Cargo.toml` can depend on to call `admit` natively.
  This doc states that boundary plainly and scopes the rust path OUT of Slice 1.

## 1. The library surface: what a downstream imports

A downstream consumer imports the layer-1 verdict surface. It is pure (no disk,
no clock, no live state; strings in, structured strings out) and is the surface
another tool builds a gate on.

```revl
# sketch (py, not revl): what a third-party MCP server / CI check imports
from revl.gate import admit, admit_into, compile_to, gate_version, Verdict
```

The four functions and their contract (`src/revl/gate.py`):

- `admit(source: str) -> Verdict`. The frontend gate. Returns an admitted
  `Verdict`, or a refusal `Verdict` whose `code`/`message` are the reference
  compiler's diagnostic VERBATIM. The security clause is load-bearing and is
  built into this function: it calls `compile_source` AND `refuse_admission`
  (the open-obligation / typed-hole check), the SAME admission gate `revl run`
  applies, so it can never admit what `revl` refuses.
- `admit_into(source, manifest) -> Verdict`. Runtime admission against a running
  composition described by `manifest` (a previously compiled IR document). G2/G3
  span the running manifest, so a candidate that collides with the live
  composition is refused with the collision's why-trace. This is the same
  `compile_source(..., manifest=...)` call truc's gatekeeper and the item-144
  service make.
- `compile_to(source, tier) -> Emit`. Verdict plus, on admission, the emitted
  target `output` for a reference-backed tier (`py`, `ts`, `rust`, `java`,
  `wasm`, `go`). An unknown tier is a control verdict (`UNKNOWN_TIER`, fail
  closed), so a caller can never mistake a bad tier for an emission.
- `gate_version() -> {api, language, frontier}`. The versioned surface a
  downstream branches on. See section 3.

The two boundary VALUE types are part of the surface: `Verdict` (`admitted`,
`code`, `message`; strings only, so the same shape crosses a crate ABI or a wasm
boundary later) and `Emit` (`verdict` plus `output`).

The layer-2 `Gate` session facade (`load`/`admit`/`call`/`commit`/`abort`/
`unload`/`close`, plus `AdmitResult`, `Handle`, `GateError`, `GateRefused`, and
the re-exported `recover`) is stateful and address-space-bound. A downstream that
wants to RUN admitted turns under revl's commit/abort semantics uses `Gate`; a
downstream that only wants the admission DECISION uses layer 1. Item 338's own
exit test needs only layer 1 (the decision); `Gate` is documented as the
run-half a consumer opts into and is where the honest-boundary line (section 4)
matters most.

The stable-versus-internal split, stated as a rule a downstream can rely on:

- STABLE (the contract): the names in `revl.gate.__all__`
  (`Verdict`, `Emit`, `admit`, `admit_into`, `compile_to`, `gate_version`,
  `Gate`, `GateError`, `GateRefused`, `AdmitResult`, `Handle`, `recover`); the
  `Verdict.admitted` boolean and its `code` (codes are append-only, an existing
  code never changes meaning); the `Emit` two-field shape; the `gate_version()`
  three-key dict; the fail-closed behavior (a class-(c) crossing with no
  approver REFUSES, never silent auto-approval); the security clause (never
  admit what the reference refuses).
- NOT STABLE (internal, do not import): everything under `revl.compiler`,
  `revl.mcp`, `revl.holes`, `revl.errors`, and every `_`-prefixed name in
  `revl.gate` (`_verdict_from_error`, `_emit_for_tier`, `_TIER_DIRS`,
  `_ACTIVE_GATE`, and the rest). A downstream that reaches past `revl.gate` into
  `revl.compiler.compile_source` gets a working call TODAY but no versioning
  promise, which is precisely the wound item 330 named for the in-language
  direction and 332 closed for the host side. `Verdict.message` TEXT is
  explicitly not promised stable across versions (it is the reference why-trace
  verbatim at this version); a downstream keys on `admitted` and `code`, and
  treats `message` as human-readable output, never as a machine contract.

The gate is a LIBRARY, not a CLI to orchestrate. The difference is the whole
point of 338: a consumer holds the compiler in its own process and calls a
function, rather than spawning `revl compile`, parsing stdout, and paying
interpreter startup per candidate (332 measured the in-process round-trip at
0.165 ms; a subprocess is orders of magnitude above the decision it wraps).

## 2. The external-consumer example (the exit test)

Exit is a REAL third-party tool that declares `revl` as a dependency, installs
it, imports the package, and admits agent-generated code in-process, with its
in-process verdict matching the CLI verdict on the same source.

### py (achievable NOW): a standalone tool that depends on the installed package

The example is a tiny package OUTSIDE the revl source tree (its own directory,
its own `pyproject.toml` with `dependencies = ["revl>=2.0"]`), so it exercises
the ACTUAL dependency path: install `revl` from a package index (or a built
wheel), import `revl.gate`, admit. It stands in for the three named consumer
kinds without being any specific product:

```revl
# sketch (py, a third-party tool's own module): an MCP-server-shaped admitter
from revl.gate import admit, gate_version

class RevlAdmissionGate:
    """A safety kernel another tool embeds: every agent-proposed component is
    admitted before the tool will register or run it. No `revl` subprocess."""

    def __init__(self):
        # record the gate surface this tool was built against, for its logs
        self.gate = gate_version()   # {api, language, frontier}

    def check(self, proposed_source: str) -> bool:
        verdict = admit(proposed_source)
        if not verdict.admitted:
            # the refusal IS the repair signal; hand code+message back upstream
            self.reject(code=verdict.code, why=verdict.message)
            return False
        return True
```

Three consumer shapes, one library call, all built on `admit`:

- an MCP server that admits every tool a model proposes before exposing it,
- a CI check that fails a PR when a committed component no longer admits,
- an agent framework that admits each per-turn candidate inside its own loop.

The oracle (the exit assertion): for each source in a fixed corpus, the
example's in-process `admit(source).admitted` (and `code` on refusal) equals the
`revl compile` CLI verdict on the same source. This is the guarantee that makes
the library trustworthy as a kernel: an embedder gets the reference decision,
byte-for-byte on `code`, not an approximation. It is the item-338 escalation of
333's in-repo batch check: same oracle, but the consumer is an EXTERNAL package
that reached revl through `pip install`, not a module inside the tree.

The example lives under `examples/` (or a sibling published-example directory)
with its own `pyproject.toml`, a README showing `pip install revl` then run, and
a test that builds the revl wheel, installs it into a throwaway venv, installs
the example against it, and asserts the corpus oracle. The freshness discipline
already exists for the site wheel (`tools/check_site_wheel.py` builds a wheel and
compares member SHAs); the example's test reuses that build path.

### rust (`cargo add revl`): BLOCKED, stated honestly

The rust path is the identical shape (a `Cargo.toml` with `revl = "…"`, a
`use revl::admit;`, the same corpus oracle against `revl compile`) but it cannot
be built: there is no `revl-gate` crate published or buildable. It is deferred in
332 and gated on the rust self-host frontier (336/337) and the native-run
coverage frontier. `compile_to(src, "rust")` on the PY gate emits rust SOURCE,
which is not the same thing as a rust CRATE a downstream depends on to make the
admission DECISION natively. Slice 1 scopes rust out and records the two
concrete unblock conditions (the `revl-gate` crate exists; native admit covers
the target corpus) so a later agent knows exactly what closing the rust path
requires.

## 3. The stability contract: gate_version, semver, and what breaks a downstream

`gate_version()` returns three values a consumer branches on, and each answers a
distinct question:

- `api` (currently `"1.0.0"`, `GATE_API_VERSION`): semver of the gate SURFACE
  itself, bumped by surface changes only, independent of the language version. A
  downstream pins compatibility with the surface here.
- `language` (read from installed distribution metadata, e.g. `"2.0.0"`): the
  revl language/package version the gate admits. Two gates at the same `api` but
  different `language` may accept different programs.
- `frontier` (`reference-full:<language>` on py): an identifier of the COVERED
  surface. On py, layer 1 is backed by the FULL reference compiler, so the
  covered surface is the whole reference language. A native (rust) gate would
  pin the self-host corpus frontier instead. This is the field item 337's seam
  re-admission and any two-gate agreement check compares BEFORE trusting that
  two gates decide alike.

Semver of the public surface, as breaking-change rules a downstream can hold
revl to:

- BREAKING (major `api` bump): removing or renaming an `__all__` name; changing
  the meaning of an existing `Verdict.code`; changing the `Verdict`/`Emit`/
  `gate_version` shape; making the gate admit something a prior version at the
  same surface refused where the change is a genuine LOOSENING of admission (a
  security-relevant class change, see the adversarial review); changing the
  fail-closed default.
- NON-BREAKING (minor `api` bump): adding a new append-only `Verdict.code`;
  adding a new `__all__` name; adding a new reference-backed tier to
  `compile_to`; TIGHTENING admission to refuse something previously (wrongly)
  admitted (a security fix; see the critical below for why a downstream must not
  be able to opt out of it silently).
- `language`-only movement: an admission difference caused by a language/package
  version change with no `api` change is signaled by `language`, not `api`. A
  downstream that needs bit-for-bit reproducible admission pins BOTH `revl==X`
  (the package) and asserts `gate_version()` at boot.

Tie to the registry (item 293) and truc (the component manager):

- Item 293 makes a registry component carry a machine-verifiable EVIDENCE BUNDLE
  (attestation, gauntlet, fault-sweep, capabilities, provenance) and ranks
  candidates by evidence quality. The gate_version that admitted a component is
  provenance: recording `gate_version()` (all three keys) in the bundle lets a
  downstream RESOLVE (293's `revl_resolve`) know not just that a candidate was
  admitted, but by a gate covering WHICH frontier, at WHICH language and surface
  version. A candidate admitted by a stale gate (older `language`, missing a
  since-added refusal) is exactly the kind of lower-evidence candidate 293's
  ranking exists to sink below a freshly-admitted one.
- truc (the component manager) is itself the FIRST in-process consumer of the
  gate machinery that already ships: `truc assemble` admits every fetched bout
  through revl's own gate in-process (`revl.truc._host.admit_all` calls
  `compile_files`). Today truc reaches the raw `compile_files` internal, not the
  versioned `revl.gate` surface. 338's stable surface is exactly what truc (and
  every external tool) SHOULD depend on instead, so that truc's gate call and a
  third-party tool's gate call are the same declared, versioned contract. Moving
  truc onto `revl.gate` is a natural follow-on (noted, not in Slice 1).

## 4. Honest boundary: the library gives the decision, not a sandbox

The single most tempting overclaim in this arc, drawn precisely so a consumer is
not misled:

- The layer-1 library gives the COMPILE-TIME ADMISSION DECISION. `admit` tells a
  consumer whether the reference compiler accepts the source AND whether the
  admission gate would let it run. That is a decision, a `Verdict`. It is not
  execution and it is not confinement.
- Running the admitted code is the CONSUMER'S responsibility, in the consumer's
  own process, with the consumer's own privileges. revl does not sandbox the
  runtime of admitted code from layer 1 alone. `src/revl/gate.py`'s own module
  docstring states it: "The gate cannot and does not claim to confine its host;
  its guarantees govern the ADMITTED code."
- The witnessed, revertible RUNTIME (accept-and-revert, residue-free abort) is a
  DIFFERENT capability: it is the layer-2 `Gate` session facade plus the
  witnessed-effect runtime (items 243/244/245/322), which item 334 embeds. A
  consumer that wants "admit AND run under revert" opts into `Gate`, supplies an
  `approver` for class-(c) crossings, and accepts the fail-closed contract. A
  consumer that only calls layer-1 `admit` and then executes the code itself has
  the admission guarantee and NOTHING about runtime effects.
- Concretely for the three named consumers: an MCP server that admits a tool
  with `admit` and then runs it has a well-typed, gate-approved tool, but if it
  runs that tool with ambient filesystem/network authority, revl did not grant
  that authority and cannot revoke it. The authority story is the manifest and
  `admit_into` (what the component may REACH is a compile-visible property);
  turning a reach-refusal into a runtime confinement requires the consumer to
  run under the witnessed runtime, not merely to have called `admit`.

## 5. Adversarial self-review

Four attacks on this design, then the critical.

**Attack A: a downstream pins an old gate_version that misses a security fix.**
A consumer pins `revl==2.0.0` and asserts `gate_version()["api"] == "1.0.0"` at
boot for reproducibility. revl later TIGHTENS admission (a minor `api` bump) to
refuse a class of unsafe program the old gate wrongly admitted. The pinned
consumer keeps admitting the unsafe program forever, and its own logs say
"admitted, gate 1.0.0" with full confidence. The pin turned a security fix into
a no-op for exactly the consumers who most wanted determinism. Reproducibility
and security are in tension and this design has to say which wins. Mitigation:
(a) a security-relevant TIGHTENING is documented as the one class of admission
change allowed under a minor bump, and the changelog machinery (item 261) that
already computes the semver bump from the manifest diff headlines it as a
security line, not a footnote; (b) `gate_version()` is a runtime read, so a
consumer's "assert exact version" is a CHOICE the docs steer away from toward
"assert `api` compatible AND log `language`"; (c) the honest limit: revl cannot
force a downstream to upgrade. What it can do is make the stale-gate condition
VISIBLE (293 provenance ranks a stale-gate admission lower; the frontier string
detects surface skew). This attack is real and only partly closable, and the
doc says so rather than pretending a pin is safe.

**Attack B: the "stable API" leaks an internal that then changes.** A consumer,
following an example that was slightly too clever, imports
`revl.compiler.compile_source` directly (it works, it is faster to reach, the
example even used it once). A later refactor changes `compile_source`'s
signature or its exception types. The consumer breaks on a revl PATCH release,
and blames the "stable gate API" that it never actually depended on. Mitigation:
section 1's stable-versus-internal split is explicit that ONLY
`revl.gate.__all__` is the contract; the example in section 2 imports strictly
from `revl.gate` and nothing deeper; the boundary exception types are
`GateError`/`GateRefused` precisely so a consumer never has to catch an internal
`RevlError`/`SessionError`. The residual risk is that Python cannot enforce the
private boundary (a determined consumer can still import internals); the defense
is documentation plus example hygiene, and a test in the example package that
asserts it imports nothing outside `revl.gate`.

**Attack C: a consumer calls the library but ignores or misreads the verdict.**
The gate is only a kernel if the consumer HONORS the verdict. A consumer that
calls `admit`, gets `admitted=False`, and runs the code anyway has bypassed the
whole point; subtler, a consumer that checks `if verdict:` (a `Verdict` object
is always truthy) instead of `if verdict.admitted:` treats every refusal as an
admission and runs everything. The library did its job and the consumer defeated
it. Mitigation: `Verdict` is a deliberately small, explicit shape with a boolean
named `admitted` (not an implicit truthiness); the example checks
`verdict.admitted` and the docs call out the truthiness trap by name; the
refusal carries the repair signal as `code`/`message` so honoring it is the easy
path. The honest limit: no library can force a consumer to obey its own return
value. This is why section 4's boundary matters: the library is a decision a
consumer must ACT on, and 338 documents that acting on it is the consumer's job.

**Attack D (the transitive-import attack surface): the py package ships the
whole compiler, not just the gate.** `pip install revl` to get `revl.gate`
drags in the ENTIRE `revl` distribution: `revl.mcp`, `revl.run`, every backend
under `backends/`, `stdlib/`, the truc component manager, and their transitive
imports. A downstream that wanted a small admission kernel now has the full
compiler, every backend emitter, and the MCP bridge in its process and on its
dependency-audit surface. Every one of those modules is code a supply-chain
review of the consumer now has to account for, and any import-time side effect
in any of them runs in the consumer's process. The "safety kernel" is shipped
inside a large, general-purpose distribution.

**The critical (D is the critical).** Attack D is the one that most undercuts
the item's own framing. 338 sells revl as "the safety kernel other tools build
on," and a kernel's credibility is inversely proportional to its attack surface.
Shipping the whole compiler to deliver a four-function admission gate means the
kernel's trusted computing base is the entire distribution, which is the
opposite of a kernel. Worse, it interacts with Attack A: the larger the shipped
surface, the more places a security-relevant change can hide from a consumer who
pinned a version. The mitigation for Slice 1 is honest scoping, not a false
claim of minimality: (1) the layer-1 functions are already lazy-import
(`admit` imports `compile_source` only when called; `compile_to` loads a backend
only for the requested tier), so nothing under `revl.mcp`/`backends` is imported
at `import revl.gate` time unless the consumer uses the run-half or emits, which
bounds the RUNTIME surface even though the on-disk INSTALL surface is the whole
package; (2) the doc states plainly that `pip install revl` today installs the
full distribution and that a minimal `revl-gate` extract (a slim package whose
install surface is only the gate and its true dependencies) is FUTURE work, tied
to the same crate/extract effort the rust path needs; (3) `gate_version().api`
is the surface a consumer's audit pins, so the audited CONTRACT is four
functions even while the installed BYTES are the distribution. Naming this
gap is the deliverable; closing it (a slim published `revl-gate`) is explicitly
deferred and recorded as the highest-value follow-on for the ecosystem story.

## 6. The sliced plan

**Slice 1 (achievable now, py library surface + external consumer):**

1. DOCUMENT `revl.gate` as the versioned public surface for downstream
   consumers: the stable-versus-internal split (section 1), the semver rules
   (section 3), the honest boundary (section 4). This is a docs pass over an
   ALREADY-LANDED surface (`src/revl/gate.py`); it adds no compiler code.
2. BUILD the external-consumer example (section 2): a standalone py package
   OUTSIDE `src/revl`, its own `pyproject.toml` with `dependencies =
   ["revl>=2.0"]`, importing only `revl.gate`, shaped as an admission-gate a
   real MCP server / CI check / agent framework would embed.
3. PROVE the oracle: a test that builds the revl wheel, installs it into a
   throwaway venv, installs the example against it, and asserts that the
   example's in-process `admit` verdict (`admitted` and `code`) equals the
   `revl compile` CLI verdict on a fixed corpus. Reuse the wheel-build path from
   `tools/check_site_wheel.py`.
4. RECORD `gate_version()` in the example's startup log, demonstrating the
   version-assertion pattern section 3 recommends (assert `api` compatible, log
   `language`), so the example also documents the stability contract by using
   it.

Exit for Slice 1: an external package that `pip install`s revl and admits code
in-process, matching CLI verdicts on the corpus. That is item 338's exit test on
the achievable (py) tier.

**Explicitly BLOCKED / DEFERRED (out of Slice 1, recorded so it is not lost):**

- The `cargo add revl` rust-crate path (section 2): blocked on the `revl-gate`
  crate not existing (deferred in 332, gated on 336/337 and the native-run
  frontier). Unblock conditions: the crate exists and builds; native admit
  covers the target corpus.
- npm / wasm consumers: item 335 (design done, impl gated on cordis-rs building
  on wasm32).
- The slim `revl-gate` EXTRACT that fixes the critical (Attack D): a published
  package whose install surface is only the gate and its real dependencies, not
  the whole compiler. Highest-value ecosystem follow-on; tied to the same
  extract/crate effort the rust path needs.
- The registry (293) / truc integration: recording `gate_version()` in the 293
  evidence bundle as provenance, and moving truc's `admit_all` from the raw
  `compile_files` internal onto the versioned `revl.gate` surface. Natural
  follow-ons, deferred; noted in section 3.

## Exit tests (for an implementing agent)

1. An external py package, outside the revl tree, with `dependencies =
   ["revl>=2.0"]`, imports only `revl.gate` and admits a corpus in-process; its
   `admit(source).admitted`/`.code` equals the `revl compile` CLI verdict for
   every source in the corpus.
2. The example's own test asserts it imports nothing outside `revl.gate`
   (Attack B / D discipline).
3. The example logs `gate_version()` and demonstrates the assert-`api`,
   log-`language` pattern (section 3).
4. The doc states the rust-crate path is blocked and names both unblock
   conditions (section 2), so the boundary is explicit and not silently skipped.
