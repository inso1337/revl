"""A python a test starts imports `revl` from the tree under test.

tools/hooks/pre-commit runs pytest with whichever interpreter it finds, and in
a linked worktree that is the MAIN checkout's `.venv`. Its editable install of
revl is a plain `.pth` path entry naming the main checkout's `src/`, so every
python started without this tree on PYTHONPATH imported the main checkout's
revl. `tests/conftest.py` puts this tree's `src/` on `sys.path` for the pytest
process itself, which is why in-process imports were right and only children
were wrong: a test that shells out to `python -m revl ...` or a tool measured a
different tree from the one being committed, with nothing to say so.

PYTHONPATH entries come before site-packages and before any path a `.pth`
appends, so exporting this tree's `src/` there is enough; measured against the
main checkout's `.venv` with python 3.14. This test starts a child the way most
tests do (inherited environment, `sys.executable`) from a directory outside the
tree, so neither the working directory nor pytest's own path handling can
supply the answer.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_a_child_python_imports_revl_from_this_tree(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-c", "import revl; print(revl.__file__)"],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    got = Path(proc.stdout.strip()).resolve()
    want = (ROOT / "src" / "revl" / "__init__.py").resolve()
    print(f"child revl: {got}")
    assert got == want, (
        f"a child python imported revl from {got}, not from the tree under "
        f"test ({want})")


def test_the_hook_exports_this_trees_src_before_any_step():
    """The conftest export covers pytest's children. The hook's other steps
    (the conformance matrix, the selector) start python too, so the hook
    exports the same path itself, before its first step."""
    lines = [line.strip() for line in
             (ROOT / "tools" / "hooks" / "pre-commit").read_text(
                 encoding="utf-8").splitlines()]
    assign = lines.index('PYTHONPATH="$root/src${PYTHONPATH:+:$PYTHONPATH}"')
    assert lines[assign + 1] == "export PYTHONPATH"
    first_step = next(i for i, s in enumerate(lines) if s.startswith("step "))
    assert assign < first_step
