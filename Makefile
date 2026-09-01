# Convenience targets. The conformance matrix in docs/conformance.md is generated, never
# authored: `make matrix` regenerates it, and CI fails if the committed block
# drifts from a fresh generation (see .github/workflows/ci.yml).

.PHONY: matrix matrix-check demo pre-merge pre-merge-affected formal

# roadmap item 327: the required gate before a change reaches main. Mirrors the
# FAST half of every per-backend CI job locally (emit/golden suites, the
# generated-artifact gates, lint) so the drift that `pytest tests/` alone misses
# — a stale per-backend suite that reds CI unnoticed — is caught before pushing.
# Steps that need a heavy/absent toolchain skip LOUDLY (never a false green); the
# target exits non-zero if any step that ran failed. See tools/pre_merge.sh and
# CONTRIBUTING.md "The pre-merge gate".
pre-merge:
	sh tools/pre_merge.sh

# FAST inner-loop gate: run ONLY the pre-merge targets that tools/affected_tests.py
# selects for the current diff (base = merge-base with origin/main). A sound
# conservative superset — any core/compile-reachable or unmapped change falls back
# to the full gate. NOT a replacement for `make pre-merge`, which remains the
# release/CI gate. Override the base with: make pre-merge-affected BASE=<ref>
pre-merge-affected:
	sh tools/pre_merge.sh --affected $(if $(BASE),--base=$(BASE),)

# Regenerate the docs/conformance.md conformance + performance matrix in place.
matrix:
	python3 tools/conformance.py --write-readme

# The staleness gate CI runs: exit non-zero if the committed block is stale.
matrix-check:
	python3 tools/conformance.py --check-readme

# v3.0 gate E3: the live-systems demo (swap / why / apply) from a clean
# checkout. Ensures the cordis-py runtime is set up, then runs the scripted
# demo under it (demo/live_systems/run_demo.py). REVL_DEMO_REQUIRE=1 makes a
# missing runtime fatal rather than a silent skip.
demo:
	@[ -x backends/python/.venv/bin/python ] || sh backends/python/setup.sh
	REVL_DEMO_REQUIRE=1 backends/python/.venv/bin/python demo/live_systems/run_demo.py

# formal/ — the machine-checked backbone (formal/STATUS.md). run_gate.sh:
# lake build, then the axioms gate (no theorem may depend on sorryAx — an
# unfinished proof — or any project-defined axiom; the machine-checked
# form of "a claim gets a command or it gets softened"), then the harness
# census. A missing elan/lake skips LOUDLY (never a false green), matching
# the pre-merge discipline. The logic lives in a script, not recipe lines:
# each recipe line is its own shell, so an in-Makefile skip-guard cannot
# stop the target.
formal:
	sh formal/scripts/run_gate.sh
