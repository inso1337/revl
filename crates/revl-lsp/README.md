# `revl-lsp` — the revl language server as a native binary

Roadmap item 336, **slice 1**
(`docs/design/336-native-single-binary-tooling.md`). An editor launches this
binary and speaks LSP on its stdin/stdout, exactly as it would launch
`python -m revl.lsp`, and gets the same answers — byte for byte.

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
  escaping, key order (`pyjson.rs`).

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

## Distribution: this is NOT a self-contained single file

The design's slice 0 embeds a private interpreter (pyo3 +
python-build-standalone + the frozen `revl` wheel) so the artifact is one file
with no Python to install. **That spike has not been done, and this crate does
not do it.** It takes the fallback the design records in the open (A2): the
reference runs as a child process against a `revl` the machine already has.

So the distribution story here is **a native binary PLUS a reference `revl`
alongside**, not a self-contained single file. What that changes:

- an editor plugin shipping this binary must also ship or require a `revl`
  install — the "no Python to install" headline of slice 1 is NOT yet
  delivered;
- cold start and per-request latency are interpreter-bound, plus one process
  spawn on first use;
- what IS delivered is the native protocol layer every later slice reuses
  unchanged, the reference-agreement oracle harness, and the fail-closed
  contract above.

What was explicitly not done to avoid the dependency: substituting a native
checker for the reference. That is the exact move A1 forbids.

The engine is one long-lived worker process running `reference/worker.py`
(embedded in the binary with `include_str!`, so nothing needs installing beside
the executable). Point it at an interpreter that can `import revl` with
`REVL_LSP_PYTHON`; it defaults to `python3`.

## `revl/gateVersion`

One deliberate, additive divergence from the reference's dispatch table: the
custom request `revl/gateVersion` answers `{api, language, frontier, engine,
server}` so a client, a CI check or a fleet audit can detect a stale
binary/reference pairing before trusting its greens (design A3: skew is made
detectable, not solved). `frontier` reads `reference` because slice 1's engine
covers the whole language rather than the self-host frontier. The reference
answers `-32601` for this method; `initialize` is left byte-identical rather
than carrying the version, so the compared surface stays exact.

## Tests

```
cargo test                                   # unit tests only need cargo
REVL_LSP_PYTHON=/path/to/python cargo test   # plus the reference oracle
```

`tests/reference_agreement.rs` is the item's exit test:

- `binary_matches_the_reference_byte_for_byte` drives this binary and
  `python -m revl.lsp` with the identical framed byte stream — initialize,
  didOpen/didChange/didClose, hover and definition probes, code actions, an
  unknown method, shutdown/exit — and compares the two reply streams byte for
  byte, then asserts the compared stream is thick enough to prove something
  (hover, definition and diagnostic payloads all present, non-ASCII escaping
  exercised).
- `off_frontier_documents_still_get_their_squiggles` asserts the binary reports
  the reference's refusal code on every rejection document in the corpus. A
  binary that quietly ran a native-only checker would fail here on exactly the
  documents where it hides errors.

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

Not wired into `.github/workflows/ci.yml` by this change. A job would need
`dtolnay/rust-toolchain` (the `backend-rust` job already has it), the repo's
`src` on `PYTHONPATH`, and `REVL_LSP_PYTHON` pointing at the CI interpreter,
then `cargo test` in this directory — with the skip-with-a-reason discipline if
cargo is absent, never a hollow green.

## Slices 2 and 3

- **Slice 2** (needs the 332 `revl-gate` crate): definition and symbol-hover on
  the native parser for all inputs; native `admit` diagnostics as an
  ACCELERATOR only where the frontier pin proves a document covered, with the
  reference retained as fallback and oracle. The engine boundary in `engine.rs`
  is where that lands.
- **Slice 3** (gated on item 391): the full native checker, the bundled
  interpreter dropped, the binary small and pure rust. Cannot start earlier for
  soundness, not scheduling.
- Also outstanding from slice 0: the bundled-interpreter spike that would make
  this a genuine single file.
