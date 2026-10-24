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
#
# The suites only CI ran (issue #2210): whole backend directories, vitest, and
# the steps that set REVL_CROSS_TIER_SLOW, REVL_SWEEP_ALL_TIERS or a
# REVL_REQUIRE_* switch. tools/ci_suites.py reads them from ci.yml, and step 8
# lists every one. Each is opt-in by its CI job name, since together they take
# far longer than this gate; one not opted in prints SKIPPED with the flag that
# runs it, so none is silently absent:
#
#     sh tools/pre_merge.sh --with=backend-go,conformance   # or --with=all
#     REVL_PREMERGE_WITH=all make pre-merge
#     sh tools/pre_merge.sh --plan     # print what would run; run nothing

set -u
root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$root"

# In a git worktree the checkout has no `.venv` of its own — it lives beside the
# main checkout. Resolve the frontend interpreter the same way the pre-commit
# hook does: this worktree's .venv, else the primary checkout's, else a bare
# pytest, else nothing (and the frontend step loud-skips).
#
# A candidate is SELECTED only when its interpreter can actually run the suite
# (issue #2074). Testing existence alone let the primary checkout's .venv win
# the chain even though it was built without the `[test]` extras, so the
# frontend step died at collection instead of taking the loud-skip branch the
# script already has. An incomplete venv must skip loudly, never red: a
# collection error is strictly worse than the no-interpreter case the script
# already handles on purpose.
main_root=$(cd "$(git rev-parse --git-common-dir 2>/dev/null)/.." 2>/dev/null && pwd || echo "$root")

# The interpreter a `pytest` script runs under: its `#!` when that names a
# python, else the sibling `python` of the venv layout. Empty when neither is
# found — what an unusable candidate looks like.
pytest_python() {
    py=$(sed -n '1s/^#!//p' "$1" 2>/dev/null)
    case "${py##*/}" in
        python*) [ -x "$py" ] && { printf '%s\n' "$py"; return 0; } ;;
    esac
    dir=$(dirname "$1")
    for cand in "$dir/python" "$dir/python3"; do
        [ -x "$cand" ] && { printf '%s\n' "$cand"; return 0; }
    done
    return 1
}

# The modules `pytest tests/` imports hard from pyproject.toml's `[test]` extra
# (`yaml` is pyyaml, `pytest_asyncio` backs the async tests). A venv missing one
# dies at COLLECTION, so this is the bar a candidate pytest has to clear before
# the frontend step may run it. `coverage` is in the extra too but is imported
# inside functions, so it is deliberately not part of the bar: requiring it
# would loud-skip a venv that runs the suite perfectly well.
suite_ok() {
    [ -x "$1" ] || return 1
    "$1" -c 'import pytest, pytest_asyncio, yaml, cryptography, llguidance' \
        >/dev/null 2>&1
}

# The weaker bar the pure-python tool steps need. `tools/conformance.py` reads
# `tests/test_cross_tier_execution.py`, which imports pytest, so a python
# without pytest turns that step into a `ModuleNotFoundError` that is no defect
# in the diff (issue #2074 defect 1).
tools_ok() {
    [ -x "$1" ] || return 1
    "$1" -c 'import pytest' >/dev/null 2>&1
}

PYTEST=""
PYTHON=""
try_pytest() {
    [ -x "$1" ] || return 1
    py=$(pytest_python "$1" || true)
    if [ -n "$py" ] && suite_ok "$py"; then
        PYTEST=$1
        PYTHON=$py
        return 0
    fi
    return 1
}
try_pytest .venv/bin/pytest ||
    try_pytest "$main_root/.venv/bin/pytest" ||
    { command -v pytest >/dev/null 2>&1 && try_pytest "$(command -v pytest)"; } ||
    true

# PYTHON runs the pure-python tool steps. Prefer the chosen pytest's own
# interpreter; else any of the same candidates that can import pytest — a venv
# that fails `suite_ok` (no llguidance) can still run conformance.py; else the
# bare python3 the other tool steps already use.
if [ -z "$PYTHON" ]; then
    for cand in .venv/bin/python "$main_root/.venv/bin/python"; do
        if tools_ok "$cand"; then PYTHON=$cand; break; fi
    done
fi
if [ -z "$PYTHON" ] && command -v pytest >/dev/null 2>&1; then
    cand=$(pytest_python "$(command -v pytest)" || true)
    if [ -n "$cand" ] && tools_ok "$cand"; then PYTHON=$cand; fi
fi
if [ -z "$PYTHON" ]; then
    for cand in "$(command -v python3 2>/dev/null)" "$(command -v python 2>/dev/null)"; do
        if tools_ok "$cand"; then PYTHON=$cand; break; fi
    done
fi
[ -n "$PYTHON" ] || PYTHON=$(command -v python3 || command -v python || echo python3)

# --affected: the FAST inner-loop gate. Runs ONLY the pre-merge targets that
# tools/affected_tests.py selects for the current diff (base = merge-base with
# origin/main by default; override with --base=<ref>). This is NOT a replacement
# for the full `make pre-merge` — that stays the release/CI gate. The selector
# fails SAFE: any change to a core/compile-reachable frontend file, or any file
# it cannot map, requires an explicit escalation to the full gate.
AFF=0
AFF_BASE=""
PLAN=0
WITH=${REVL_PREMERGE_WITH:-}
for arg in "$@"; do
    case "$arg" in
        --affected) AFF=1 ;;
        --base=*) AFF_BASE="${arg#--base=}" ;;
        --plan) PLAN=1 ;;
        --with=*) WITH="${WITH:+$WITH,}${arg#--with=}" ;;
        *) ;;
    esac
done

# The python that reads ci.yml for step 8 (tools/ci_suites.py needs PyYAML).
YAML_PY=""
for cand in "$PYTHON" "$(command -v python3 2>/dev/null)"; do
    if [ -n "$cand" ] && "$cand" -c 'import yaml' >/dev/null 2>&1; then YAML_PY=$cand; break; fi
done

# Check the --with names before any step runs: a typo must stop the gate at
# once, not leave the suite quietly not opted in after the whole run.
if [ -n "$WITH" ]; then
    if [ -z "$YAML_PY" ]; then
        echo "pre-merge: --with=$WITH needs a python with PyYAML to read ci.yml, and none was found."
        exit 1
    fi
    if ! with_err=$("$YAML_PY" tools/ci_suites.py --root "$root" --check-with "$WITH" 2>&1); then
        echo "pre-merge: --with refused: $with_err"
        exit 1
    fi
fi

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
        echo "pre-merge --affected: selector unavailable (no python3 or python on PATH)."
        echo "pre-merge --affected: no full gate was run."
        echo "pre-merge --affected: to explicitly escalate, review the change and run 'make pre-merge'."
        exit 2
    else
        base_arg=""
        [ -n "$AFF_BASE" ] && base_arg="--base $AFF_BASE"
        if SEL_OUT=$("$PY" tools/affected_tests.py --root "$root" --format machine $base_arg 2>&1); then
            SEL_RC=0
        else
            SEL_RC=$?
        fi
        SEL_FULL=$(printf '%s\n' "$SEL_OUT" | sed -n 's/^FULL //p')
        SEL_REASON=$(printf '%s\n' "$SEL_OUT" | sed -n 's/^REASON //p')
        echo "pre-merge --affected: selector receipt:"
        if [ -n "$SEL_OUT" ]; then
            printf '%s\n' "$SEL_OUT" | sed 's/^/  /'
        else
            echo "  (no output)"
        fi
        if [ "$SEL_RC" -ne 0 ]; then
            echo "pre-merge --affected: selector exited with status $SEL_RC; refusing to run the FULL gate automatically."
            echo "pre-merge --affected: no full gate was run."
            echo "pre-merge --affected: to explicitly escalate, review the change and run 'make pre-merge'."
            exit 2
        fi
        if [ -z "$SEL_FULL" ]; then
            echo "pre-merge --affected: selector produced no usable FULL line; refusing to run the FULL gate automatically."
            echo "pre-merge --affected: no full gate was run."
            echo "pre-merge --affected: to explicitly escalate, review the change and run 'make pre-merge'."
            exit 2
        fi
        case "$SEL_FULL" in
            1)
                echo "pre-merge --affected: ${SEL_REASON:-selection requires the FULL gate}."
                echo "pre-merge --affected: refusing to run the FULL gate automatically."
                echo "pre-merge --affected: no full gate was run."
                echo "pre-merge --affected: to explicitly escalate, review the receipt and run 'make pre-merge'."
                exit 2
                ;;
            0)
                RUN_ALL=0
                SEL_PYTEST=$(printf '%s\n' "$SEL_OUT" | sed -n 's/^PYTEST //p')
                SEL_BACKENDS=$(printf '%s\n' "$SEL_OUT" | sed -n 's/^BACKENDS //p')
                SEL_GATES=$(printf '%s\n' "$SEL_OUT" | sed -n 's/^GATES //p')
                echo "pre-merge --affected: ${SEL_REASON:-targeted}"
                echo "pre-merge --affected: pytest $(printf '%s' "$SEL_PYTEST" | wc -w | tr -d ' ') node(s) | backends [${SEL_BACKENDS:-none}] | gates [${SEL_GATES:-none}]"
                echo "pre-merge --affected: inner-loop gate only; full \`make pre-merge\` still required at release/CI."
                ;;
            *)
                echo "pre-merge --affected: selector produced invalid FULL value '$SEL_FULL'; refusing to run the FULL gate automatically."
                echo "pre-merge --affected: no full gate was run."
                echo "pre-merge --affected: to explicitly escalate, review the receipt and run 'make pre-merge'."
                exit 2
                ;;
        esac
    fi
fi

fail=0
ran=0
skipped=0
planned=0
optout=0

# step "<label>" <cmd...> : run a command that MUST pass. Prints ok / FAILED and
# the tail of its output on failure. A failure fails the whole gate.
#
# Exit 77 (SKIP_RC, the automake "skipped" code) means the command checked
# nothing because a tool it needs is absent: formal/scripts/run_gate.sh without
# lake, regen_goldens via `skip_on` below. That prints SKIPPED with the
# command's own `SKIP` lines and counts as skipped, never "ok", and it does not
# fail the gate (issue #2209). Before this, `step` printed "ok" for any exit 0
# and dropped the output, so a skipped formal gate read as passed.
SKIP_RC=77
step() {
    label=$1
    shift
    printf '  %-42s' "$label"
    if [ "${PLAN:-0}" -eq 1 ]; then
        echo "would run: $*"
        planned=$((planned + 1))
        return
    fi
    if out=$("$@" 2>&1); then
        rc=0
    else
        rc=$?
    fi
    if [ "$rc" -eq "$SKIP_RC" ]; then
        echo "SKIPPED (a tool is missing; see the SKIP lines)"
        printf '%s\n' "$out" | grep '^SKIP' | tail -5 | sed 's/^/      /'
        skipped=$((skipped + 1))
        return
    fi
    ran=$((ran + 1))
    if [ "$rc" -eq 0 ]; then
        echo "ok"
    else
        echo "FAILED"
        echo "$out" | tail -30 | sed 's/^/      /'
        fail=1
    fi
}

# skip_on <rc> <cmd...> : run <cmd...>, turning its own "skipped" exit code
# <rc> into SKIP_RC for `step`. Any other code passes through unchanged.
skip_on() {
    _skip_rc=$1
    shift
    "$@"
    _rc=$?
    [ "$_rc" -eq "$_skip_rc" ] && return "$SKIP_RC"
    return "$_rc"
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

# >>> cordis-step (sliced and executed by tests/test_pre_merge_cordis_half_2207.py)
# cordis_step : run tools/cordis_half.py and map its STATUS line. `ok` only when
# pytest really ran the runtime half and passed; a missing runtime is NOT OK.
CORDIS_LOG=${CORDIS_LOG:-}
cordis_missing=0
cordis_step() {
    label="frontend-cordis (cordis-py venv)"
    printf '  %-42s' "$label"
    if [ "${PLAN:-0}" -eq 1 ]; then
        echo "would run: tools/cordis_half.py (the tests the frontend step skips for cordis)"
        planned=$((planned + 1))
        return
    fi
    set -- --root "$root" --select "$SEL_PYTEST" \
        --python "${REVL_CORDIS_PYTHON:-}" \
        --python "$root/backends/python/.venv/bin/python" \
        --python "$main_root/backends/python/.venv/bin/python"
    # The frontend step ran iff it created the log; an EMPTY log is its answer
    # "nothing skipped for cordis", never a reason to fall back to every file
    # that mentions cordis. The fallback is for a gate with no frontend pytest.
    [ -n "$CORDIS_LOG" ] && [ -f "$CORDIS_LOG" ] && set -- "$@" --log "$CORDIS_LOG"
    out=$("$PYTHON" tools/cordis_half.py "$@" 2>&1)
    rc=$?
    status=$(printf '%s\n' "$out" | sed -n 's/^STATUS //p' | tail -1)
    case "$rc:$status" in
        0:none)
            echo "nothing to run (no selected test needs cordis)" ;;
        0:ran*)
            ran=$((ran + 1))
            echo "ok (${status#ran } file(s))" ;;
        10:missing*)
            cordis_missing=1
            echo "MISSING, NOT OK (${status#missing })"
            echo "$out" | grep -v '^STATUS ' | tail -10 | sed 's/^/      /' ;;
        *)
            ran=$((ran + 1))
            fail=1
            echo "FAILED"
            echo "$out" | tail -30 | sed 's/^/      /' ;;
    esac
}
# <<< cordis-step

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
    CORDIS_LOG=$(mktemp "${TMPDIR:-/tmp}/revl-premerge-cordis.XXXXXX") || CORDIS_LOG=""
    step "$flabel" env REVL_CORDIS_SKIP_LOG="$CORDIS_LOG" "$PYTEST" $SEL_PYTEST -q -p no:cacheprovider
else
    skip "frontend  (pytest tests/)" "no pytest that can import the suite's deps"
fi

# 1b. The cordis-runtime half of tests/ (issue #2207). The frontend venv has no
#     cordis-py, so every test that needs it skipped in step 1; the frontend
#     step logged those (tests/conftest.py, REVL_CORDIS_SKIP_LOG) and this step
#     re-runs their files under the cordis-py venv backends/python/setup.sh
#     builds (pinned fork, watchdog extra). Without such a venv the step is
#     MISSING and the gate is NOT OK: a skipped runtime half is exactly how a G7
#     (LIFO teardown) regression passed every local gate.
if ! want frontend; then
    note "frontend-cordis (cordis-py venv)"
else
    cordis_step
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
#    Every one of these needs a suite-capable pytest; when the chain above found
#    none, they loud-skip like the frontend step rather than exec an empty
#    command (issue #2074: a rejected candidate leaves PYTEST empty).
if ! want backend go; then
    note "backend-go  (emit goldens)"
elif command -v go >/dev/null 2>&1; then
    if [ -z "$PYTEST" ]; then
        skip "backend-go  (emit goldens)" "no pytest"
    else
        step "backend-go  (emit goldens)" "$PYTEST" backends/go/test_emit_go.py -q -p no:cacheprovider
    fi
else
    skip "backend-go  (emit goldens)" "no go toolchain"
fi
gemit rust "backend-rust (emit goldens)" backends/rust/test_emit_rust.py
gemit wasm "backend-wasm (emit goldens)" backends/wasm/test_v3_emit.py backends/wasm/test_canonical_abi.py
gemit java "backend-java (emit goldens)" backends/java/test_emit_java.py

# NOTE the typescript backend suite (npx vitest / tsc) is deliberately NOT in
# this fast gate — it needs backends/typescript/node_modules and is heavy. Run it
# yourself when you touch that tier: cd backends/typescript && npm ci && npx vitest run
#
# The GOLDENS, though, are pure Python and sub-second. This used to check the
# typescript target alone on the grounds that the tier suites and `pytest
# tests/` covered the rest; they do not cover all of it, which is how a stale
# emitted go module reached main (issue #1089). Check every target: the driver
# loud-SKIPS one whose tool is missing (`gofmt` for go) rather than reporting a
# drift it cannot trust, so this is honest on a machine without Go and is the
# full comparison on one with it. CI runs the same check in `lint` and
# `backend-go`, and adds `--strict` there so a skip is an error: in CI the tool
# is supposed to be present, and a gate that skipped compared nothing. Locally
# a missing gofmt is an ordinary fact about the machine, so it must not fail the
# gate, but it must not read as "ok" either (issue #2209). `--strict` makes the
# driver exit 3 (its SKIPPED code) only when a target skipped and nothing
# drifted or broke, and `skip_on 3` turns that into a SKIPPED line here.
step "goldens      (drift, all targets)" skip_on 3 "$PYTHON" tools/regen_goldens.py --check --strict

# 4. Generated-artifact gates (pure Python, always run): the README conformance
#    matrix must match a fresh generation, the same contract the frontend CI job
#    enforces. The playground wheel is not committed (pages.yml builds it into
#    the published artifact), so its gate checks the deploy contract instead:
#    that pages.yml still builds it, under the name the two playground pages
#    fetch, from what git tracks.
if want gate conformance; then
    step "conformance matrix (--check-readme)" "$PYTHON" tools/conformance.py --check-readme
else
    note "conformance matrix (--check-readme)"
fi
if want gate site-wheel; then
    step "site wheel deploy contract"        "$PYTHON" tools/check_site_wheel.py
else
    note "site wheel deploy contract"
fi

# issue #255: the source-derived doc blocks (the MCP verb table, the CLI verb
# list, the guarantee tables, the DOC-STATUS inventory, the test count) must
# equal a fresh generation, and every subcommand and verb must be documented at
# all. Same contract as the matrix above. `make docs-gen` is the fix for a stale
# block; prose is the fix for a coverage failure.
if want gate docs; then
    step "docs drift      (docgen --check)"  "$PYTHON" tools/docgen.py --check
else
    note "docs drift      (docgen --check)"
fi

# issue #1204: docs/vision.md's commands, links and paths, resolved against the
# tree, plus the generated six-tier block re-checked against docs/conformance.md.
# The self-test plants a rename per rule first, so a gate that stopped firing is
# a red here rather than a green line that checked nothing. CI runs both in
# `lint`; this mirrors them. It resolves the commands, it does not run them.
if want gate docs; then
    step "vision claims   (self-test)"  "$PYTHON" tools/check_vision_claims.py --self-test
    step "vision claims   (--check)"    "$PYTHON" tools/check_vision_claims.py --check
else
    note "vision claims   (--check)"
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

# issue #1285 / issue #1332: the closed vocabularies this tree declares in more
# than one place. Stdlib-only and whole-tree, so there is no path set to select
# it on and it is always in the selection: a mirror is a relation between two
# files and the commit that creates one is routinely neither of the two the
# ledger names, which is how four classes reached `main` and reddened the
# required `lint` check with every affected run before them green. The
# self-test runs first, as with the vision gate, so a gate that stopped firing
# is a red here and not a green line that checked nothing.
if want gate vocabulary; then
    step "vocabulary mirrors (self-test)" "$PYTHON" tools/check_vocabulary_mirrors.py --self-test
    step "vocabulary mirrors (--check)"   "$PYTHON" tools/check_vocabulary_mirrors.py --check
else
    note "vocabulary mirrors (--check)"
fi

# 6. The census artifact (docs/census-artifact/, roadmap item 560, issue
#    #2103): the committed records have to be what THIS tree produces, program
#    by program. This is CI's `census-artifact` job run locally, with the same
#    command, and the selector puts `census` in the gate set on exactly the
#    paths that job's filter names.
#
#    Before this step the local gate had NO census entry, and the test the
#    selector did pick for these paths (`tests/test_census_artifact.py`) SKIPS
#    the half that reds CI: the crate-reproduction half is root-suite-skipped
#    by design (issue #1917). So a diff that staled the artifact printed a green
#    "83 passed, 1 skipped" locally and then reddened `census-artifact` on the
#    PR -- a green local signal for a change CI would refuse, which is the
#    defect this step closes. It is the same defect shape as the comment about
#    the formal gate below: a gate that only CI runs is a gate the local signal
#    lies about.
#
#    Slow (a full census re-run: ~83s on this shared box, ~20s in CI), so it is
#    a GATE and not an always-on step: the full gate always runs it, and an
#    affected selection runs it only when the diff moves one of the artifact's
#    inputs -- asked of the artifact's OWN filter rather than a copy of it, so
#    the local gate cannot disagree with CI about what the artifact reads.
#
#    On failure the tool names both remedies itself: `--write` for the records,
#    and `regen_generated.py --only census` for a crate-reproduction record the
#    checker version has moved past (that one needs cargo and minutes).
if want gate census; then
    step "census artifact (--verify --strict)" "$PYTHON" tools/census_artifact.py --verify --strict
else
    note "census artifact (--verify --strict)"
fi

# 7. The formal backbone (formal/STATUS.md): lake build, then the axioms
#    gate (no theorem may depend on sorryAx — an unfinished proof — or any
#    project-defined axiom), then the harness census. Needs elan/lake;
#    absent, loud-skip — CI's `formal` job is the real gate for those.
if ! want gate formal; then
    note "formal     (lake build + axioms gate)"
else
    step "formal     (lake build + axioms gate)" sh formal/scripts/run_gate.sh
fi

# 8. The suites only CI ran (issue #2210). tools/ci_suites.py reads ci.yml and
#    prints one line per CI-only suite: run (opted in, toolchain present), skip
#    (opted in, toolchain missing: counts as skipped), optout (not opted in:
#    SKIPPED with the flag that runs it) or covered (a step above runs it).
#    Reading ci.yml needs PyYAML; without it the whole set is one loud SKIPPED,
#    never an empty section.
# >>> ci-only-step (sliced and executed by tests/test_pre_merge_ci_only_suites_2210.py)
ci_line() { printf '  %s  %s\n' "$1" "$2"; }

ci_run() {
    label=$1
    shift
    printf '  %s  ' "$label"
    # stdin is the suite list being read; a child that reads stdin would eat it.
    if out=$("$@" </dev/null 2>&1); then rc=0; else rc=$?; fi
    ran=$((ran + 1))
    count=$(printf '%s\n' "$out" | grep -E '[0-9]+ (passed|failed|errors?)|no tests ran|Tests +[0-9]+' \
        | tail -1 | sed 's/^[= ]*//; s/[= ]*$//')
    if [ "$rc" -eq 0 ] && [ -n "$count" ]; then
        echo "ok ($count)"
    elif [ "$rc" -eq 0 ]; then
        # Exit 0 with no count is a run that may have run nothing: never ok.
        echo "FAILED (exit 0 but no test count in its output)"
        printf '%s\n' "$out" | tail -30 | sed 's/^/      /'
        fail=1
    else
        echo "FAILED (exit $rc${count:+, $count})"
        printf '%s\n' "$out" | tail -30 | sed 's/^/      /'
        fail=1
    fi
}

ci_only_suites() {
    yp=${YAML_PY:-}
    if [ -z "$yp" ]; then
        skip "ci-only suites (from ci.yml)" "no python with PyYAML to read ci.yml; the CI-only suites were not even listed"
        return
    fi
    list=$(mktemp "${TMPDIR:-/tmp}/revl-premerge-ci.XXXXXX") || {
        skip "ci-only suites (from ci.yml)" "mktemp failed"
        return
    }
    if ! "$yp" tools/ci_suites.py --root "$root" --list --with "$WITH" \
            --pytest "$PYTEST" --pytest-python "${PYTEST:+$PYTHON}" >"$list" 2>&1; then
        ci_line "ci-only suites (from ci.yml)" "FAILED (tools/ci_suites.py --list)"
        tail -30 "$list" | sed 's/^/      /'
        fail=1
        rm -f "$list"
        return
    fi
    tab=$(printf '\t')
    while IFS="$tab" read -r sid sstate slabel sdetail; do
        case "$sstate" in
            run)
                if [ "${PLAN:-0}" -eq 1 ]; then
                    ci_line "$slabel" "would run: $sdetail"
                    planned=$((planned + 1))
                else
                    ci_run "$slabel" "$yp" tools/ci_suites.py --root "$root" --run "$sid" --pytest "$PYTEST"
                fi ;;
            skip)
                ci_line "$slabel" "SKIPPED ($sdetail)"
                skipped=$((skipped + 1)) ;;
            optout)
                ci_line "$slabel" "SKIPPED (not opted in; CI job ${sid%/*} runs it; $sdetail)"
                optout=$((optout + 1)) ;;
            covered)
                [ "${PLAN:-0}" -eq 1 ] && ci_line "$slabel" "covered ($sdetail)" ;;
            *)
                ci_line "${slabel:-$sid}" "FAILED (unknown state from tools/ci_suites.py: $sstate)"
                fail=1 ;;
        esac
    done <"$list"
    rm -f "$list"
}
# <<< ci-only-step
ci_only_suites

# Clean up the emit shim and the cordis skip log.
[ -n "$EMIT_PATH" ] && rm -rf "$EMIT_PATH"
[ -n "$CORDIS_LOG" ] && rm -f "$CORDIS_LOG"

echo
if [ "${PLAN:-0}" -eq 1 ]; then
    echo "pre-merge --plan: $planned would run, $skipped skipped, $optout CI-only suite(s) not opted in; nothing was run"
    exit 0
fi
echo "pre-merge: $ran ran, $skipped skipped"
if [ "$fail" -ne 0 ]; then
    echo "pre-merge: FAILED — a step that ran did not pass (see above). Fix it before merging to main."
    exit 1
fi
if [ "$cordis_missing" -ne 0 ]; then
    echo "pre-merge: NOT OK: the cordis-runtime half of tests/ did not run (no cordis-py venv at CORDIS_PY_PIN, or no pin found; see above)."
    echo "           Run \`sh backends/python/setup.sh\` or set REVL_CORDIS_PYTHON, then re-run."
    exit 1
fi
if [ "${optout:-0}" -ne 0 ]; then
    echo "pre-merge: ${optout} CI-only suite(s) not opted in did not run; CI runs them."
    echo "           Opt in by CI job name (--with=backend-go,...) or all of them (--with=all)."
fi
if [ "$skipped" -ne 0 ]; then
    echo "pre-merge: green for what ran. $skipped step(s) skipped for a missing toolchain —"
    echo "           those are covered by CI's per-backend jobs, which are the real gate for them."
fi
if [ "$RUN_ALL" -eq 0 ]; then
    echo "pre-merge (affected): ok for the selected targets — NOT the full gate."
    echo "           Run \`make pre-merge\` (full) before release / on CI."
elif [ "$skipped" -ne 0 ]; then
    echo "pre-merge: ok, but $skipped step(s) SKIPPED: what needs a missing tool was not checked (see above)"
elif [ "${optout:-0}" -ne 0 ]; then
    echo "pre-merge: ok for what ran, which is not the CI-only suites listed above"
else
    echo "pre-merge: ok"
fi
