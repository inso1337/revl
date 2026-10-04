"""Compiling never imports `revl.mcp` (issue #1780).

`revl.mcp` is the MCP server and the live session: a runtime surface.
Compiling a program used to put it on the import chain three ways:
- `revl.lower` imported its type schema from `revl.mcp.schema`, eagerly;
- `revl.attest` lazily imported `revl.gate`, which lazily imports
  `revl.mcp.session`;
- `revl.policy` and `revl.registry` lazily imported `revl.fault`, whose tier
  runners reach `revl.placement` and from there `revl.mcp`.

The first one actually ran: `import revl` loaded `revl.mcp` and
`revl.mcp.schema`. The others were import edges the compiler never took, but
`tools/affected_tests.py` cannot tell an edge that runs from one that does not,
so every `src/revl/mcp/` change selected the full root suite.

The type schema now lives in `revl.type_schema`, `attest` owns the version it
reports, and the two producers that run a program to make evidence live in
`revl.evidence_recompute` (injected into the policy evaluator) and
`revl.registry_evidence`.

Two tests pin it, one per side:
- **Measured.** A fresh interpreter compiles the corpus with an import hook
  that records every attempt to import `revl.mcp` or below, including one that
  would fail. There must be none. Every `revl` module the compile did load must
  also be on the static compile graph, which checks the static analysis against
  the run.
- **Static.** No `revl.mcp` module is on `compile_reachable`, so a change to
  one selects a narrow set.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "revl_affected_tests_1780", ROOT / "tools" / "affected_tests.py")
at = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(at)

#: Every `.rvl` the compile is driven over: the examples, every fixture and
#: corpus document under tests/, and the stdlib.
CORPUS_GLOBS = ("examples/**/*.rvl", "tests/**/*.rvl", "stdlib/*.rvl")

_PROBE = r'''
import glob, json, sys
watched = []


class Watch:
    """Records every attempt to import revl.mcp or below; finds nothing, so the
    normal import still happens and the measurement changes no behaviour."""

    def find_spec(self, name, path=None, target=None):
        if name == "revl.mcp" or name.startswith("revl.mcp."):
            watched.append(name)
        return None


sys.meta_path.insert(0, Watch())
sys.path.insert(0, sys.argv[1] + "/src")
import revl  # noqa: E402
from revl import compile_files  # noqa: E402
from revl.errors import RevlError  # noqa: E402

compiled = refused = 0
for pattern in json.loads(sys.argv[2]):
    for path in sorted(glob.glob(sys.argv[1] + "/" + pattern, recursive=True)):
        try:
            compile_files([path])
            compiled += 1
        except RevlError:
            refused += 1
        except Exception:  # noqa: BLE001 -- a crash is not this test's subject
            refused += 1
print(json.dumps({
    "watched": watched, "compiled": compiled, "refused": refused,
    "loaded": sorted(m for m in sys.modules if m == "revl" or m.startswith("revl.")),
}))
'''


def _measure() -> dict:
    done = subprocess.run(
        [sys.executable, "-c", _PROBE, str(ROOT), json.dumps(CORPUS_GLOBS)],
        capture_output=True, text=True, timeout=1800, check=False)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


_RESULT: dict = {}


def _result() -> dict:
    if not _RESULT:
        _RESULT.update(_measure())
    return _RESULT


def test_compiling_the_corpus_never_imports_revl_mcp():
    result = _result()
    # a vacuous run (nothing compiled) would pass the assertion below
    assert result["compiled"] > 300, result["compiled"]
    assert result["watched"] == [], sorted(set(result["watched"]))
    assert not [m for m in result["loaded"] if m.startswith("revl.mcp")]


def test_every_module_the_compile_loads_is_on_the_static_graph():
    """The static graph is a superset of what a real compile loads; a module
    loaded here and missing from it would make the selector unsound."""
    reach = at.compile_reachable(ROOT)
    loaded = {m[len("revl."):] if m != "revl" else "__init__"
              for m in _result()["loaded"]}
    missing = sorted(m for m in loaded
                     if m not in reach and f"{m}.__init__" not in reach)
    assert missing == []


def test_no_revl_mcp_module_is_on_the_static_compile_graph():
    reach = at.compile_reachable(ROOT)
    assert sorted(m for m in reach if m.startswith("mcp")) == []
    # and the module the compiler does need is reached, not missed
    assert "type_schema" in reach
