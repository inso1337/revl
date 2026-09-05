#!/bin/sh
# One-command setup for the revl cordis-py backend.
#
# Clones the target runtime (cordis-py, branch harden-fiber-lifecycle — the
# fork carrying the lifecycle reentrancy fixes the R1-R5 semantics depend
# on) at a PINNED revision (roadmap item 76(c): a fresh clone must not drift
# onto the branch's moving HEAD — bump CORDIS_PY_PIN deliberately), creates a
# local venv, and installs it editable with the test deps — plus revl itself,
# editable, so this interpreter can run the toolchain.
#
# The revl package is installed twice: once with `uv` for the cheap dep
# resolution, then once more with `python -m pip` so `[project.scripts]`
# actually materialises as the `revl` and `truc` console scripts on PATH.
# `uv pip install -e .` does not (the editable install resolves to a `.pth`
# file, no entry points); the second install through stock pip is what makes
# `revl` callable without `python -P -m revl` (the documented happy path is
# the console script; the `-P` is the PYTHONSAFEPATH safety bit, issue
# #317) and closes the CWD-shadowing window issue #317 names (issue #336).
# The success message points at `revl` for the same reason — the
# documented happy path is the one with no window.
#
# That is the command `revl run` prints when cordis is missing, and the one
# --placement prints in its preflight; both only work if revl is installed
# here, so it is installed here.
#
# Override CORDIS_PY to point at an existing clone.
set -eu
cd "$(dirname "$0")"

CORDIS_PY="${CORDIS_PY:-.cordis-py}"
# The TESTED commit, not the branch's moving HEAD. The repo's suite is
# validated against this exact vintage — 1c5e6f1 = the dict-plugin Config fix
# (roadmap 74b) on top of the 1316174 A8 async-body fiber-FAILED fix. A
# fresh clone must not drift onto the branch's moving HEAD (roadmap 76c);
# bump this pin deliberately when the runtime moves, and record why.
# Override with CORDIS_PY_PIN.
CORDIS_PY_PIN="${CORDIS_PY_PIN:-1c5e6f17abf538bf01012f9d72ce0cfa978d91b3}"
if [ ! -d "$CORDIS_PY" ]; then
    git clone --branch harden-fiber-lifecycle https://github.com/inso1337/cordis-py "$CORDIS_PY"
fi
# Existing clones (e.g. from an older setup.sh) may sit on the moving branch
# HEAD; always move the checkout to the tested pin.
git -C "$CORDIS_PY" fetch --quiet origin
git -C "$CORDIS_PY" checkout --quiet "$CORDIS_PY_PIN"

# --allow-existing: re-running setup on an existing venv must work, since the
# `revl run` diagnostic tells people to run exactly this line.
uv venv --allow-existing .venv
uv pip install --python .venv/bin/python pip
# `coverage` is here for the same reason `pytest` is: this venv runs the WHOLE
# `tests/` root in the `frontend-cordis` job, and that root includes the item
# 429 self-host coverage ratchets, which need a real tracer. The names are
# listed literally rather than installed as revl's `test` extra because the
# editable install below is `--editable ../..` with no extras and because
# pytest-asyncio, pyyaml and watchdog are not in that extra either; keep the
# two in step. Without coverage here those gates ERROR rather than skip, which
# is deliberate — a ratchet that goes quiet when its instrument is missing is
# the exact failure it exists to catch.
uv pip install --python .venv/bin/python pytest pytest-asyncio pyyaml watchdog coverage \
    --editable "$CORDIS_PY"
# Re-install revl through stock pip so `[project.scripts]` (the `revl` and
# `truc` console-script entries) are written to .venv/bin/. `uv pip install -e`
# resolves the editable to a `.pth` and skips the entry-point step; issue #336.
.venv/bin/python -m pip install --no-deps -e ../..

echo
echo "setup complete — run the suite with:  .venv/bin/pytest"
echo "                 run the demo with:   .venv/bin/python demo.py"
echo "                 run a composition:   revl run ../../examples/user_cache.rvl"
