"""A value graph is not a tree: the confidential walks must terminate on a cycle.

`backends/python/confidential.py` walks a value three times — `register_secret_tree`
at a declared marking, `_needles` before already-rendered host text is scrubbed,
and `redact_value` before a value is kept anywhere durable. All three recursed
with no bound and no visited set, so a self-referential value raised
`RecursionError` INSIDE the funnel. What that costs is not a lost redaction but a
lost call: a `Secret[T]`-returning extern whose `@py` body returns a container
that contains itself died in `@_revl_secret_result` before it could return, and a
capture point died before it wrote its record. Nothing here is exotic to build —
a `@py` body is verbatim python, so `a.append(a)` is enough, and the traceback
points at `confidential.py` rather than at the author's bridge.

The guard is a PATH, not a depth. A depth cap terminates as well, but it also
drops the leaves of any legal value deeper than the cap — the confidentiality
regression these walks exist to prevent. A path reaches every leaf a cycle does
not cut off, because a back-edge can only lead to a node the walk is already
inside. `test_a_shared_sublist_is_still_rendered_at_both_positions` is the other
half of that claim: a node two siblings share is released once it is done, so a
DAG is still rendered in full at both.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

import confidential  # noqa: E402  (load path mirrors test_secret_externalization.py)

CANARY = "SEKRIT-CANARY-CYC-9"   # >= _MIN_MARKABLE
PUBLIC = "PUBLIC-NOTE-CYC-9"


@pytest.fixture(autouse=True)
def _isolate_registry():
    confidential.forget_secret_values()
    yield
    confidential.forget_secret_values()


def _self_referential_list() -> list:
    value: list = [PUBLIC, CANARY]
    value.append(value)
    return value


def _self_referential_dict() -> dict:
    value: dict = {"note": CANARY}
    value["self"] = value
    return value


class _Node:
    """A record-shaped container: `_members` reaches the cycle through
    `__dict__`, which is the path an emitted record takes."""

    def __init__(self, leaf: str) -> None:
        self.leaf = leaf
        self.me = self


# --------------------------------------------------------------------------
# the three walks terminate, and still do their job
# --------------------------------------------------------------------------


def test_a_self_referential_list_still_registers_its_leaf():
    confidential.register_secret_tree(_self_referential_list())
    assert confidential.is_secret_value(CANARY)
    assert confidential.redact_text(f"note: {CANARY}") == "note: <redacted:secret>"


def test_a_self_referential_dict_still_registers_its_leaf():
    confidential.register_secret_tree(_self_referential_dict())
    assert confidential.is_secret_value(CANARY)


def test_two_containers_pointing_at_each_other_still_register():
    """The back-edge does not have to be a self-reference to close the loop."""
    first: list = [CANARY]
    second: list = [first]
    first.append(second)
    confidential.register_secret_tree(first)
    assert confidential.is_secret_value(CANARY)


def test_a_cycle_behind_a_record_member_still_registers():
    confidential.register_secret_tree(_Node(CANARY))
    assert confidential.is_secret_value(CANARY)


def test_a_self_referential_argument_still_contributes_its_needles():
    """`_needles` is the only cover for a CALLER'S OWN argument inside free-form
    host text, so raising here meant the sink printed the value verbatim."""
    text = f"seam refused: {CANARY} was rejected"
    assert confidential.redact_call_text(text, (_self_referential_list(),)) == (
        "seam refused: <redacted:arg> was rejected"
    )


def test_redact_value_marks_the_back_edge_and_keeps_the_leaves():
    rendered = confidential.redact_value(_self_referential_list())
    assert rendered[0] == PUBLIC          # an ordinary value is untouched
    assert rendered[1] == CANARY          # unregistered: the funnel is exact-match
    assert rendered[2] == confidential.RECURSIVE


def test_a_shared_sublist_is_still_rendered_at_both_positions():
    """The guard is a path, not a global visited set: releasing a node when it is
    done is what keeps a DAG — which is not a cycle — fully rendered."""
    shared = ["inner"]
    rendered = confidential.redact_value([shared, shared])
    assert rendered == [["inner"], ["inner"]]


def test_a_cyclic_value_is_still_scrubbed_at_every_sink():
    value = _self_referential_list()
    confidential.register_secret_tree(value)
    assert confidential.redact_text(f"trace {CANARY} end") == "trace <redacted:secret> end"
    assert confidential.redact_value(value)[1] == confidential.REDACTED
    assert confidential.redact_value(value)[2] == confidential.RECURSIVE


# --------------------------------------------------------------------------
# end to end: a `Secret[T]`-returning extern whose body returns a cycle
# --------------------------------------------------------------------------

# `a.append(a)` is what a `@py` body does when it builds a graph rather than a
# tree — a parent pointer, a cache that holds its own table, a linked structure
# closed on itself. The front end admits the `Secret[List[Str]]` return, so the
# emitted `@_revl_secret_result` wrapper is the only thing standing between the
# call and a `RecursionError` raised out of the confidentiality funnel.
CYCLIC = '''
extern pure fn cyc() -> Secret[List[Str]] = @py {
    a = ["SEKRIT-CANARY-CYC-9"]
    a.append(a)
    return a
}
'''


def _emit_and_exec(source: str) -> dict:
    from revl import compile_source

    spec = importlib.util.spec_from_file_location(
        "pyemit_cycle_guard", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    namespace: dict = {}
    code = str(module.emit(compile_source(source, "cycle.rvl")))
    exec(compile(code, "cycle_emitted.py", "exec"), namespace)  # noqa: S102
    return namespace


def test_a_secret_result_extern_returning_a_cycle_returns_and_is_registered():
    namespace = _emit_and_exec(CYCLIC)
    value = namespace["cyc"]()          # used to raise RecursionError here
    assert value[0] == CANARY
    assert confidential.is_secret_value(CANARY)
    assert confidential.redact_text(f"leaked {CANARY}") == "leaked <redacted:secret>"
