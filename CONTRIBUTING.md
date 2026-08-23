# Contributing to revl

revl gets two kinds of contributor, and this guide is written for both:

- **Humans** — reading the design, fixing the checker, adding a tier, writing
  docs.
- **AI agents** — the language's primary *author* is increasingly an agent, and
  agents also *contribute to the compiler itself*, in parallel, in isolated
  worktrees. The workflow below was built for that and is described literally,
  because an agent cannot infer it from repo vibes.

If you only want to *write* revl (not change the compiler), you are in the wrong
file — read [docs/guide-humans.md](docs/guide-humans.md) or
[docs/guide-ai-agents.md](docs/guide-ai-agents.md) instead. This file is about
changing revl.

Everything here is enforced, not aspirational: the pre-commit hook and CI run
the same checks, and a change that skips them will be caught at the gate.

## The one rule that shapes everything

> **The checker is the product.** revl sells exactly one promise — a component
> that would corrupt a running system cannot compile. Every contribution is
> measured against whether it keeps that promise provable. That is why the
> rejection suite is an executable spec, why v1 output is byte-frozen, and why
> emitted code is handed to real compilers instead of trusted.

Read [DESIGN.md](DESIGN.md) §4 (the guarantees G1–G8 and lifecycle rules
A1–A8) before changing the frontend. Read
[docs/backend-ir-v1.md](docs/backend-ir-v1.md) and
[docs/backend-ir-v3.md](docs/backend-ir-v3.md) before changing an emitter.

## Setup

```bash
uv venv && uv pip install -e ".[test]"
.venv/bin/pytest tests/            # the frontend suite — should be green
```

Install the pre-commit hook once per clone:

```bash
git config core.hooksPath tools/hooks
```

The hook (`tools/hooks/pre-commit`) is deliberately kept under ~20s — a hook
people wait a minute for is a hook people turn off. It runs the checks that have
actually broken CI (see [The pre-commit contract](#the-pre-commit-contract)).
Skip it for a single commit with `git commit --no-verify`; do not make that a
habit.

Per-tier toolchains are only needed if you touch that tier — the frontend suite
runs the pure-Python halves of every backend and skips the runtime-integration
tests cleanly when a toolchain is absent. To work on a tier you will want its
runtime:

- **python** tier: `sh backends/python/setup.sh` (builds the cordis-py venv)
- **typescript** tier: `cd backends/typescript && npm ci`
- **wasm** tier: `wasmtime` on `PATH` (CI pins v47.0.3)
- **rust** tier: a Rust toolchain (`cargo`), crates.io reachable
- **java** tier: JDK 21
- **go** tier: Go 1.26

## The branch model: waves, worktrees, integration into main

revl is developed in **waves**. A wave is one focused unit of work — a roadmap
item, a bug, a tier fix — done on its own branch, in its own git worktree, so
that many waves (human and agent) run in parallel without treading on each
other. The pattern, visible throughout the commit history:

1. **Branch from `origin/main` in a fresh worktree.**

   ```bash
   git fetch -q origin
   git worktree add /tmp/wave-<name> -b wave-<name> origin/main
   ```

   A worktree is a separate checkout sharing the one `.git`, so the pre-commit
   hook resolves the primary checkout's `.venv` automatically (see the hook's
   comments). Work there; never on `main` directly.

2. **Do the work, commit on the wave branch.** Small, well-described commits.
   Commit messages are prose, present-tense, scoped
   (`fix(stdlib): …`, `docs(roadmap): …`, `test(swap): …` — match the existing
   log style).

3. **Integrate by merging `origin/main` into your branch, not by rebasing main
   onto you.** Before pushing:

   ```bash
   git fetch -q origin
   git merge --no-edit origin/main          # take origin's moves onto your branch
   # re-run the suite — a clean merge can still be a semantic conflict
   ```

   These integration merges are the `Merge remote-tracking branch 'origin/main'
   into wave…` commits you see in the log. Resolve conflicts on *your* branch,
   with the suite green, before the change reaches `main`.

4. **Fast-forward `main` only when it is truly an ancestor.** Guard the push so
   you never clobber a concurrent wave:

   ```bash
   git fetch -q origin && git merge --no-edit origin/main
   git merge-base --is-ancestor origin/main HEAD && git push origin HEAD:main
   ```

   If `origin/main` moved under you, re-fetch, re-merge, re-guard. **Never
   force-push `main`.**

5. **Clean up the worktree** when the wave lands:

   ```bash
   git worktree remove /tmp/wave-<name>
   ```

Human contributors from outside the core repo use the same shape through a
**fork + pull request**: branch, keep it merged up to date with `main`, and open
a PR. The pre-commit contract below is what a reviewer will check the PR
against, so run it before you push.

## The pre-commit contract

Every change must pass these before it merges — the hook runs them locally and
CI (`.github/workflows/ci.yml`) runs them again. A green local hook is the
minimum bar, not a substitute for CI, because the hook skips what your machine
cannot run.

1. **Frontend suite + emitted-code validation** — `pytest tests/ -q`. This is
   the core gate. It includes `tests/test_conformance_validate.py`, which hands
   each tier's emitted output to that tier's *real* compiler (`tsc`,
   `cargo check`, `javac`, `wasmtime`, a scope walk for python) — because "the
   emitter did not raise" never meant "the code compiles." Runtime-integration
   tests skip cleanly when a toolchain is absent; **read the skips** (`-rs`), a
   skip is not a pass.

2. **The conformance matrix (emit)** — `python3 tools/conformance.py`. Pure
   Python, no toolchain, always runs: it catches a backend that stops emitting a
   construct entirely. See [docs/conformance.md](docs/conformance.md).

3. **The TypeScript backend suite** — `cd backends/typescript && npx vitest run`
   (and `npx tsc --noEmit`). Runs `tsc` over emitted code; the hook runs it only
   where `node_modules` exists.

4. **The backend golden tests** — each tier's emitter has a checked-in golden
   and a test asserting it reproduces it **byte-identically**:

   | tier | command |
   |---|---|
   | python | `sh backends/python/setup.sh && cd backends/python && .venv/bin/pytest -q` |
   | typescript | `cd backends/typescript && npm ci && npx vitest run` |
   | wasm | `pytest backends/wasm/test_v3_emit.py tests/test_wasm_backend.py -q` |
   | rust | `pytest backends/rust/test_emit_rust.py -q` |
   | java | `pytest backends/java/test_emit_java.py -q` |
   | go | `pytest backends/go/test_emit_go.py -q` |

   CI runs each tier in its own job with the toolchain pinned. If you touch an
   emitter, run its tier's goldens — the frontend suite alone does **not** cover
   every per-backend golden.

### Two invariants that will fail your change if you break them

**Byte-identical v1 goldens.** The v1 IR is frozen and emitted v1 output is
byte-for-byte fixed (`backends/<tier>/golden/user_cache.<ext>`). A change that
alters emitted v1 bytes fails the suite and does not land. If you *intend* to
change output, you are changing a frozen contract — that is a design decision,
not a golden regen. Do not "fix" a failing golden by regenerating it to match
your change; understand why it moved first. (New IR capability goes under a new
`ir_version`, not into v1 — see [docs/stability.md](docs/stability.md).)

**Every rejection joins the executable spec.** This is the rule that keeps the
soundness promise self-proving. When you make the checker refuse something it
used to accept — closing a hole, adding a guarantee — you must:

1. add a minimal fixture under **`examples/rejections/`** (e.g.
   `g4_my_new_hole.rvl`) that triggers the rejection;
2. add a row to the **`REJECTIONS`** table in
   **`tests/test_frontend.py`** mapping that filename to the exact diagnostic
   substring the checker must produce.

`tests/test_frontend.py::test_every_rejection_file_is_covered` asserts the set
of files on disk **equals** the set of keys in `REJECTIONS` — so you cannot add a
fixture without a spec entry, or an entry without a fixture. The rejection suite
is therefore the checker's *definition* of sound, exhaustive by test, not a
sample. (The handful of guarantees that no source program can violate at compile
time — G5, G7, A3, A4, A5, A7 — are documented in the block after the
`REJECTIONS` table, with where each is actually enforced. If you believe you
have a new such case, extend that block; do not leave it implicit.)

A diagnostic is itself a deliverable ([DESIGN.md](DESIGN.md) §9): it must name
its guarantee and state the fix, and it is written for the model that will read
it. A rejection whose message is a bare "error" is not done.

## Adding or changing a backend tier

- Read [docs/backend-ir-v3.md](docs/backend-ir-v3.md) (and
  [docs/backend-ir-v1.md](docs/backend-ir-v1.md) for the frozen core). The
  frontend is the single IR producer; a backend consumes IR and must gate on
  `ir_version`, refusing a version it does not implement rather than
  misemitting.
- Keep the tier honest with the conformance matrix: `tools/conformance.py` emits
  every construct through all backends, and `--validate` hands the output to the
  real toolchain. A new gap must show up there, and a baselined failure that
  starts passing must be un-baselined (`tests/test_conformance_validate.py`
  fails both on new breakage and on a baselined case that starts passing, so the
  list can only shrink).
- A tier's runtime-integration tests should **skip loudly** without their
  toolchain, never silently pass. CI installs the toolchain and, for the tiers
  where it matters, makes a missing runtime *fatal* (e.g.
  `REVL_REQUIRE_WASMTIME=1`) so coverage is real and not implied.

Some Cordis runtime targets are upstream projects (cordis-py, cordis, cordis-rs,
cordis4j, stc-go) — a genuine *runtime* defect there is fixed upstream
(fork + PR to that repo), while an *emitter* defect is fixed here in
`backends/<tier>/emit.py`. Fix the emitter for an emitter bug; do not fork a
runtime to paper over a lowering mistake.

## Documentation changes

Docs are load-bearing and public-facing — accuracy matters more than polish.
Match the existing tone: precise, honest about what is tested vs. demonstrated
vs. claimed, and specific about the command that backs a claim (a number with no
command behind it is the failure mode this project keeps hitting). The roadmap
([docs/v2.0-roadmap.md](docs/v2.0-roadmap.md)) is the ledger of what is done and
what is in flight; keep it current when you land a roadmap item.

## Reporting bugs and requesting features

Use the issue templates (bug report / feature request). A bug report that
arrives with the `.rvl` source, the target tier, and the emitted/expected output
is triageable in minutes; one that arrives as prose is a day of archaeology. The
templates ask for exactly what a maintainer needs — please fill them in.

For a **security or soundness escape** — a program the checker *accepts* that
violates a guarantee — do **not** open a public issue. Follow
[SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
