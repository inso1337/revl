"""Test-root path bootstrap: make `pytest tests/ -q` work from any checkout
or worktree with no PYTHONPATH juggling.

Inserts `<rootdir>/src` ahead of sys.path only when it actually exists —
an installed-package environment (no in-tree src/) is left untouched, and
the backends/*/ suites keep owning their own loader paths (this conftest
scopes to tests/ only).
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"

if (_SRC / "revl").is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
