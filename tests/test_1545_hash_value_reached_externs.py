"""Issue #1545: the standing-approval candidate hash (item 427 F4) covers the
`@py` body of an extern reached as a FUNCTION VALUE.

`ClassMap._reached_host_code` closed the hash over CALLED names only. An extern
reached as a value has no call site naming it, so its body was left out of the
hash: a swap that rewrote only that body kept every standing grant valid, and
the rewritten body ran under a grant minted for the old one. Measured before
the fix, on the eight spellings in `tests/test_deploy_118.py`: the hash was
unchanged for 8/8, and the swapped body ran without a prompt for the 7 that run
on the py tier.

The closure now walks the call AND value channels the G4 fixed point folds,
at the scope and in every fn body on the way.
"""

from __future__ import annotations

import copy
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
sys.path.insert(0, str(ROOT / "tests"))

from _load_by_path import load_by_path  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.mcp.approval import ApprovalRequired, ClassMap  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="runs the swapped host body in a live cordis-py composition; "
           "install it with `sh backends/python/setup.sh`",
)


# `tests/test_deploy_118.py` holds the eight spellings; loading it under its
# own module name shares one copy with pytest's collection of that file.
_T118 = load_by_path("test_deploy_118", ROOT / "tests" / "test_deploy_118.py")
SPELLINGS = sorted(_T118._FN_VALUE_SPELLINGS)
# Every spelling runs end to end. The record-field one used to be `r.f(n)`,
# which crashed on the py tier with AttributeError; that call is refused in a
# component body since issue #1547, and the spelling now reads the function
# off the field and calls it through a binding, which runs.
RUNNABLE = list(SPELLINGS)

_EXTERN = """extern emission fn charge(n: Int) -> Int = @py {{
    with open({sink!r}, 'a') as _f: _f.write({dest!r} + chr(10))
    return n
}}
"""


def _ir(sink: str, dest: str, spelling: str) -> dict:
    source = _T118._fn_value_source(_EXTERN.format(sink=sink, dest=dest), spelling)
    return copy.deepcopy(compile_source(source, "item1545.rvl"))


def _sink_lines(sink: str) -> list:
    if not os.path.exists(sink):
        return []
    return Path(sink).read_text(encoding="utf-8").splitlines()


@pytest.fixture
def sink(tmp_path):
    return str(tmp_path / "crossings.log")


# --- the hash ----------------------------------------------------------------


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_a_body_change_moves_the_candidate_hash(sink, spelling):
    good = _ir(sink, "good.example", spelling)
    evil = _ir(sink, "evil.example", spelling)
    assert good["components"] == evil["components"], \
        "the fixture no longer isolates the extern body"
    assert ClassMap(good).candidate_hash({"C"}) \
        != ClassMap(evil).candidate_hash({"C"})


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_an_unchanged_recompile_keeps_the_candidate_hash(sink, spelling):
    a = _ir(sink, "good.example", spelling)
    b = _ir(sink, "good.example", spelling)
    assert ClassMap(a).candidate_hash({"C"}) == ClassMap(b).candidate_hash({"C"})


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_the_hashed_host_code_covers_the_checkers_reach(sink, spelling):
    """Every extern the checker's reach finds (`Composition._host_routes`, the
    walk behind the `externs` facts) is in the hashed closure."""
    from revl.query import _channels

    cmap = ClassMap(_ir(sink, "good.example", spelling))
    hashed, _fns = cmap._reached_host["C"]
    for sid in cmap.index.scopes_of["C"]:
        routes, _ = cmap.index._host_routes(
            *_channels(cmap.index.scopes[sid]["nodes"]))
        assert set(routes) <= hashed
    assert "charge" in hashed


# --- the executed consequence ------------------------------------------------


def _granted_session(sink: str, spelling: str):
    from revl.mcp.session import Session

    session = Session()
    session.approval_policy = "auto"
    session.load(_ir(sink, "good.example", spelling), record=True)
    with pytest.raises(ApprovalRequired) as caught:
        session.call("s", "go", [1])
    ticket = caught.value.ticket
    for capability in ticket["classCCapabilities"]:
        session.mint_standing_grant(ticket_hash=ticket["hash"],
                                    capability=capability, uses=9)
    session.call("s", "go", [2])
    assert _sink_lines(sink) == ["good.example"]
    return session


@needs_cordis
@pytest.mark.parametrize("spelling", RUNNABLE)
def test_a_standing_grant_fails_closed_when_the_value_reached_body_is_swapped(
        sink, spelling):
    session = _granted_session(sink, spelling)
    session.swap(_ir(sink, "evil.example", spelling))
    with pytest.raises(ApprovalRequired):
        session.call("s", "go", [3])
    assert _sink_lines(sink) == ["good.example"]   # the swapped body never ran


@needs_cordis
@pytest.mark.parametrize("spelling", RUNNABLE)
def test_a_standing_grant_survives_a_swap_that_changes_nothing(sink, spelling):
    session = _granted_session(sink, spelling)
    session.swap(_ir(sink, "good.example", spelling))
    session.call("s", "go", [3])
    assert _sink_lines(sink) == ["good.example", "good.example"]
