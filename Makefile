# Convenience targets. The conformance matrix in README.md is generated, never
# authored: `make matrix` regenerates it, and CI fails if the committed block
# drifts from a fresh generation (see .github/workflows/ci.yml).

.PHONY: matrix matrix-check

# Regenerate the README conformance + performance matrix in place.
matrix:
	python3 tools/conformance.py --write-readme

# The staleness gate CI runs: exit non-zero if the committed block is stale.
matrix-check:
	python3 tools/conformance.py --check-readme
