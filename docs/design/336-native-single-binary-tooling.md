# 336: native single-binary tooling (the LSP + checker as a rust binary)

Design note for roadmap item 336 (`docs/v2.0-roadmap.md:4305`), part of the
embeddable-compiler arc (items 332-338, arc header at
`docs/v2.0-roadmap.md:3935`). The ask: ship the language server and checker as a
native rust single binary embedding the self-hosted front end, so an editor gets
fast diagnostics/hover/definition from one distributable file with no Python to
install, and that file ships the ACTUAL compiler rather than a reimplementation
that drifts. The item's exit test: a standalone rust LSP binary answers
`publishDiagnostics` / `hover` / `definition` matching `python -m revl.lsp` on a
corpus.

This is design-first. It changes no compiler code and adds no `src/` behavior;
it records the one tension this item cannot wish away (a checker in native rust
that matches the reference EVERYWHERE is item 391, the full self-host compiler,
which is deferred and done last), the honest split between what a slice-1 binary
ships WITHOUT the native checker and what genuinely waits on 391, the protocol
boundary the rust binary owns on either side of that split, the editor-specific
fail-closed rule that keeps the binary sound, the distribution and trust
questions a redistributed binary raises, a staged plan, an adversarial review
that names the one CRITICAL and closes it, and exit tests an implementation agent
can pick up the day the frontier allows.

## The tension, stated before anything else

The item text asks for "the language server and checker as a native rust binary
embedding the self-hosted front end ... the ACTUAL compiler, not a
reimplementation that drifts," and its dependency line reads "Depends on 332 +
native-run on rust (ready today)." Read quickly, that says: the self-host
`admit` already runs natively on rust (332 background: `admit` runs end to end on
rust, made viable by item 284's clone-elision), 332 designed the `revl-gate`
crate that packages it, so a rust LSP on the native checker is startable now.

That reading is a trap, and naming it is most of this design's job. The
self-hosted front end does not cover the full reference language. Its coverage is
the conformance `revl` column (`docs/conformance.md:122`, "revl conforming to
itself"), the self-host frontier: `ok` rows are byte-identical to the reference,
`lim` rows are constructs the native pipeline runs but does not yet reproduce
faithfully, "or its own gate declines the construct" (`conformance.md:136`). The
335 design snapshots that frontier at 23 `ok` / 36 `lim`
(`docs/design/335-wasm-edge-gate.md`). A checker that reproduces the reference on
ALL of it, every diagnostic, every message, every rejection, is precisely the
deliverable of item 391 ("the self-host is a FULL CURRENT-LANGUAGE compiler for
v3," `docs/v2.0-roadmap.md:4473`), which is multi-wave and explicitly done last.

So this design must split the item cleanly along that line:

- **What a slice-1 rust binary can ship WITHOUT the full native checker**: the
  native LSP protocol server (the JSON-RPC/stdio loop, the document store, the
  capability negotiation, the exact wire shapes `revl.lsp` emits) as pure rust,
  plus editor integration and single-file distribution TODAY, with the analysis
  itself (diagnostics/hover/definition content) produced by the REFERENCE front
  end embedded in the binary, so it matches `python -m revl.lsp` by construction.
- **What genuinely depends on 391**: replacing that embedded reference with a
  native-rust checker that matches `python -m revl.lsp` across the WHOLE
  language. That is not an optimization the arc can schedule freely; its
  soundness (match the reference everywhere) IS 391's deliverable, and until 391
  lands there is no native checker that can be the LSP's sole diagnostics engine
  without hiding errors the reference would show.

The rest of this note holds those two apart at every turn, and the adversarial
review's CRITICAL is exactly the failure that follows from NOT holding them
apart.

## What the reference LSP is (the oracle the exit test names)

`python -m revl.lsp` (`src/revl/lsp/__main__.py`) is a shipping, human-facing
language server (`src/revl/lsp/server.py`, `analysis.py`). It is the exit test's
oracle, so what it consults fixes what "matching" means:

- **Diagnostics** come from `compile_source`, the CLI's own entry point, run to
  its first rejection (or, under item 386, its full multi-refusal carrier), each
  `RevlError` mapped through `diagnostics.classify` (`analysis.py:40-66`). This
  is the FULL reference checker, the whole language, every G-class refusal, not a
  frontier subset.
- **Hover** is two halves: for a diagnostic under the cursor it returns
  `diagnostics.explain(code)`, the same guarantees-and-fixes text `revl explain`
  prints (`analysis.py:185-235`); over a declared name it reads the parser's AST
  (`build_symbols`, `analysis.py:207` onward).
- **Definition** reads the parser AST symbol table (`analysis.py:239` onward),
  resolving the innermost enclosing scope.

Two consequences fix the design. First, the diagnostics oracle is the FULL
reference front end, so "match `python -m revl.lsp` on a corpus" means match the
full language, which is why the native-checker half is 391-gated and not a
free-scheduling optimization. Second, definition and the symbol half of hover are
PARSER-based, and the self-host parser runs natively on rust and is
byte-agreement-tested against the reference parser on the corpus (the item-391
oracle family, `tests/test_selfhost_parser.py`); this is the one capability where
a native path is sound over the covered frontier before 391 completes, and the
staged plan uses that, carefully.

The protocol surface the server advertises and answers (from `server.py`):
`initialize` / `initialized` / `shutdown` / `exit`, the document lifecycle
`didOpen` / `didChange` / `didClose` (full-document sync, capability `1`), the
`publishDiagnostics` push on every open and edit, and the requests
`textDocument/hover`, `textDocument/definition`, and `textDocument/codeAction`.
The item names diagnostics/hover/definition as the exit surface; code actions
(item-274 navigable refusals, the item-386 fixgen) ride the same dispatch and are
noted where the plan reaches them.

## Ground truth: the assets and the walls (checkable in the tree today)

Five facts fix the design space.

**1. The self-host `admit` runs natively on rust, and covers only the frontier.**
`selfhost/compile.rvl:75` exports `admit(source)` driving the fully native
lex/parse/check/lowering-admission chain; it runs on rust end to end
(`tools/bench_selfhost_rust.py`, items 283/284, `docs/bench-selfhost.md`), and
332 stage 3 mechanizes it as the committed `revl-gate` crate. Its verdict is `""`
on admission or `"<TAG>|<message>"` on refusal. But its faithful coverage is the
conformance `revl` column, not the reference language: outside the frontier the
native pipeline either declines the construct as out-of-surface (a parse/check
limitation, a DIFFERENT diagnostic than the reference's real semantic one) or
lacks the check entirely (NO diagnostic where the reference has one). That second
case is the whole hazard of this item (the adversarial review's CRITICAL).

**2. The 332 `revl-gate` crate is the rust front-end dependency, and it is
`admit`-only.** 332's rust packaging ships v0 exporting `admit` (the half that
runs natively today); `compile_to` and `admit_into` are staged/refused
(`docs/design/332-embeddable-gate-api.md`, rust packaging section; the 333
dependency table). The crate is COMMITTED generated rust source that "must build
with no Python on the machine ... for 336's sake as much as 338's" (332, rust
packaging point 2, and the arc-relationship line naming 336 by name). So the rust
binary's native front-end input exists as a designed, Python-free build unit;
what it does NOT yet give is full-language coverage or the run half.

**3. The reference checker is Python, and a single-file binary that embeds it
needs a bundled interpreter.** The reference `compile_source`/`explain`/`Parser`
are the wheel's Python. To put them inside ONE rust file with "no Python to
install," the binary embeds a private interpreter (pyo3 over a
`python-build-standalone` runtime, or a PyOxidizer-style self-contained build)
with the `revl` wheel frozen in. This is a real, mature technique, not
speculative, and it is heavy: tens of megabytes, a bundled CPython, a slower cold
start than a pure-rust binary. The honest reading of "no Python dependency" for
slice 1 is therefore "no Python for the user to INSTALL; a private interpreter is
bundled inside the single distributable file," NOT "no Python anywhere." The
distinction is load-bearing and the adversarial review treats a naive "just shell
out to `python -m revl.lsp`" as a hidden dependency that breaks the single-file
claim.

**4. Definition and symbol-hover are parser-shaped, and the self-host parser is
native and corpus-agreed.** `build_symbols` parses and walks declarations; the
self-host `parser.rvl` runs natively on rust and is held byte-identical to the
reference parser on the corpus by the 391 oracle. So over the covered frontier,
definition and the symbol half of hover CAN be answered by native rust without
the reference, and identically to it, before 391 is complete. Diagnostics and the
explain half of hover cannot, because they are checker-shaped and the checker is
the frontier-bounded half.

**5. The rust ecosystem has the LSP plumbing; nothing here reinvents a protocol
stack.** `tower-lsp` / `lsp-server` (the `lsp-types` crate) give the JSON-RPC
framing, capability types, and the stdio loop as maintained rust, the same
posture 335 took toward rustc as a wasm compiler ("a production toolchain we do
not have to build"). The binary owns its dispatch and its wire shapes; it does
not hand-roll JSON-RPC.

## The slice split (the heart of the item)

Capability by capability, engine by engine, with each slice's exact dependency.
The columns are the two engines the analysis can run on: REFERENCE (the full
Python front end, embedded) and NATIVE (the self-host front end via the
`revl-gate` crate, frontier-scoped).

| capability            | reference engine (Python, embedded) | native engine (rust self-host)              | slice | depends on                                  |
|-----------------------|-------------------------------------|---------------------------------------------|-------|---------------------------------------------|
| LSP protocol loop     | n/a (pure rust)                     | n/a (pure rust)                             | 1     | nothing (rust `lsp-server` + `revl.lsp` wire shapes) |
| document lifecycle    | n/a (pure rust)                     | n/a (pure rust)                             | 1     | nothing                                     |
| `publishDiagnostics`  | full-language, exact match          | frontier only; missing squiggles off it     | 1 (ref) / 2 (native, gated) / 3 (native, sole) | 1: bundled reference. 2: 332 crate `admit`. 3: **391** |
| `hover` (explain)     | full-language, exact match          | frontier only                               | 1 (ref) / 3 (native, sole) | 1: bundled reference. 3: **391**            |
| `hover` (symbol) / `definition` | full-language, exact match | frontier, corpus-agreed, sound now          | 1 (ref) / 2 (native) | 1: bundled reference. 2: 332 crate + native parser |
| `codeAction` (fixgen) | full-language, exact match          | frontier only                               | later | reference; native follows diagnostics       |

### Slice 1 (startable now, no 391): the single binary with the reference embedded

The binary is native rust for everything that is NOT the checker, and embeds the
reference front end for the analysis. Concretely:

- **Pure rust, 391-independent**: the JSON-RPC/stdio serve loop, the per-URI
  full-text document store (`didOpen`/`didChange`/`didClose`), the capability
  negotiation (`initialize` advertising exactly what `revl.lsp` advertises), the
  request routing, the `publishDiagnostics` push cadence, and the byte-exact wire
  shapes for `Diagnostic`, `Hover`, and `Location`/`LocationLink` that
  `src/revl/lsp/protocol.py` produces. This is the "native single binary" the
  item names, and it is fully buildable today.
- **The analysis engine is the reference, embedded**: diagnostics, hover-explain,
  hover-symbol, and definition are computed by calling
  `revl.lsp.analysis.compute_diagnostics` / `compute_hover` / `compute_definition`
  in the bundled interpreter, so the binary's answers are the reference's,
  verbatim. It MATCHES `python -m revl.lsp` by construction, because it IS
  `revl.lsp`, hosted in a rust process rather than launched by a Python one. The
  exit test passes on slice 1, soundly, over the full language.
- **What slice 1 delivers**: editor integration and single-file distribution
  today (a marketplace plugin ships one binary per platform; the developer
  installs no Python), the rust protocol layer the later slices reuse unchanged,
  and a machine-checkable oracle harness (the binary's answers versus
  `python -m revl.lsp`) that every later slice runs against.
- **What slice 1 does NOT deliver, stated plainly**: a native-rust checker. The
  analysis is still the reference engine, merely bundled. And "no Python
  dependency" is delivered as "no Python to install" (fact 3): the file carries a
  private interpreter, so it is large and its cold start is interpreter-bound. A
  design that claimed a small pure-rust checker here would be claiming 391.

Slice 1's honest name is "the native LSP shell with the reference engine
frozen in," and it is the whole of what ships before the frontier moves.

### Slice 2 (332-only native acceleration, still NOT 391): the sound native subset

With the `revl-gate` crate (332 stage 3) and the native parser in hand, two
things can move to native rust WITHOUT waiting on 391, because they are sound over
the covered frontier:

- **`definition` and the symbol half of `hover`** run on the native parser for
  ALL inputs: the self-host parser is corpus-agreed with the reference, and where
  it cannot parse a construct it yields no symbol (exactly `build_symbols`'s own
  parse-failure behavior, `analysis.py`: "a parse failure yields an empty table
  rather than raising"), which for definition/hover is a benign "resolve
  nothing," not a wrong answer. This removes the interpreter from the
  navigation-hot path.
- **`publishDiagnostics` on the native `admit`, but ONLY as an ACCELERATOR under
  the agree-or-add rule below, never as the sole engine.** For a document the
  frontier pin proves fully covered, native `admit` can produce the diagnostics
  at native speed; for anything else, the embedded reference produces them. The
  reference is NOT removed in slice 2; it remains the fallback and the
  correctness oracle. The value bought is speed on the in-frontier hot path, not
  Python-removal.

Slice 2 is where the CRITICAL lives, because the tempting shortcut is to let
native `admit` be the diagnostics engine outright. The next section's rule and
the adversarial review forbid that.

### Slice 3 (the full native checker, no Python anywhere): GATED ON 391

Only when the self-host front end is a full current-language compiler (item 391)
does a native-rust checker exist that reproduces `python -m revl.lsp`'s
diagnostics and explain-hover across the WHOLE language. At that point:

- the diagnostics and explain-hover engines move to native rust for all inputs;
- the bundled interpreter is dropped, the binary becomes small and pure-rust, and
  "no Python dependency, the ACTUAL compiler" is finally true in the strong
  sense the item's prose reaches for;
- the "reimplementation that drifts" risk is answered structurally, because the
  native checker is the SELF-HOST source (co-compiled, byte-agreement-tested),
  not a hand-written rust port.

Slice 3 cannot start before 391 for a reason that is not scheduling but
soundness: its exit condition (match the reference everywhere) is identical to
391's deliverable. Any attempt to ship it earlier ships an LSP that hides errors
outside the frontier. This is stated so no implementer reads slice 3 as "hard but
doable now."

## Fail-closed for an editor: the missing-squiggle rule

332's security clause is "never admit what the reference refuses." An LSP has no
`admit` verb, so the clause must be projected onto the editor surface, and the
projection INVERTS which direction is dangerous.

In an editor, the analog of admission is a CLEAN diagnostics result: green,
no squiggle, "this compiles." So:

- A binary that shows a squiggle the reference does NOT (a false positive) is an
  inconvenience: the developer sees a spurious error, investigates, finds the
  code is fine. Annoying, not unsafe.
- A binary that FAILS to show a squiggle the reference DOES (a missing
  diagnostic, green where the reference refuses) is the editor's false-admit: the
  developer sees green, believes the code is admitted, ships it, and `revl run`
  or CI then refuses it, or worse the divergence sits in an admission-relevant
  check. Because the binary is sold as "the ACTUAL compiler," the developer
  trusts its green more than a linter's. This is the 332 clause violated at the
  editor surface.

So the binary's diagnostics contract is: **show every diagnostic the reference
shows; you may show more, never fewer.** The native engine is only ever an
ACCELERATOR against that contract:

1. Native diagnostics are trusted only for a document the frontier pin
   (`gate_version().frontier`, 332's versioning surface) proves fully covered.
2. For any document not proven covered, the embedded reference computes the
   diagnostics, and the native result is discarded.
3. Where native and reference are both computed (the CI oracle, and optionally a
   belt-and-braces runtime cross-check), any disagreement resolves toward the
   reference (MORE squiggles), never toward native (fewer), and a native result
   with FEWER diagnostics than the reference on the same input is a
   release-blocking defect, exactly the false-admit direction 332 makes a release
   blocker.
4. The embedded reference is never removed until slice 3 (391), because until
   then it is the only engine that can honor the contract off the frontier.

This is the same asymmetry 332 and 335 drew ("a gate that refuses what the
reference admits is an inconvenience; a gate that ADMITS what the reference
refuses is the defect class this arc exists to prevent"), re-pointed for a
surface whose "admit" is silence.

### Versioning and the stale-binary skew

A single distributable binary pins one language version and one frontier into an
artifact that a plugin marketplace redistributes and developer machines cache. A
stale binary keeps issuing diagnostics from an old language version: green where
the CURRENT reference refuses (a skew-induced missing squiggle), or a squiggle
where the current reference admits. This is the edge-skew failure 335 named and
337 polices at seams, sharpened because a binary is a more freely redistributed,
longer-lived artifact than a wheel a package manager version-resolves.

336 owes the same thing 335 owed: a detectable version surface, not a solved skew
problem. The binary exposes `gate_version()` (`{api, language, frontier}`) over a
custom LSP request (`revl/gateVersion`) and in its `initialize` `serverInfo`, so
an editor client, a CI check, or a fleet audit can compare the binary's language
and frontier against what it expects before trusting its greens. Solving skew
(forcing currency) is out of scope and belongs to distribution (338) and the
seam-policing 337 does; making skew detectable is in scope and delivered here.

## What authority the binary has: a checker, not a runtime

Mirroring 335 §4, so no one reads more into a native binary than is there. The
LSP DIAGNOSES; it does not run, admit-into-a-live-composition, hold a session,
prompt an approver, or enforce anything.

- **No layer 2.** The item is diagnostics/hover/definition, all of which are
  functions of source text (parse + check). The stateful `Gate` facade (load,
  admit, call, commit, abort) and the 245/246/322 machinery are a runtime; they
  are not in this binary. Admit-and-run at native cost is 334's lane, and the
  serialized `Verdict` that travels between a diagnosing tier and a running tier
  is 332's shape, inherited unchanged.
- **The binary executes no source it analyzes.** Diagnostics are parse + check;
  there is no `eval`, no import of the document as a module, no running of its
  bodies (the same invariant 333's adversarial review pinned for `admit`). A
  malicious document cannot execute at diagnosis time; an `extern` body is
  surfaced (G8), never run by the checker.
- **Distribution and trust.** The binary is a redistributed artifact: a
  marketplace serves it, machines cache it, and (unlike the wheel a package
  manager version-resolves) nothing forces currency. The trust story is (a) the
  binary is built from committed generated crate source at a pinned sha (332's
  build-with-no-Python discipline), reproducibly, so what it contains is
  auditable; (b) `gate_version` makes its coverage and version legible to a
  client; (c) artifact signing/attestation is a distribution-model adjacency
  (335 deferred it by name; 338 owns the publish), noted, not started here.

## Use cases, grounded in what each slice supports

- **An editor plugin that ships one binary per platform (slice 1).** The user
  installs the plugin; no Python, no `pip`, no virtualenv. The binary answers
  diagnostics/hover/definition identically to `python -m revl.lsp` because it
  embeds it. This is the headline distribution win, and it lands in slice 1
  without touching the frontier.
- **A fast navigation path (slice 2).** Definition and symbol-hover answer from
  the native parser with no interpreter on the hot path, for all inputs, soundly
  (parse-failure yields no symbol, never a wrong one). Diagnostics accelerate on
  the in-frontier hot path with the reference as the off-frontier fallback and
  the correctness oracle.
- **The pure-rust, no-Python tool (slice 3, gated on 391).** The end-state the
  item's prose describes: a small native binary whose checker IS the self-host
  compiler, matching the reference everywhere, no bundled interpreter. This is
  real only when 391 makes the self-host full-coverage.

What none of these get from 336: running effects (334/411), admission into a live
composition (`admit_into`, native gap), `compile_to` at the edge (332 stage 4),
or full-language native diagnostics before 391.

## Staged plan

Each slice lands independently; every existing golden and `revl.lsp` behavior
stays byte-identical throughout (the additivity discipline). Location decision at
implementation: a `crates/revl-lsp/` top level beside the 332-designed
`crates/revl-gate/`.

- **Slice 0 (the go/no-go spike).** Stand up a rust `lsp-server` binary that
  embeds the reference via a bundled interpreter (pyo3 + a
  `python-build-standalone` runtime + the frozen `revl` wheel) and answers
  `initialize` + one `publishDiagnostics` on a smoke document, byte-identical to
  `python -m revl.lsp`. The known risk is the embed itself (interpreter bundling,
  wheel freezing, binary size); if it will not build single-file on a target, the
  recorded fallback is the honest "requires a `revl` runtime alongside" framing
  for that target, taken in the open, never a silent shell-out. Output: a spike
  note, a yes/no per target, measured binary size and cold-start latency.
- **Slice 1 (the native LSP shell + reference engine, the shippable binary).**
  The full protocol loop, document lifecycle, capability negotiation, and the
  exact `revl.lsp` wire shapes in rust; diagnostics/hover/definition/codeAction
  routed to the embedded reference. The oracle harness: for a corpus of
  documents, the binary's responses to `publishDiagnostics`/`hover`/`definition`
  equal `python -m revl.lsp`'s, byte-identical (satisfies the item's exit test).
  CI drift/skip discipline inherited from 332 (skip-with-a-reason where the
  bundling toolchain is absent, never a hollow green).
- **Slice 2 (the sound native subset).** Definition and symbol-hover on the
  native parser (via the `revl-gate` crate's parser surface) for all inputs;
  native `admit` diagnostics as an accelerator on the frontier-proven-covered hot
  path under the agree-or-add rule, reference fallback and oracle retained. Exit:
  the oracle harness still byte-matches `python -m revl.lsp`, AND a
  frontier-spanning corpus proves the native path never yields FEWER diagnostics
  than the reference (the release-blocking direction has zero rows).
- **Slice 3 (deferred, GATED ON 391): the full native checker.** Move diagnostics
  and explain-hover to native rust for all inputs; drop the bundled interpreter;
  the binary is small and pure-rust. Exit: the oracle harness byte-matches
  `python -m revl.lsp` across the FULL corpus with no interpreter present. Cannot
  start before 391 (soundness, not scheduling).
- **Deferred and named, not implied:** artifact signing/attestation (distribution
  adjacency); the `codeAction`/fixgen native path (follows diagnostics, so it
  trails slice 3 for the checker-derived actions); `admit_into` / live-composition
  diagnostics (native gap, 334's lane); any editor client packaging beyond the
  binary itself (338's ecosystem step).

## Adversarial review

Four attacks. The CRITICAL is A1.

### A1 (CRITICAL): the "startable now" native-checker LSP is a silent editor false-admit

The item's dependency line ("332 + native-run on rust, ready today") tempts the
obvious slice 1: build the LSP directly on the native self-host `admit`, because
it runs on rust today and needs no interpreter, delivering the small pure-rust
binary the prose describes right now. This is the design's single most dangerous
move, and it is UNSOUND.

The native front end covers only the conformance `revl` frontier (23 `ok` / 36
`lim`). For a document using any construct outside it, native `admit` either
declines it as out-of-surface (a different diagnostic than the reference's real
semantic one) or, in the hazard case, simply has no check for it and returns
`""` (admitted, no diagnostic). In the editor that second case is a MISSING
squiggle: the developer sees green on code the reference refuses. Because the
binary is sold as "the ACTUAL compiler," the developer trusts the green, ships,
and `revl run`/CI refuses. This is 332's "never admit what the reference refuses"
clause violated at the editor surface, and it fails the exit test in the silent
direction: "matching `python -m revl.lsp` on a corpus" would pass on an
in-frontier corpus and hide the divergence, so a naive corpus would green a
broken binary.

Worse, the "fix" an implementer reaches for under exit-test pressure is to prune
the corpus to the frontier so the match passes, which bakes the unsoundness into
the test. That is the same failure shape 333's CRITICAL found (oracle wired to
the wrong question, then "fixed" into a security regression).

**Resolution (mandatory in the design, and it is why the slice split is shaped as
it is).** Slice 1's diagnostics engine is the REFERENCE, embedded, so the binary
matches `python -m revl.lsp` by construction over the FULL language, not the
frontier. The native checker is demoted from "slice 1's engine" to "slice 2's
accelerator," admitted only where the frontier pin proves the document covered,
under the agree-or-add rule (show every reference squiggle; never fewer), with a
release-blocker on any native result that yields FEWER diagnostics than the
reference on the same input. The embedded reference is never removed until slice 3
(391), the point at which a native checker genuinely matches the reference
everywhere. And the exit-test corpus MUST span constructs OUTSIDE the frontier (a
frontier-crossing corpus is a required exit condition below), so that a binary
which quietly ran native-only would fail loudly on exactly the documents where it
hides errors, turning A1 from a silent trap into a test the exit criteria enforce.

### A2: "no Python dependency" hides a subprocess that breaks the single-file claim

A second tempting slice 1: keep the reference engine but reach it by shelling out
to `python -m revl.lsp` as a child process. This "matches the reference" trivially
and needs no interpreter bundling. But it reintroduces exactly the Python
dependency and the process-orchestration the item exists to remove (332's opening
problem: "a framework embedding revl this way orchestrates subprocesses"), and it
is not one distributable file. A slice 1 built this way is not startable AS
SPECIFIED; it only looks startable.

**Assessment and resolution.** The single-file, no-install requirement is met by
EMBEDDING the interpreter (fact 3: pyo3 + `python-build-standalone`, or a
PyOxidizer-style build), verified in slice 0 as the go/no-go, with the honest cost
recorded (binary size, cold start). Where a target genuinely cannot bundle
single-file, the design records the honest "requires a `revl` runtime alongside on
that target" fallback in the open, never a silent shell-out presented as the
single binary. The design states plainly that slice-1's "no Python dependency"
means "no Python to install; a private interpreter is bundled," so the claim the
binary makes is the claim it can keep.

### A3: the stale redistributed binary issues verdicts from an old language

A binary is cached on developer machines and redistributed by a marketplace with
nothing forcing currency (unlike a version-resolved wheel). A stale binary shows
green where the CURRENT reference refuses (skew-induced missing squiggle) with the
authority of "the actual compiler."

**Assessment and resolution.** In the threat model, and mitigated the way 335
mitigated its edge twin: `gate_version()` (`{api, language, frontier}`) is exposed
over `revl/gateVersion` and in `initialize` `serverInfo`, so a client, a CI gate,
or a fleet audit can detect skew before trusting the binary's greens. Forcing
currency is 338's (distribution) and 337's (seam) job; making skew detectable is
delivered here. The design does not claim to solve skew, only to make it legible.

### A4: native definition/hover drifting from the reference off the frontier

Slice 2 moves definition and symbol-hover to the native parser for ALL inputs. Off
the frontier the native parser may parse a construct differently, or fail to parse
it, so a symbol the reference parser would resolve is missed.

**Assessment and resolution.** For navigation this is the benign direction: a
missed symbol yields "resolve nothing" (a no-op jump / empty hover), exactly the
reference's own parse-failure behavior (`build_symbols`: "a parse failure yields
an empty table rather than raising"), never a WRONG jump to the wrong
declaration. The agree-or-add rule does not apply to navigation the way it applies
to diagnostics (a missing squiggle is unsafe; a missing jump is merely
unhelpful), so native navigation over the corpus-agreed parser is sound to ship in
slice 2. The oracle harness still checks it against the reference and files any
WRONG-location result (not a missing one) as a parser byte-agreement defect
against 391's oracle, never worked around in the LSP.

## Exit tests

- **The item's own exit (satisfied by slice 1).** A standalone rust LSP binary,
  run on a machine with no Python installed, answers
  `publishDiagnostics`/`hover`/`definition` byte-identically to
  `python -m revl.lsp` over a corpus. Slice 1 satisfies it by construction
  (embedded reference); slices 2 and 3 must preserve it as they move engines.
- **The frontier-crossing corpus (the A1 enforcement).** The exit corpus MUST
  include documents whose refusals lie OUTSIDE the self-host frontier (the `lim`
  rows and beyond), and the binary must show the reference's diagnostics on them.
  A binary that quietly ran native-only fails here, loudly, on exactly the inputs
  where it would hide errors. This corpus is a required exit condition, not an
  optional extension.
- **The missing-squiggle rule (the release blocker).** Over a frontier-spanning
  corpus, the binary NEVER yields fewer diagnostics than `python -m revl.lsp` on
  the same input; a native accelerator result with fewer diagnostics than the
  reference is release-blocking. Zero rows in the fewer-than-reference direction,
  ever.
- **Single-file, no-install.** The binary runs and answers on a machine with no
  Python and no `revl` installed; its `gate_version()` is reachable over
  `revl/gateVersion` and reports `{api, language, frontier}`.
- **Native navigation soundness (slice 2).** Definition/symbol-hover on the native
  parser return either the reference's location or nothing, never a wrong
  location, across the corpus.
- **Skew detectability.** A binary built at an old sha reports an older
  `language`/`frontier` through `gate_version()`, and the CI/audit check flags the
  mismatch against the expected pin.
- **Additivity.** The full suite, every backend golden, and `revl run`/`test`/
  `mcp`/`python -m revl.lsp` behavior are byte-identical with the binary present;
  nothing in `src/revl` changes for a user who never runs the binary.
- **Drift and honesty.** Rebuilding the binary from the same sha is reproducible;
  the build/oracle jobs skip with a stated reason (bundling toolchain or cargo
  absent), never a hollow green.
- **`test_doc_examples` stays green**: every code block in this note is rust, WIT,
  json, shell, or a table, and none is a `revl` fence, so nothing here is expected
  to compile until the feature lands.

## The honest hard part (consolidated)

Four costs, taken in the open. First, the native-checker half of this item is item
391 wearing a different hat: a rust checker that matches `python -m revl.lsp`
EVERYWHERE is the full self-host compiler, so slice 3 is not startable now, and
any slice-1 that ships the native checker as its diagnostics engine hides real
errors outside the 23 `ok` / 36 `lim` frontier, an editor false-admit that
violates the arc's load-bearing clause (A1). Second, the startable slice buys
distribution, not a native compiler: slice 1's binary is the reference engine
frozen inside a rust protocol shell, "no Python to install" bought with a bundled
interpreter that makes the file large and interpreter-cold-started, and pretending
slice 1 is the small pure-rust tool the prose describes would be selling 391.
Third, a single redistributed binary is a stale-verdict hazard a version-resolved
wheel is not, and 336 delivers detectability (`gate_version` over
`revl/gateVersion`), not a solution, leaving forced currency to 337/338. Fourth,
the sound native wins available before 391 are narrow and real, native
definition/symbol-hover on the corpus-agreed parser, and native diagnostics as a
frontier-gated accelerator under the never-fewer-squiggles rule, and the
temptation this whole design resists is to widen that narrow, sound subset into
the unsound "just use the native checker now" that the item's own dependency line
seems to invite.
