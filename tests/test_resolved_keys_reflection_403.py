"""The in-language `resolved_keys()` reflection query — roadmap item 403.

A running composition asks what keys it itself provides/resolves through the
un-privileged in-language surface (`stdlib/reflect.rvl`), instead of the harness
reaching into the runtime through a CLASSIFIED host escape hatch. The query is a
plain (non-`emission`) service method backed by a `pure` extern: reading what you
provide changes nothing, so it is on the G8 audit surface as an ordinary pure
read, NOT a privileged crossing. That is the whole of item 403 — landing it
REMOVES a privileged crossing.

The runtime proofs need a live cordis composition (install with
`sh backends/python/setup.sh`, run under its venv).
"""

import copy
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the resolved_keys() reflection query is proven against a live "
           "cordis-py composition — install it with `sh backends/python/setup.sh`",
)

# The running composition: two components that each provide a key with a live
# provider (no unmet injection, so both activate), PLUS the in-language
# reflection query from stdlib/reflect.rvl (which itself provides `reflection`).
_BASE = (
    "service Db { fn ping() -> Str }\n"
    "service Cache { fn ping() -> Str }\n"
    "component PgDatabase provides db: Db {\n"
    '  provide db { fn ping() = "db" }\n'
    "}\n"
    "component MemCache provides cache: Cache {\n"
    '  provide cache { fn ping() = "cache" }\n'
    "}\n"
)


def _base_ir():
    # the running composition = the base tools + stdlib/reflect.rvl, composed as
    # co-root files (components are never `use`-imported).
    from revl import compile_files
    from revl._paths import stdlib_root
    reflect_path = str(stdlib_root() / "reflect.rvl")
    base_abs = os.path.abspath("base_403.rvl")
    return compile_files([base_abs, reflect_path], sources={base_abs: _BASE})


def _session_loaded():
    from revl.mcp.session import Session
    s = Session()
    s.load(copy.deepcopy(_base_ir()))
    return s


def _linker_provided_keys(ir: dict) -> set:
    """Every key the linker recorded a component as providing — the static
    provision set the runtime resolves a live subset of."""
    keys: set = set()
    for entry in (ir.get("manifest") or {}).get("components") or []:
        keys |= set(entry.get("provides") or [])
    return keys


# --------------------------------------------------------------------------- #
# Compile-only: the query is wired as a PURE (un-privileged) crossing.
# --------------------------------------------------------------------------- #

def test_reflection_query_is_wired_pure_not_privileged():
    ir = _base_ir()
    # `ReflectGate` provides `reflection: Reflection`, backed by the `pure`
    # `host_resolved_keys` extern — the in-language surface, on the G8 boundary
    # as an ordinary pure read (NOT an `emission`/classified crossing).
    names = {c["name"] for c in ir["components"]}
    assert "ReflectGate" in names
    ext = next((e for e in ir.get("externs") or []
                if e["name"] == "host_resolved_keys"), None)
    assert ext is not None, "host_resolved_keys extern was not wired"
    assert ext["class"] == "pure", (
        "resolved_keys() must be an UN-privileged reflection read, not an "
        f"emission/classified crossing (got class {ext['class']!r})")
    # the linker records the provisions the query reflects
    assert {"db", "cache", "reflection"} <= _linker_provided_keys(ir)


# --------------------------------------------------------------------------- #
# Runtime: the query returns the composition's resolved provisions, sorted.
# --------------------------------------------------------------------------- #

@needs_cordis
def test_resolved_keys_returns_the_composition_provisions_sorted():
    session = _session_loaded()
    # call the query the way a program (or the harness panel) would — a plain
    # service call on the provided `reflection` key.
    out = session.call("reflection", "resolved_keys", [])["result"]

    # 1. it returns exactly the running composition's resolved provisions:
    #    db + cache from the base, reflection from the query gate itself.
    assert out == ["cache", "db", "reflection"]

    # 2. it is deterministically SORTED (a stable canonical order).
    assert out == sorted(out)

    # 3. it matches what the linker recorded the composition as providing (every
    #    provider here activates, so the resolved set equals the provided set).
    assert set(out) == _linker_provided_keys(session.ir)

    # 4. it agrees with the runtime driver's own resolved_keys() — the query is
    #    a faithful in-language view of the same FIBERS/ROOT-consistent set the
    #    session's state() report derives from, not a separate accounting.
    assert set(out) == set(session._driver.resolved_keys())
    assert out == session.state()["providedKeys"]
