#!/bin/sh
# One-command setup for the revl cordis-py backend.
#
# Clones the target runtime (cordis-py, branch harden-fiber-lifecycle — the
# fork carrying the lifecycle reentrancy fixes the R1-R5 semantics depend
# on), creates a local venv, and installs it editable with the test deps —
# plus revl itself, editable, so this interpreter can run the toolchain:
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
# The TESTED commit, not the branch's moving HEAD. The repo's suite is
# validated against this exact vintage (docs/contract-errata.md, A8: the
# async-body fiber-FAILED fix at inso1337/cordis-py@harden-fiber-lifecycle
# commit 1316174, folded into geohotstan/cordis-py#1). Cloning the branch
# HEAD made vintage-pinned tests fail on a fresh worktree through no fault of
# the change under test — upstream moved and setup.sh followed it
# (dogfood/findings-faultres.md). Update this pin deliberately when the
# runtime moves, and record why. Override with CORDIS_PY_REV.
CORDIS_PY_REV="${CORDIS_PY_REV:-1316174a983bf4851efd072d04f139e0ed174f2f}"
if [ ! -d "$CORDIS_PY" ]; then
    git clone --branch harden-fiber-lifecycle https://github.com/inso1337/cordis-py "$CORDIS_PY"
fi
git -C "$CORDIS_PY" checkout --quiet "$CORDIS_PY_REV"

# --allow-existing: re-running setup on an existing venv must work, since the
# `revl run` diagnostic tells people to run exactly this line.
uv venv --allow-existing .venv
uv pip install --python .venv/bin/python pytest pytest-asyncio pyyaml watchdog \
    --editable "$CORDIS_PY" --editable ../..

echo
echo "setup complete — run the suite with:  .venv/bin/pytest"
echo "                 run the demo with:   .venv/bin/python demo.py"
echo "                 run a composition:   .venv/bin/python -m revl run ../../examples/user_cache.rvl"
