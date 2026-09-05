# 336. Native single-binary tooling (LSP + checker as a rust binary)

Status: PARTLY IMPLEMENTED. Slices 1 and 2 ship as `crates/revl-lsp`, against
the `crates/revl-gate` crate this note treats as absent; `crates/revl-lsp/README.md`
is the current account of what is native and what is not. Slice 3 is still gated on
item 391. The §"Honest status" block below carries the same correction inline.
Originally: DESIGN, doc only. Depends on item 332 (embeddable-gate API) and
native-run on rust (item 266/270/278 lineage). Reference source this design is
grounded in: `src/revl/lsp/` (the Python LSP the binary must match),
`selfhost/*.rvl` (the front end the binary embeds), `backends/rust/emit.py`
(how a self-host stage is emitted to rust), `tools/bench_selfhost_rust.py` (the
emit -> assemble -> cargo build -> run path that already works for the lexer),
`src/revl/gate.py` (item 332's verdict surface), and item 391 (the self-host
parity frontier).

The headline promise: editor tooling that ships the ACTUAL compiler (the
self-host front end compiled to rust), not a second implementation that drifts.
This doc's job is to make that promise precise about what "the actual compiler"
covers, name honestly the two places where the promise does NOT hold today, and
slice the work so the first landable increment is real.

---

## 1. Architecture

The rust LSP binary has two parts, and only ONE of them is "the compiler":

```
+------------------------------------------------------------------+
|  revl-lsp  (one native rust binary, no python)                   |
|                                                                  |
|  +------------------------+     +---------------------------+    |
|  |  LSP PROTOCOL SHELL    |     |  THE EMITTED FRONT END    |    |
|  |  (hand-written rust)   |     |  (selfhost/*.rvl -> rust  |    |
|  |                        |     |   via backends/rust/emit) |    |
|  |  - Content-Length      |     |                           |    |
|  |    framed JSON-RPC     |     |  lexer -> parser ->       |    |
|  |  - document store      | --> |  checker -> lower         |    |
|  |    (uri -> full text)  |     |                           |    |
|  |  - didOpen/didChange/  | <-- |  the SAME passes revl     |    |
|  |    didClose dispatch   |     |  compile runs; a rejected |    |
|  |  - request -> response |     |  program cannot compile,  |    |
|  +------------------------+     |  enforced here            |    |
|                                 +---------------------------+    |
+------------------------------------------------------------------+
```

The **protocol shell** is a native port of `src/revl/lsp/protocol.py`
(Content-Length framing over a byte stream) and `src/revl/lsp/server.py` (the
`didOpen`/`didChange`/`didClose` full-text document store and the
method-to-handler dispatch). Both py files are already hand-rolled with no
third-party dependency (protocol.py header comment: "Hand-rolled, with no
third-party dependency"), so the rust shell is a faithful transliteration of a
small, closed surface. It is not emitted from revl and never was: it is I/O and
wire framing, not compilation.

The **emitted front end** is `selfhost/lexer.rvl -> parser.rvl -> checker.rvl
-> lower.rvl`, compiled to rust through `backends/rust/emit.py` and cargo-built,
exactly as `tools/bench_selfhost_rust.py` already does for the benchmark. This
is the load-bearing claim: the checker that decides publishDiagnostics IS the
self-host checker, byte-for-byte the same code path `compile.rvl::admit` runs
on py and rust today.

### What answers each verb

| LSP verb | Reference (py) source of truth | Native binary source |
| --- | --- | --- |
| `publishDiagnostics` | `compute_diagnostics` -> `compile_source` refusals, each mapped by `diagnostics.classify` (analysis.py:41) | the self-host front end's refusal(s), positioned + classified (see §3, §5) |
| `hover` | guarantee text via `diagnostics.explain`; symbol type via `build_symbols` parser-AST signatures (analysis.py:334) | a portable `explain` DATA table + a parser-AST symbol surface (see §5-A3, Slice 2) |
| `definition` | `build_symbols(...).resolve(word, line)` -> declaration line + column (analysis.py:388) | the same symbol surface (Slice 2) |

The insight that shapes the whole design is that publishDiagnostics is almost
entirely the compiler's own output, while hover and definition are a PROJECTION
the py LSP builds in `lsp/analysis.py` and `lsp/document.py` on top of the
parser AST. The compiler-owned part ships honestly; the projection part is
where a reimplementation would live, and therefore where drift hides (§5-A1).

---

## 2. The MATCH guarantee

Exit test (from the roadmap): a standalone rust LSP binary answers
publishDiagnostics/hover/definition matching `python -m revl.lsp` on a corpus.

### The differential harness

`python -m revl.lsp` is the reference: an editor drives it over stdio, and it
is deterministic and stateless over the document text (server.py: "the analysis
is stateless over that text"). The harness drives BOTH servers through the same
synthetic LSP session and compares the returned messages:

```revl
// sketch (not a compiling program): the harness shape, one corpus doc
//  1. didOpen { uri, text = <corpus doc> }
//       -> reference publishDiagnostics  R_diag
//       -> native    publishDiagnostics  N_diag        assert R_diag == N_diag
//  2. for each probe position P in the doc:
//       hover      { uri, position: P }  ->  R_hov, N_hov   assert equal
//       definition { uri, position: P }  ->  R_def, N_def   assert equal
```

Equality is on the JSON-RPC `result` payloads: for diagnostics the
`{range, severity, code, source, message}` list AND its ORDER (multi-error
order is observable, item 386); for hover the `contents.value` markdown and the
`range`; for definition the `{uri, range}` Location.

This reuses the wire discipline item 332 already designed for a native gate:
`gate.py`'s `Verdict.from_native(wire)` parses a native verdict string
`"<code>|<message>"` "through the SAME shape; the py package does" (gate.py:115
comment), and gate.py:99 anticipates the verdict shape crossing "a crate ABI
(335/336) or a wasm-component boundary unchanged". The LSP diagnostic is a
strict superset of that verdict (it adds a position and a severity), so the
harness compares the same structured fields on both sides rather than raw
bytes.

### The corpus

Two families, both scoped to the self-host frontier (§4):

1. **Diagnostics corpus** (Slice 1): refused programs, at least one per
   guarantee tag the self-host checker/lowering emits (`tagged("G1", ...)`,
   `tagged("G4", ...)`, `tagged("HANDOFF", ...)` in `selfhost/lower.rvl`), plus
   multi-error documents (several independent refusals in one file) so the
   ordering and completeness of the squiggle list is under test, and clean
   programs (empty diagnostics, how the editor clears stale squiggles).
2. **Symbol corpus** (Slice 2): programs whose declarations (fn/extern/type/
   service/component/param/let) are probed for hover and definition at fixed
   positions.

The diagnostics corpus SHOULD be derived from, not merely adjacent to, the
existing self-host byte-identity corpus (`tests/test_selfhost_*.py`), so a
program the compiler cross-check already trusts is the same program the LSP
cross-check trusts. Item 391 warns that a corpus that does not exercise a
feature leaves the oracle green while the implementation lacks it; §5-A4
carries that warning into this harness.

---

## 3. The item-332 dependency

332 is the "foundation of this arc": emit `selfhost/` as a real consumable
library per tier with a STABLE `admit(source) -> verdict` /
`compile_to(source, tier) -> output` surface, "so a host program imports the
revl admission gate as a native function." 336 is a host program that imports
exactly that.

### What 332 must expose for 336

- A rust `revl-gate` crate that links the emitted self-host front end and
  exposes `admit`. The LSP binary depends ONLY on that crate (mirroring 332's
  own exit test: "a standalone Rust binary depending ONLY on the published
  crate calls `admit`").
- A verdict that carries a **source position**. This is the gap. 332's
  `Verdict` is `{admitted, code, message}` (gate.py:102, `__slots__`) with NO
  line or column. The py LSP does not read the verdict at all for positioning:
  it reads `error.line` off the raised `RevlError` (errors.py:18; analysis.py
  `_range_for` at :76 uses `error.line`) and then tightens the range onto a
  backticked token via `find_symbol_column`. A wire verdict of
  `"<code>|<message>"` cannot be positioned into a squiggle. So the embeddable
  surface 332 designed is NECESSARY but NOT SUFFICIENT for the LSP: 336 requires
  a positioned diagnostic on the embeddable boundary, e.g.
  `diagnose(source) -> List[{line, code, message}]`, not just a scalar verdict.

### Honest status

> **Superseded in part.** This section was written when 332 was python-only.
> `crates/revl-gate` has since landed, generated from the self-host sources by
> `tools/build_gate_crate.py`, and `crates/revl-lsp` slices 1 and 2 ship against
> it. The first blocker below is resolved; the second is not. The current status
> lives in `crates/revl-lsp/README.md`, which is written against what the binary
> actually does.

332 was `◑ PY IMPL LANDED` when this was written: the PYTHON library facade
(`src/revl/gate.py`) existed and was tested, while the rust `revl-gate` crate
was deferred and not built. Therefore:

- ~~**Blocked on 332's rust half.**~~ **Resolved.** `crates/revl-gate` exists and
  the binary depends on it (`revl_gate::symbols` answers `definition` and the
  signature half of `hover` with no interpreter on the path). No inlining of the
  emitted self-host was needed.
- **Blocked on a widened verdict shape.** STILL TRUE. The positioned,
  multi-error diagnostic the LSP needs is not on 332's surface: `revl.gate`
  exports `admit` / `admit_into` / `compile_to` / `gate_version` and no
  `diagnose`, and the crate's non-refusing arm is `NoObjection`, which says only
  that this gate found nothing it could refuse. So the checker in
  `crates/revl-lsp` is still the reference front end, forwarded verbatim, and a
  native refusal is ADDED to the publish rather than replacing it. 336 must drive
  that addition, and it belongs on 332's tier-agnostic boundary (so the wasm gate
  in 335 inherits it) rather than being invented privately inside the LSP binary.

---

## 4. The self-host coverage gap (item 391)

The binary is only as complete as the self-host front end. Item 391 is explicit
that the self-host has fallen behind the reference and is a catch-up in
progress: "the self-host lexer does not lex 0x/<</& ... the self-host currently
CANNOT compile a program using the ~15 features added this session."

Consequence for the LSP, stated plainly: on a document that uses an
out-of-frontier construct (a hex literal, a bitwise operator, `break`/
`continue`, `.map`/`.filter`, string interpolation, and the rest of 391's
list), the self-host lexer/parser will refuse where the REFERENCE admits. In an
editor that is not a missing feature, it is a WRONG red squiggle on valid code.
That is the most user-visible way "ships the actual compiler" can betray the
user, and it is a MATCH failure by construction.

Two obligations follow:

1. **Scope the corpus to the frontier.** The MATCH guarantee (§2) is only
   claimed over the surface the self-host front end covers today
   (`selfhost/compile.rvl`'s covered rows, the frontier `gate_version()` would
   pin for a native gate, gate.py:272). The binary is honest for that surface
   and silent outside it only if §5-A2's loud-failure rule holds.
2. **Ride 391, do not fork.** Every construct 391 ports into the self-host
   front end automatically widens the LSP frontier, at zero LSP cost, because
   the binary embeds that front end. 336 must NOT reimplement a construct in
   the shell to paper over a self-host gap: that recreates the drifting second
   implementation the whole item exists to avoid (§5-A1). The correct backlog
   entry for "the LSP does not understand hex literals" is the 391 lexer port,
   not an LSP patch.

---

## 5. Adversarial self-review

Four attacks, then the critical.

### A1. The binary drifts from the py reference despite "ships the actual compiler"

The claim is that the binary IS the compiler, so it cannot drift. Look at what
is actually NOT emitted from revl and therefore CAN drift: the protocol shell
(§1), and, more dangerously, the LSP PROJECTION in `src/revl/lsp/analysis.py`
and `document.py`. That projection is not compiler output; it is editor-facing
logic the py LSP wrote on top of the compiler:

- range tightening onto the first backticked identifier, whole-line fallback
  otherwise (`_range_for`, `_first_backticked`, `_whole_line_span`);
- the `classify` code + severity mapping and the `explain` guarantee/fix table
  (`src/revl/diagnostics.py`);
- the symbol table, scope spans, and signature rendering (`build_symbols`,
  `_fn_signature`, `_scopes_of`).

If the rust shell reimplements any of this by hand, it is exactly the drifting
reimplementation item 336 forbids, just relocated from the checker to the
projection. Mitigation: treat the projection as data or as emitted-from-revl
wherever it is constant. The `explain` table is language-version-constant
(GUARANTEES/FIXES in diagnostics.py), so it ships as an embedded data table
generated from the py source, not as re-typed rust. The range-tightening rule
is a tiny pure function that SHOULD be ported into the self-host front end (so
it too is emitted, not re-typed) or, failing that, pinned hard by the
differential harness. This attack is real but containable, and the containment
is: minimise hand-written rust to the wire shell, push everything else to
emitted or generated artifacts.

### A2. The self-host front end is missing a construct, so the LSP gives wrong diagnostics

Covered in §4: an out-of-frontier construct makes the binary red-squiggle valid
code. The danger is not that it happens (391 is closing it) but that it happens
SILENTLY. Rule: the binary must fail LOUDLY on a construct the self-host lexer/
parser cannot represent. A lexer that hits an unknown byte class must surface
"this construct is outside the native tooling frontier" as a distinct,
recognisable diagnostic, never as a generic syntax error that the user will
read as "my code is wrong." The differential harness cannot catch this alone,
because it only runs in-frontier documents (§2); the loud-failure behaviour is
its own targeted test with an out-of-frontier document asserting the frontier
diagnostic, not a false syntax error.

### A3. Hover/definition need type info the self-host checker does not expose

The py hover shows a symbol's signature (`_fn_signature`, `_extern_signature`,
`_type_signature`) and resolves a name to its declaration line+column through
`build_symbols` / `SymbolTable.resolve`. The self-host parser produces an AST
internally, but `selfhost/*.rvl` exposes NO symbol-table query, no decl-line
lookup, and no signature renderer on its public surface (compile.rvl exposes
only `admit` and `compile_to`). So hover and definition require NEW self-host
surface: a queryable declaration/scope projection emitted to rust, plus the
`explain` data table from A1. This is precisely why Slice 1 defers hover and
definition: publishDiagnostics is compiler output the self-host already
computes; hover/definition are a projection the self-host does not expose yet.
Reaching for them prematurely forces the A1 reimplementation.

### A4. The corpus does not cover the drift

The differential harness only proves agreement on the documents in the corpus.
A construct that is inside the frontier but absent from the corpus can diverge
while the harness stays green: this is the exact failure mode item 391 names for
the byte-agreement oracles ("a feature the corpus does not exercise leaves the
oracle green while the self-host lacks it"). Mitigation: derive the diagnostics
corpus to hit every guarantee tag the self-host emits (enumerate the `tagged(...)`
call sites in `selfhost/lower.rvl`, one refusing document each), add multi-error
documents, and fuzz WITHIN the frontier (mutate corpus documents through the
same in-frontier generators the self-host cross-check fuzzers already use) so
coverage is a property, not a hope.

### THE CRITICAL

The sharpest finding is not a drift risk; it is a structural mismatch that
makes publishDiagnostics UN-matchable today, corpus-independent:

**The self-host / 332 admission surface is a single first-refusal wire verdict
WITHOUT a position, while the LSP reference produces positioned, classified,
MULTI-error diagnostics.**

Two independent halves of this gap:

1. **No position.** `compile.rvl::admit` returns `""` or `"<TAG>|<message>"`
   and 332's `Verdict` is `{admitted, code, message}` (§3). Neither carries a
   line. The py LSP positions every squiggle from `error.line`, a field the
   embeddable surface drops. Without a line the binary cannot draw a squiggle at
   all, so it cannot match publishDiagnostics on even one refused document.
2. **First refusal only.** `admit_src` stops at the first rejection and returns
   one verdict. The py LSP shows EVERY squiggle at once: `compute_diagnostics`
   reads `error.errors` (the item-386 multi-refusal carrier) and returns a list
   (analysis.py:52). On any document with two independent errors the binary
   would show one squiggle where the reference shows two, a guaranteed MATCH
   failure that no amount of corpus tuning fixes.

The line numbers exist inside the self-host front end (its tokens carry
`line`, e.g. `tkc(ts, i).line` in `selfhost/lower.rvl`), and multi-error
collection is a known reference feature (item 386). So the fix is not
research, it is a self-host surface widening: expose a positioned, multi-error
diagnostic list from the self-host front end (a `diagnose(source) ->
List[{line, code, message}]` alongside `admit`), and port item 386's collect
into the self-host checker/lowering. This is a 391-family self-host change, and
it is the true first task of 336. It reframes Slice 1: the smallest landable
increment is NOT an LSP shell over today's `admit`; it is this diagnostic
surface, without which no LSP shell can match the reference.

---

## 6. Sliced plan

Ordering reflects the critical: the self-host diagnostic surface comes first,
because everything downstream depends on it.

### Slice 0 (prerequisite, self-host surface, 391-family)

Extend the self-host front end to emit a POSITIONED, MULTI-ERROR diagnostic
list: `diagnose(source) -> List[Diag]` where `Diag = { line, code, message }`,
carrying the token line already threaded through the stages, the guarantee tag
already produced by `tagged(...)`, and the message. Port item 386's collect so
independent refusals accumulate instead of stopping at the first. Cross-check
against `compile_source`'s `.errors` list on the diagnostics corpus. This lands
in `selfhost/*.rvl` + `tests/test_selfhost_*.py` and is the honest content of
"the smallest landable step," because Slice 1 cannot begin without it.

### Slice 1 (smallest landable 336): publishDiagnostics only

- Emit the self-host front end (through `diagnose`, Slice 0) to rust via
  `backends/rust/emit.py`, assembled and cargo-built exactly as
  `tools/bench_selfhost_rust.py` builds the stages today.
- A minimal rust protocol shell: `didOpen`/`didChange`/`didClose` +
  `publishDiagnostics` (the full-text document store and JSON-RPC framing,
  ported from server.py/protocol.py). No hover, no definition, no code actions.
- The `classify` code+severity mapping and the range-tightening rule shipped as
  a generated data table / a ported pure function, NOT hand-written rust (A1).
- Differential harness: the rust binary vs `python -m revl.lsp` on the
  diagnostics corpus (per-tag refusals + multi-error + clean docs), asserting
  the `{range, severity, code, source, message}` list and its order are equal,
  plus the loud out-of-frontier test (A2).
- **Gated additionally on the full front end cargo-building on rust.** Today
  only the lexer emits+builds+runs on rust (item 270); parser/checker/lower
  still fail `cargo build` (item 278: E0072 recursive-ADT-needs-Box, E0391,
  more E0382 move shapes, E0308/E0609/E0282). Slice 1 cannot run until item 278
  closes, because publishDiagnostics needs the whole front end, not just the
  lexer. Name this gate; do not hide it.

Deferred out of Slice 1: hover, definition, code actions, the 332 `revl-gate`
crate packaging, and single-file distribution.

### Slice 2: hover + definition

Add the self-host symbol/declaration projection (A3): a queryable surface for
declaration line+column, scope spans, and rendered signatures, emitted to rust;
plus the `explain` guarantee/fix table as embedded data. Extend the shell with
`textDocument/hover` and `textDocument/definition` and the symbol corpus in the
harness. This is larger than Slice 1 precisely because it needs new self-host
surface, not just a shell.

### Slice 3: one distributable file, no python

Package the binary against the 332 `revl-gate` crate once that crate is built
(§3), so the binary "depends only on the published crate," strip the temporary
inlined-front-end integration from Slice 1, and produce the single
distributable file the roadmap headline promises. The differential harness
still runs `python -m revl.lsp` as the reference at test time; that is a
test-time dependency, not a runtime one, and does not weaken the "no python
dependency" claim for the shipped binary.

---

## 7. Decisions, in one place

- The binary is a hand-written rust protocol shell wrapping the self-host front
  end emitted to rust. The shell is I/O; the compiler is emitted. Keep the shell
  minimal so drift has nowhere to live.
- publishDiagnostics first, because it is compiler output; hover/definition
  later, because they are a projection the self-host does not expose yet.
- The MATCH guarantee is a differential harness against `python -m revl.lsp`
  over a frontier-scoped corpus, comparing structured diagnostic fields (the
  332 verdict shape, widened with a position), not raw bytes.
- The critical prerequisite is a positioned, multi-error diagnostic surface on
  the self-host front end (Slice 0); today's scalar `admit` verdict cannot be
  turned into editor squiggles.
- 332 is depended on twice: the rust `revl-gate` crate must exist (its rust half
  is not built yet), and 332's verdict shape must gain a source position (it has
  none today). Both are honestly open.
- The binary is only as complete as the self-host frontier (item 391); it must
  fail loudly, never silently mis-diagnose, outside that frontier, and it rides
  391 rather than forking a second implementation.
