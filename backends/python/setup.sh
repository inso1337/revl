#!/bin/sh
# One-command setup for the revl cordis-py backend.
#
# Clones the target runtime (cordis-py, branch harden-fiber-lifecycle — the
# fork carrying the lifecycle reentrancy fixes the R1-R5 semantics depend
# on) at a PINNED revision (roadmap item 76(c): a fresh clone must not drift
# onto the branch's moving HEAD — bump CORDIS_PY_PIN deliberately), creates a
# local venv, and installs it editable with the test deps — plus revl itself,
# editable, so this interpreter can run the toolchain:
#
#     backends/python/.venv/bin/python -m revl run app.rvl
#     backends/python/.venv/bin/python -m revl run app.rvl --placement p.toml --once
#
# That is the command `revl run` prints when cordis is missing, and the one
# --placement prints in its preflight; both only work if revl is installed
# here, so it is installed here.
#
# Override CORDIS_PY to point at an existing clone.
set -eu
cd "$(dirname "$0")"

CORDIS_PY="${CORDIS_PY:-.cordis-py}"
# 1c5e6f1 = the dict-plugin Config fix (74b), on top of the 1316174 A8 fix.
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
uv pip install --python .venv/bin/python pytest pytest-asyncio pyyaml watchdog \
    --editable "$CORDIS_PY" --editable ../..

echo
echo "setup complete — run the suite with:  .venv/bin/pytest"
echo "                 run the demo with:   .venv/bin/python demo.py"
echo "                 run a composition:   .venv/bin/python -m revl run ../../examples/user_cache.rvl"
