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

# --affected: the FAST inner-loop gate. Runs ONLY the pre-merge targets that
# tools/affected_tests.py selects for the current diff (base = merge-base with
# origin/main by default; override with --base=<ref>). This is NOT a replacement
# for the full `make pre-merge` — that stays the release/CI gate. The selector
# fails SAFE: any change to a core/compile-reachable frontend file, or any file
# it cannot map, falls back to the full gate here.
AFF=0
AFF_BASE=""
for arg in "$@"; do
    case "$arg" in
        --affected) AFF=1 ;;
        --base=*) AFF_BASE="${arg#--base=}" ;;
        *) ;;
    esac
done

# Selection defaults ARE the full gate, so with AFF=0 (plain `make pre-merge`)
# every step runs exactly as before. `want` short-circuits to true whenever
# RUN_ALL=1, which is the case for the full gate and for a FULL selection.
RUN_ALL=1
SEL_PYTEST="tests/"
SEL_BACKENDS="python go rust wasm java"
SEL_GATES="conformance site-wheel ruff"

if [ "$AFF" -eq 1 ]; then
    PY=$(command -v python3 || command -v python || true)
    if [ -z "$PY" ]; then
        echo "pre-merge --affected: no python3 to run the selector -> running FULL gate."
    else
        base_arg=""
        [ -n "$AFF_BASE" ] && base_arg="--base $AFF_BASE"
        SEL_OUT=$("$PY" tools/affected_tests.py --root "$root" --format machine $base_arg 2>/dev/null)
        SEL_FULL=$(printf '%s\n' "$SEL_OUT" | sed -n 's/^FULL //p')
        SEL_REASON=$(printf '%s\n' "$SEL_OUT" | sed -n 's/^REASON //p')
        if [ -z "$SEL_OUT" ] || [ -z "$SEL_FULL" ]; then
            echo "pre-merge --affected: selector produced no usable output -> running FULL gate."
        elif [ "$SEL_FULL" = "1" ]; then
            echo "pre-merge --affected: ${SEL_REASON:-full}"
            echo "pre-merge --affected: selection is the FULL gate (nothing skipped)."
        else
            RUN_ALL=0
            SEL_PYTEST=$(printf '%s\n' "$SEL_OUT" | sed -n 's/^PYTEST //p')
            SEL_BACKENDS=$(printf '%s\n' "$SEL_OUT" | sed -n 's/^BACKENDS //p')
            SEL_GATES=$(printf '%s\n' "$SEL_OUT" | sed -n 's/^GATES //p')
            echo "pre-merge --affected: ${SEL_REASON:-targeted}"
            echo "pre-merge --affected: pytest $(printf '%s' "$SEL_PYTEST" | wc -w | tr -d ' ') node(s) | backends [${SEL_BACKENDS:-none}] | gates [${SEL_GATES:-none}]"
            echo "pre-merge --affected: inner-loop gate only; full \`make pre-merge\` still required at release/CI."
        fi
    fi
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

# want "<kind>" "<name>" : is this target in the current selection? Always true
# for the full gate (RUN_ALL=1), so plain `make pre-merge` is unchanged.
want() {
    [ "$RUN_ALL" -eq 1 ] && return 0
    case "$1" in
        frontend) [ -n "$SEL_PYTEST" ] && return 0 ;;
        backend)  case " $SEL_BACKENDS " in *" $2 "*) return 0 ;; esac ;;
        gate)     case " $SEL_GATES " in *" $2 "*) return 0 ;; esac ;;
    esac
    return 1
}

# note "<label>" : a step the affected selection did not pick (neither ran nor a
# toolchain skip — just out of scope for this diff).
note() { printf '  %-42s%s\n' "$1" "not selected (affected mode)"; }

# gemit "<tier>" "<label>" <files...> : emit_step guarded by the selection.
gemit() {
    tier=$1
    shift
    if want backend "$tier"; then
        emit_step "$@"
    else
        note "$1"
    fi
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

if [ "$RUN_ALL" -eq 1 ]; then
    echo "pre-merge:"
else
    echo "pre-merge (affected):"
fi

# 1. Frontend suite + emitted-code validation (tests/test_conformance_validate.py
#    hands each tier's output to its real compiler where present). The core gate.
if ! want frontend; then
    note "frontend  (pytest)"
elif [ -n "$PYTEST" ]; then
    if [ "$RUN_ALL" -eq 1 ]; then flabel="frontend  (pytest tests/)";
    else flabel="frontend  (pytest, $(printf '%s' "$SEL_PYTEST" | wc -w | tr -d ' ') node(s))"; fi
    step "$flabel" "$PYTEST" $SEL_PYTEST -q -p no:cacheprovider
else
    skip "frontend  (pytest tests/)" "no pytest on PATH or in .venv"
fi

# 2. The python backend semantics + golden suite — the suite item 247's respec
#    left stale. Its cordis-py venv is built by `sh backends/python/setup.sh`;
#    absent it loud-skips (never a silent green).
if ! want backend python; then
    note "backend-python (.venv pytest)"
elif [ -x backends/python/.venv/bin/pytest ]; then
    step "backend-python (.venv pytest)" \
        sh -c 'cd backends/python && .venv/bin/pytest -q'
else
    skip "backend-python (.venv pytest)" "no backends/python/.venv — run sh backends/python/setup.sh"
fi

# 3. The tier emit/golden suites. go's suite is toolchain-light (sub-second) and
#    needs `go` on PATH for its generated-current check, so it runs with the real
#    PATH; the other three run emit-only with compilers hidden (see emit_step).
if ! want backend go; then
    note "backend-go  (emit goldens)"
elif command -v go >/dev/null 2>&1; then
    step "backend-go  (emit goldens)" "$PYTEST" backends/go/test_emit_go.py -q -p no:cacheprovider
else
    skip "backend-go  (emit goldens)" "no go toolchain"
fi
gemit rust "backend-rust (emit goldens)" backends/rust/test_emit_rust.py
gemit wasm "backend-wasm (emit goldens)" backends/wasm/test_v3_emit.py backends/wasm/test_canonical_abi.py
gemit java "backend-java (emit goldens)" backends/java/test_emit_java.py

# NOTE the typescript backend suite (npx vitest / tsc) is deliberately NOT in
# this fast gate — it needs backends/typescript/node_modules and is heavy. Run it
# yourself when you touch that tier: cd backends/typescript && npm ci && npx vitest run

# 4. Generated-artifact gates (pure Python, always run): the README conformance
#    matrix and the site/playground wheel must match a fresh generation, the same
#    contract the frontend CI job enforces.
if want gate conformance; then
    step "conformance matrix (--check-readme)" python3 tools/conformance.py --check-readme
else
    note "conformance matrix (--check-readme)"
fi
if want gate site-wheel; then
    step "site wheel freshness"                python3 tools/check_site_wheel.py
else
    note "site wheel freshness"
fi

# 5. Lint (the CI `lint` job): ruff at the pinned version. Prefer a ruff already
#    on PATH; else fetch the pinned one with uvx; else loud-skip.
if ! want gate ruff; then
    note "ruff check"
elif command -v ruff >/dev/null 2>&1; then
    step "ruff check" ruff check
elif command -v uvx >/dev/null 2>&1; then
    step "ruff check (uvx pinned)" uvx --from ruff==0.16.4 ruff check .
else
    skip "ruff check" "no ruff and no uvx"
fi

# 6. The formal backbone (formal/STATUS.md): lake build, then the axioms
#    gate (no theorem may depend on sorryAx — an unfinished proof — or any
#    project-defined axiom), then the harness census. Needs elan/lake;
#    absent, loud-skip — CI's `formal` job is the real gate for those.
if ! want gate formal; then
    note "formal     (lake build + axioms gate)"
else
    step "formal     (lake build + axioms gate)" sh formal/scripts/run_gate.sh
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
if [ "$RUN_ALL" -eq 0 ]; then
    echo "pre-merge (affected): ok for the selected targets — NOT the full gate."
    echo "           Run \`make pre-merge\` (full) before release / on CI."
else
    echo "pre-merge: ok"
fi
