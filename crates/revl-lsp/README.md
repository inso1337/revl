# `revl-lsp` — the revl language server as a native binary

Roadmap item 336, **slices 1 and 2**
(`docs/design/336-native-single-binary-tooling.md`). An editor launches this
binary and speaks LSP on its stdin/stdout, exactly as it would launch
`python -P -m revl.lsp` (the `-P` is the PYTHONSAFEPATH safety bit, issue
#317), and gets the same answers — byte for byte.

```
cargo build --release
./target/release/revl-lsp            # serve LSP on stdio
./target/release/revl-lsp --version  # server + gate version
```

## What is native, and what is not

Native rust, and the part item 336 is about:

- the `Content-Length` framed JSON-RPC loop over stdio (`protocol.rs`);
- the document lifecycle — `didOpen` / `didChange` / `didClose` over a per-URI
  full-text store, full-document sync (`server.rs`);
- capability negotiation: `initialize` advertises exactly what
  `src/revl/lsp/server.py` advertises, in the same wire shape;
- the dispatch table, the `publishDiagnostics` cadence, and an encoder that
  reproduces CPython's `json.dumps` bytes — separators, `ensure_ascii`
  escaping, key order (`pyjson.rs`);
- **slice 2:** `definition` and the signature half of `hover`, answered from
  the self-host front end through `revl_gate::symbols` with no interpreter on
  the path (`native.rs`). The table is rebuilt once per document version and
  held beside the text, so a navigation request costs a lookup.

**Not native: the checker.** Diagnostics, hover and definition are computed by
the REFERENCE front end and forwarded verbatim. This is the design's CRITICAL
(A1), and it is not an implementation shortcut. The self-host front end runs
natively on rust today but covers only the conformance `revl` frontier; off
that frontier it has no check to run and reports admission, which in an editor
is a MISSING squiggle — green on code the reference refuses, with the authority
of "the actual compiler". A native checker that matches the reference
everywhere is roadmap item 391, and slice 3 is gated on it.

The rule this binary keeps: **show every diagnostic the reference shows; you
may show more, never fewer.** An engine that cannot answer publishes a visible
`REVL-LSP-ENGINE` error diagnostic on the document, never an empty list —
silence is the editor's false-admit.

### Why slice 2 did not make diagnostics an accelerator

The design's slice 2 planned one: native `admit` producing the diagnostics for
a document the frontier pin proves fully covered, with the reference kept as
the off-frontier fallback. **The `revl-gate` crate that landed cannot support
that, and the reason is in its own surface.** That crate "issues no
admissions": its non-refusing arm is `NoObjection`, meaning *this gate found
nothing it is able to refuse*, because it decides the composition and guarantee
layer and does **not** run the reference type layer. So no document is ever
proven fully covered — a clean native result does not show the document is
clean, and even a native REFUSAL does not show the reference would raise only
that one diagnostic (a multi-refusal compile carries several). Short-circuiting
the reference on either is the missing-squiggle direction, which the design
makes release-blocking.

What IS sound is the other half of the same rule, and it is what shipped: a
native refusal the reference did not report is ADDED to the publish, tagged
`source: "revl-native"`, never replacing anything. On the covered corpus the
two agree and the add path stays silent — `the_binary_never_shows_fewer_
squiggles_than_the_reference` asserts both halves. A native `BAD` (the
self-host's own parse failure) is never shown: it is a frontier gap wearing a
refusal's clothes, and `verified fn` draws one on a document the reference
diagnoses correctly as `G7`.

### What navigation may answer, and when

Native navigation answers only on a document with **no diagnostics**. This runs
the opposite way from the risk A4 anticipated. A4 expected the native parser to
be less capable than the reference, where a missed symbol is benign. On the
real corpus it is sometimes MORE capable: the reference PARSER raises on a
large class of refusals (an `effect` with no `undo` raises `G4` inside
`Parser.parse`), and after a parse failure `analysis.build_symbols` yields an
empty table, so the reference resolves nothing anywhere in that document. A
document with no diagnostics is exactly the set where the reference's own parse
is known to have succeeded.

Within that set the native table still declines anything it cannot answer
exactly: a construct the self-host parser cannot read (`pub`, `verified`, a
`fn` type-parameter list) makes the whole document undecided; a name a
parameter or `let` might shadow is dropped, because the reference resolves the
innermost scope and this path cannot see scopes; a `type` sharing a name with a
`fn` takes the name back; and a signature the crate cannot spell the way the
reference spells it comes back without a signature, so hover defers while
definition still answers.

## Distribution: the private runtime, and the PATH fallback

The engine picks an interpreter to run `reference/worker.py` (embedded in the
binary with `include_str!`) in a fixed order (`runtime.rs`, `engine.rs`):

1. **`REVL_LSP_PYTHON`**, if set, wins — the explicit override CI and the
   oracle use to pin the reference.
2. otherwise a **bundled PRIVATE RUNTIME**, if one is resolvable: a pinned
   `python-build-standalone` tree with the `revl` wheel frozen into its own
   site, extracted **atomically into a versioned private cache**
   (`<cache>/revl-lsp/runtime/<pin>/`) on first use and run in **isolated mode**
   (`-I`), so the worker imports `revl` only from that private site and never
   the machine's `PYTHONPATH`, user site, or a `python3` on `PATH`. This is the
   self-contained single-file path. A runtime that is CONFIGURED but broken
   fails closed (a visible `REVL-LSP-ENGINE` diagnostic), never a silent
   degrade to PATH.
3. otherwise **`python3` on PATH** — the honest fallback design A2 records, "a
   native binary PLUS a reference `revl` alongside", so a bare `cargo` build and
   every machine that already has `revl` keep working unchanged.

A runtime is resolved from `REVL_LSP_RUNTIME` (an already-extracted directory),
`REVL_LSP_RUNTIME_ARCHIVE` (a pinned `tar`, extracted atomically on first use),
or a `runtime/<pin>/` tree / `runtime.tar` sitting beside the executable (the
packaged install layout). `REVL_LSP_CACHE` and `REVL_LSP_RUNTIME_PIN` override
where and under what key the cache lands.

**What this slice landed, and what it defers.** The runtime-management contract
is here — the pin, the atomic versioned cache, the isolated-mode launch, and
the fail-closed rule — and `revl/gateVersion` now reports an `embedding` of
`private-runtime` or `system-python` so a client can tell a self-contained
artifact from one leaning on an installed `revl`. What is NOT in this crate is
shipping the pinned `python-build-standalone` archive itself, nor the in-process
pyo3 link (`libpython`, `PyConfig.isolated`); those are the distribution/build
step (338, and the design's slice-0 embed), which layer on this contract
without changing it — a child process or a linked interpreter reads the same
pin, cache and isolation. What was explicitly NOT done to avoid the dependency:
substituting a native checker for the reference, the exact move A1 forbids.

## `revl/gateVersion`

One deliberate, additive divergence from the reference's dispatch table: the
custom request `revl/gateVersion` answers `{api, language, frontier, engine,
native, server}` so a client, a CI check or a fleet audit can detect a stale
binary/reference pairing before trusting its greens (design A3: skew is made
detectable, not solved). `frontier` reads `reference` because DIAGNOSTICS — the
answers a green depends on — still cover the whole language rather than the
self-host frontier; the native engine's own pin (`api`, `language`,
`frontier`, `layer`, and the verbs it answers) sits beside it under `native`,
and that is the id a stale-binary audit compares. The reference answers
`-32601` for this method; `initialize` is left byte-identical rather than
carrying the version, so the compared surface stays exact.

## Tests

```
cargo test                                   # unit tests only need cargo
REVL_LSP_PYTHON=/path/to/python cargo test   # plus the reference oracle
```

`tests/reference_agreement.rs` is the item's exit test:

- `binary_matches_the_reference_byte_for_byte` drives this binary and
  `python -P -m revl.lsp` (the `-P` is the PYTHONSAFEPATH safety bit,
  issue #317) with the identical framed byte stream — initialize,
  didOpen/didChange/didClose, hover and definition probes, code actions, an
  unknown method, shutdown/exit — and compares the two reply streams byte for
  byte, then asserts the compared stream is thick enough to prove something
  (hover, definition and diagnostic payloads all present, non-ASCII escaping
  exercised).
- `off_frontier_documents_still_get_their_squiggles` asserts the binary reports
  the reference's refusal code on every rejection document in the corpus. A
  binary that quietly ran a native-only checker would fail here on exactly the
  documents where it hides errors.
- `the_binary_never_shows_fewer_squiggles_than_the_reference` is the
  missing-squiggle rule as an executable check: every diagnostic the reference
  publishes is present in the binary's publish, byte for byte. Zero rows in the
  fewer-than-reference direction, ever. It also asserts the native ADD path
  stayed silent on the corpus, so byte-identity is not being bought back by
  absorbing a divergence.
- `native_navigation_answers_and_never_disagrees` is slice 2's soundness exit
  test. It runs the binary a second time with `REVL_LSP_NATIVE_ONLY=1`, which
  turns off the reference fallback so the NATIVE answers are observable on
  their own, and asserts each is either the reference's answer or null — never
  a third thing. It then asserts the native path actually answered, including
  one hover of every declaration kind with its full signature, so agreement
  cannot be bought by answering nothing.

`tests/private_runtime.rs` is the one-file bundling exit check:

- `a_bundled_runtime_answers_from_a_versioned_private_cache_in_isolated_mode`
  builds a runtime archive around a real `revl`-capable interpreter, drives the
  binary with only that archive to reach `revl` (no `REVL_LSP_PYTHON`), and
  asserts the interpreter was extracted into the versioned cache, the binary
  reports the `private-runtime` embedding, its published diagnostics equal the
  reference server's byte for byte, and a second launch REUSES the cache rather
  than re-extracting. It sources the interpreter from
  `REVL_LSP_TEST_RUNTIME_PYTHON` (or `REVL_LSP_PYTHON`), skipping with a stated
  reason when neither is set.

**The corpus crosses the self-host frontier on purpose** (a required exit
condition, not an optional extension). It is built from `examples/rejections/`
and `examples/`, and spans G1 declared access over a component `requires`
clause, G2 provision conflict, G3 dependency cycle, G4 missing `undo` and
unmarked emission, G6 impurity in a component body, G7 `verified fn` totality,
G8 extern classification, A1 async reach through a required key, A9 a `provide`
against an undeclared key, and T1 return-path and match-exhaustiveness typing —
component activation bodies, `provide` methods, `effect`/`undo` acquisition and
service linking, which `docs/conformance.md` records as the `lim` rows of the
self-host frontier. Clean compositions and protocol edges (an empty document, a
syntax error, an in-frontier `fn` body) are in the corpus too.

The reference is both this crate's engine and its oracle, so a machine that
cannot `import revl` cannot verify anything: the oracle tests FAIL with that
reason rather than passing hollowly. Set `REVL_LSP_ALLOW_NO_REFERENCE=1` to
turn that into a stated skip.

## CI

The `backend-rust` job runs `cargo test` here with the repo's `src` on
`PYTHONPATH` and `REVL_LSP_PYTHON` pointing at the job's interpreter, so the
oracle executes for real rather than skipping.

## What is still open

- **Slice 3** (gated on item 391): the full native checker, the bundled
  interpreter dropped, the binary small and pure rust. Cannot start earlier for
  soundness, not scheduling.
- Outstanding from the one-file bundling slice: shipping the pinned
  `python-build-standalone` archive with the crate (so a distributable actually
  carries a runtime for the resolver above to find), and the in-process pyo3
  link that replaces the child process with a linked `libpython`. The
  runtime-management contract they build on — pin, atomic versioned cache,
  isolated launch — landed here (`runtime.rs`, `tests/private_runtime.rs`).
- Outstanding from slice 2: `hover`, `codeAction` and everything about
  diagnostics remain reference-served, and native navigation is confined to
  documents with no diagnostics and to declarations the self-host parser
  models. Widening either needs the self-host front end to carry `pub`,
  `verified`, `type` declarations, scopes and parameters — which is item 391,
  the same gate slice 3 waits on.
