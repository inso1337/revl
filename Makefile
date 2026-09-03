# Convenience targets. The conformance matrix in docs/conformance.md is generated, never
# authored: `make matrix` regenerates it, and CI fails if the committed block
# drifts from a fresh generation (see .github/workflows/ci.yml).

.PHONY: matrix matrix-check matrix-execute docs-gen docs-check demo pre-merge pre-merge-affected formal roadmap-check workflow-permissions

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

# The roadmap's in-progress markers, checked against git. The roadmap is the
# state-of-record and its state is PROSE, so it rots silently: a marker reading
# "FIXING on `fix/x`" for a branch that merged hours ago costs the next agent a
# full re-investigation. Same contract as matrix-check: a claim that can be
# checked mechanically must be. See tools/check_roadmap_markers.py for what it
# can and cannot know, and CONTRIBUTING.md "Tracking work" for the discipline.
# Add --require-issue once the GitHub-issue migration lands.
roadmap-check:
	python3 tools/check_roadmap_markers.py --check-contradiction --check-delegation --check-duplicate-headers --check-orphan

# The same tool with all five prose checks on: self-contradiction, dangling
# delegation, orphaned findings, single-tier fixes for language-wide
# guarantees, and duplicate item headers. A, B, C and E are green on main and
# run in CI (the `roadmap-check` target above mirrors that line). D is the only
# one still red, with two real findings (items 421 F6 and 422 F6), so this
# target is RED on main by design: every finding it prints is a real finding.
# Run it before writing a roadmap item and after closing one.
roadmap-check-all:
	python3 tools/check_roadmap_markers.py --check-all

# issue #191: the workflow permission gate. A job-level `permissions` block
# REPLACES the workflow-level one rather than merging into it, and the release
# path is the one place where getting that wrong is invisible until a tag is
# already pushed. `--self-test` reintroduces the original bug into a synthetic
# workflow and asserts the gate catches it; `--strict` also fails a job with no
# block at any level. Both run in the `lint` job and in tools/pre_pr.sh.
workflow-permissions:
	uv run --no-project --with pyyaml python3 tools/check_workflow_permissions.py --self-test
	uv run --no-project --with pyyaml python3 tools/check_workflow_permissions.py --strict

# Regenerate the docs/conformance.md conformance + performance matrix in place.
matrix:
	python3 tools/conformance.py --write-readme

# The staleness gate CI runs: exit non-zero if the committed block is stale.
matrix-check:
	python3 tools/conformance.py --check-readme

# The matrix's third question (issue #244): RUN the cases that have an answer
# and check every tier computes the same one. `--validate` stops at compile
# depth and cannot see a tier that compiles and then means something else.
# A tier whose runtime is absent here reports `-`, never agreement.
matrix-execute:
	python3 tools/conformance.py --execute

# issue #255: the source-derived blocks in DESIGN.md and the five docs that
# carry them. `docs-gen` regenerates them in place; `docs-check` is the gate the
# frontend CI job runs. Run docs-gen after editing anything under docs/ (the
# DOC-STATUS inventory is a function of every docs/*.md) or after adding a CLI
# subcommand, an MCP verb or a diagnostic guarantee.
docs-gen:
	python3 tools/docgen.py --write

docs-check:
	python3 tools/docgen.py --check

# v3.0 gate E3: the live-systems demo (swap / why / apply) from a clean
# checkout. Ensures the cordis-py runtime is set up, then runs the scripted
# demo under it (demo/live_systems/run_demo.py). REVL_DEMO_REQUIRE=1 makes a
# missing runtime fatal rather than a silent skip.
demo:
	@[ -x backends/python/.venv/bin/python ] || sh backends/python/setup.sh
	REVL_DEMO_REQUIRE=1 backends/python/.venv/bin/python demo/live_systems/run_demo.py

# formal/ — the machine-checked backbone (formal/STATUS.md). run_gate.sh:
# the import-layering and non-vacuity gates (both toolchain-free, so they
# always run), then lake build, then the axioms gate (no theorem may
# depend on sorryAx, an unfinished proof, or on any project-defined axiom;
# the machine-checked form of "a claim gets a command or it gets
# softened") over both CheckAxioms.lean and harness/Oracle.lean's own
# print block — the oracle file is outside the RevL library root, so it
# needs its own pass — then the harness census. The non-vacuity gate is roadmap
# item 418 step 8: `#print axioms` is as clean on a theorem whose
# hypotheses cannot all hold as on a load-bearing one, so every registered
# theorem carries a row in formal/scripts/nonvacuity.tsv naming its
# evidence. A missing elan/lake skips LOUDLY (never a false green),
# matching the pre-merge discipline. The logic lives in a script, not
# recipe lines: each recipe line is its own shell, so an in-Makefile
# skip-guard cannot stop the target.
formal:
	sh formal/scripts/run_gate.sh
