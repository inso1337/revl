#!/bin/sh
# revl pre-merge gate: the one command to run before a change reaches main.
#
#     make pre-merge          # or: sh tools/pre_merge.sh
#
# WHY THIS EXISTS (roadmap item 327). The suite a wave agent self-verifies
# against — `pytest tests/` — does NOT run the per-backend suites. Those live
# OUTSIDE tests/ and run as their own CI jobs (backend-python, backend-typescript,
# backend-{go,rust,wasm,java}), because each needs its own toolchain. So a change
# that is green under `pytest tests/` can still red CI on a per-backend suite and
# go unnoticed — which is exactly how main stayed RED for ~2h / ~19 pushes on
# 2026-08-26 (item 247's A5 respec left a stale per-backend semantics test, and a
# ts fixture went uncommitted). See the wave-backend-golden-gap and item 327.
#
# This gate mirrors the FAST half of every per-backend CI job locally: the emit
# and golden tests that need no heavy toolchain, plus the generated-artifact and
# lint gates. It is intentionally NOT a full CI replica — the toolchain-executing
# tests (cargo build, javac + cordis4j clone, wasmtime, tsc/vitest) stay CI's job
# and are loud-skipped here, because they are slow/networked and SIGKILL on the
# shared dev box. Nothing here is a silent skip: every step that cannot run says
# so, so the gate is never a false green.
#
# Contract: each step is either RAN (and must pass) or SKIPPED (loud, toolchain
# absent). The gate exits non-zero if any step ran and failed.

set -u
root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$root"

# In a git worktree the checkout has no `.venv` of its own — it lives beside the
# main checkout. Resolve the frontend interpreter the same way the pre-commit
# hook does: this worktree's .venv, else the primary checkout's, else a bare
# pytest, else nothing (and the frontend step loud-skips).
main_root=$(cd "$(git rev-parse --git-common-dir 2>/dev/null)/.." 2>/dev/null && pwd || echo "$root")
if [ -x .venv/bin/pytest ]; then
    PYTEST=.venv/bin/pytest
elif [ -x "$main_root/.venv/bin/pytest" ]; then
    PYTEST="$main_root/.venv/bin/pytest"
elif command -v pytest >/dev/null 2>&1; then
    PYTEST=pytest
else
    PYTEST=""
fi

fail=0
ran=0
skipped=0

# step "<label>" <cmd...> : run a command that MUST pass. Prints ok / FAILED and
# the tail of its output on failure. A failure fails the whole gate.
step() {
    label=$1
    shift
    printf '  %-42s' "$label"
    ran=$((ran + 1))
    if out=$("$@" 2>&1); then
        echo "ok"
    else
        echo "FAILED"
        echo "$out" | tail -30 | sed 's/^/      /'
        fail=1
    fi
}

# skip "<label>" "<why>" : a step that cannot run here. Loud on purpose — the
# whole point of item 327 is that a silent gap is what let CI stay red.
skip() {
    printf '  %-42s%s\n' "$1" "SKIPPED ($2)"
    skipped=$((skipped + 1))
}

# The per-backend emit/golden suites (rust, java, wasm) live in ONE file each
# alongside that tier's toolchain-executing tests, gated on `shutil.which(<tool>)`.
# Running them with the compiler on PATH would fire cargo builds against
# crates.io, a cordis4j clone + javac, and wasmtime execution — minutes of work
# that SIGKILLs on the shared box. Run them with the heavy compilers hidden so
# the toolchain tests skip LOUDLY (reported by `-rs`) and only the emit/golden
# tests run — which is the half that catches a stale golden / respec'd emitter,
# the item-327 drift class. `sys.executable` is absolute, so pytest still runs;
# only the tests' `which(<compiler>)` probes come up empty.
EMIT_PATH=""
emit_shim() {
    [ -n "$EMIT_PATH" ] && return 0
    EMIT_PATH=$(mktemp -d "${TMPDIR:-/tmp}/revl-premerge-shim.XXXXXX") || return 1
    py=$(command -v python3 || command -v python) || return 1
    ln -s "$py" "$EMIT_PATH/python3" 2>/dev/null || true
    ln -s "$py" "$EMIT_PATH/python"  2>/dev/null || true
}

# emit_step "<label>" <file...> : run an emit/golden suite with compilers hidden.
emit_step() {
    label=$1
    shift
    if [ -z "$PYTEST" ]; then
        skip "$label" "no pytest"
        return
    fi
    if ! emit_shim; then
        skip "$label" "cannot build emit shim"
        return
    fi
    label_full="$label"
    step "$label_full" env PATH="$EMIT_PATH" "$PYTEST" "$@" -q -rs -p no:cacheprovider
}

echo "pre-merge:"

# 1. Frontend suite + emitted-code validation (tests/test_conformance_validate.py
#    hands each tier's output to its real compiler where present). The core gate.
if [ -n "$PYTEST" ]; then
    step "frontend  (pytest tests/)" "$PYTEST" tests/ -q -p no:cacheprovider
else
    skip "frontend  (pytest tests/)" "no pytest on PATH or in .venv"
fi

# 2. The python backend semantics + golden suite — the suite item 247's respec
#    left stale. Its cordis-py venv is built by `sh backends/python/setup.sh`;
#    absent it loud-skips (never a silent green).
if [ -x backends/python/.venv/bin/pytest ]; then
    step "backend-python (.venv pytest)" \
        sh -c 'cd backends/python && .venv/bin/pytest -q'
else
    skip "backend-python (.venv pytest)" "no backends/python/.venv — run sh backends/python/setup.sh"
fi

# 3. The tier emit/golden suites. go's suite is toolchain-light (sub-second) and
#    needs `go` on PATH for its generated-current check, so it runs with the real
#    PATH; the other three run emit-only with compilers hidden (see emit_step).
if command -v go >/dev/null 2>&1; then
    step "backend-go  (emit goldens)" "$PYTEST" backends/go/test_emit_go.py -q -p no:cacheprovider
else
    skip "backend-go  (emit goldens)" "no go toolchain"
fi
emit_step "backend-rust (emit goldens)" backends/rust/test_emit_rust.py
emit_step "backend-wasm (emit goldens)" backends/wasm/test_v3_emit.py backends/wasm/test_canonical_abi.py
emit_step "backend-java (emit goldens)" backends/java/test_emit_java.py

# NOTE the typescript backend suite (npx vitest / tsc) is deliberately NOT in
# this fast gate — it needs backends/typescript/node_modules and is heavy. Run it
# yourself when you touch that tier: cd backends/typescript && npm ci && npx vitest run

# 4. Generated-artifact gates (pure Python, always run): the README conformance
#    matrix and the site/playground wheel must match a fresh generation, the same
#    contract the frontend CI job enforces.
step "conformance matrix (--check-readme)" python3 tools/conformance.py --check-readme
step "site wheel freshness"                python3 tools/check_site_wheel.py

# 5. Lint (the CI `lint` job): ruff at the pinned version. Prefer a ruff already
#    on PATH; else fetch the pinned one with uvx; else loud-skip.
if command -v ruff >/dev/null 2>&1; then
    step "ruff check" ruff check
elif command -v uvx >/dev/null 2>&1; then
    step "ruff check (uvx pinned)" uvx --from ruff==0.16.4 ruff check .
else
    skip "ruff check" "no ruff and no uvx"
fi

# Clean up the emit shim.
[ -n "$EMIT_PATH" ] && rm -rf "$EMIT_PATH"

echo
echo "pre-merge: $ran ran, $skipped skipped"
if [ "$fail" -ne 0 ]; then
    echo "pre-merge: FAILED — a step that ran did not pass (see above). Fix it before merging to main."
    exit 1
fi
if [ "$skipped" -ne 0 ]; then
    echo "pre-merge: green for what ran. $skipped step(s) skipped for a missing toolchain —"
    echo "           those are covered by CI's per-backend jobs, which are the real gate for them."
fi
echo "pre-merge: ok"
