#!/bin/sh
# One-command setup for the revl cordis-py backend.
#
# Clones the target runtime (cordis-py, branch harden-fiber-lifecycle — the
# fork carrying the lifecycle reentrancy fixes the R1-R5 semantics depend
# on), creates a local venv, and installs it editable with the test deps.
#
# Override CORDIS_PY to point at an existing clone.
set -eu
cd "$(dirname "$0")"

CORDIS_PY="${CORDIS_PY:-.cordis-py}"
if [ ! -d "$CORDIS_PY" ]; then
    git clone --branch harden-fiber-lifecycle https://github.com/inso1337/cordis-py "$CORDIS_PY"
fi

uv venv .venv
uv pip install --python .venv/bin/python pytest pytest-asyncio pyyaml watchdog --editable "$CORDIS_PY"

echo
echo "setup complete — run the suite with:  .venv/bin/pytest"
echo "                 run the demo with:   .venv/bin/python demo.py"
