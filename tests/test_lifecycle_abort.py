"""`abort` in a `lifecycle test` body — roadmap item 377 (F-H1.7).

H1's flagship proof — perform witnessed mutations (write, overwrite, delete),
ABORT, then assert the workspace is residue-free / byte-identical to before —
could previously ONLY be written in host Python (tests/test_session_commit.py::
test_abort_reverts_witnessed_and_drops_deferred), because a `lifecycle test`
body had no `abort` statement. For a language whose pitch is "the guarantees
live in the language," the differentiator's own proof being inexpressible in its
test surface is a gap. This adds the `abort` lifecycle statement, which drives
the enclosing session frame's 245 abort (mark frames aborting, replay the
witnessed inverses, drop the deferral queue), so the H1 proof is pure `.rvl`.

The `abort` statement mirrors `revl.mcp.session.Session.abort` (item 245,
docs/design/245-session-commit.md): begin_abort -> dispose LIFO (inverses
replay) -> finalize_abort.
"""

import copy
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from revl.compiler import compile_source

ROOT = Path(__file__).resolve().parents[1]
CORDIS_PY = ROOT / "backends" / "python" / ".venv" / "bin" / "python"


# ---------------------------------------------------------------------------
# 1. `abort` parses and lowers inside a `lifecycle test` body
# ---------------------------------------------------------------------------

_SRC_MIN = (
    "service Cache {\n"
    "  fn get(key: Str) -> Opt[Str]\n"
    "  emission fn put(key: Str, value: Str)\n"
    "}\n"
    "component UserCache provides cache: Cache {\n"
    "  let store = effect Map.new() undo store.drop()\n"
    "  provide cache {\n"
    "    fn get(key) = store.get(key)\n"
    "    fn put(key, value) { effect store.insert(key, value) undo store.remove(key) }\n"
    "  }\n"
    "}\n"
    "lifecycle test \"abort tears the composition down\" {\n"
    "  load UserCache\n"
    "  call cache.put(\"k\", \"v\")\n"
    "  abort\n"
    "  assert no_residue\n"
    "}\n"
)


def test_abort_parses_and_lowers_in_a_lifecycle_body():
    ir = compile_source(_SRC_MIN, "lifecycle_abort_min.rvl")
    tests = ir.get("tests") or []
    (lc,) = [t for t in tests if t.get("lifecycle")]
    steps = [s.get("step") for s in lc["body"]]
    assert "abort" in steps, f"no abort step lowered: {steps}"


def test_abort_is_rejected_outside_a_lifecycle_body():
    """`abort` is a lifecycle statement; a plain `test` block is pure."""
    from revl.errors import RevlError
    src = (
        "test \"pure\" {\n"
        "  abort\n"
        "  assert 1 == 1\n"
        "}\n"
    )
    with pytest.raises(RevlError):
        compile_source(src, "abort_in_pure.rvl")
