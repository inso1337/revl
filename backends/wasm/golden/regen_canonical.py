#!/usr/bin/env python3
"""Regenerate this tier's goldens, the canonical-ABI ones included.

Kept as the path this tier's tests and docs point at; the producer itself lives
in `tools/regen_goldens.py`, the one place every backend's goldens are
regenerated and drift-checked from. Equivalent to:

    python3 tools/regen_goldens.py wasm

which covers more than this script used to: `functions.wat`, `Beacon.wat`,
`Auditor.wat` and `Pulse.wat` as well as the three canonical fixtures.
"""

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.exit(subprocess.run(
    [sys.executable, str(ROOT / "tools" / "regen_goldens.py"), "wasm"],
    cwd=ROOT).returncode)
