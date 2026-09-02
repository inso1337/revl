#!/usr/bin/env python3
"""Regenerate this tier's goldens.

Kept as the path `package.json` ("npm run golden") and this tier's docs point
at; the producer itself lives in `tools/regen_goldens.py`, the one place every
backend's goldens are regenerated and drift-checked from. Equivalent to:

    python3 tools/regen_goldens.py typescript
"""

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.exit(subprocess.run(
    [sys.executable, str(ROOT / "tools" / "regen_goldens.py"), "typescript"],
    cwd=ROOT).returncode)
