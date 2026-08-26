# Convenience targets. The conformance matrix in docs/conformance.md is generated, never
# authored: `make matrix` regenerates it, and CI fails if the committed block
# drifts from a fresh generation (see .github/workflows/ci.yml).

.PHONY: matrix matrix-check demo

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
