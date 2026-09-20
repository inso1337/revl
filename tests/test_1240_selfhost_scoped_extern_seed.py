"""`selfhost/lower.rvl` seeds a scoped extern by its declared token - issue #1240.

The self-host port of the emission fixed point (`emit_caps_reach`) seeded a
boundary-crossing extern with the extern's own NAME, and `p_extern` never read
the `[caps]` bracket at all, so there was no scope on `FnD` to seed with.
`docs/capabilities.md` §2 states the rule the reference follows everywhere: a
scope replaces the name; it does not join it.

Two halves, with different ages. The `witnessed` half predates issue #1234: the
reference has seeded `witnessed[fs] fn stash` with `fs` since item 243, while
the gate called it `stash`. The `emission` half opened when #1238 keyed the
reference's emission seed the same way. Neither fired, because no document in
the census corpus put a scoped extern behind a provide-method bound - the
oracles catch divergence, not a feature the port does not have. The documents
that make the agreement a measurement are in `ACCEPTED_PROGRAMS`
(`tests/test_selfhost_lower.py`); this file holds the halves that do not belong
in a corpus the census reads message-for-message.

`OUT_OF_BOUNDS` is the non-vacuity control: the subset check still REFUSES an
excess, and names the declared token as the excess. A seed that stopped
distinguishing anything would admit these and pass the corpus documents.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402


def _emitted() -> dict:
    """`selfhost/lower.rvl` compiled and run under the python backend - the
    same harness `tests/test_selfhost_lower.py` uses."""
    ir = compile_files([str(ROOT / "selfhost" / "lower.rvl")])
    spec = importlib.util.spec_from_file_location(
        "pyemit_selfhost_lower_1240", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "selfhost_lower.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def admit():
    return _emitted()["admit_src"]


IN_BOUNDS = """
extern emission[db] fn pg_write(t: Str) = @py { return }
service Worker { emission[db] fn go(t: Str) }
component W provides worker: Worker {
  provide worker { fn go(t) { emit pg_write(t) } }
}
"""

# genuinely out of bounds: the method promises `bus`, the body reaches `db`.
OUT_OF_BOUNDS = """
extern emission[db] fn pg_write(t: Str) = @py { return }
service Worker { emission[bus] fn go(t: Str) }
component W provides worker: Worker {
  provide worker { fn go(t) { emit pg_write(t) } }
}
"""

# an UNSCOPED extern still names itself (docs/capabilities.md §2). The rule the
# fix must not disturb: absent a scope the extern IS the boundary.
UNSCOPED_OUT_OF_BOUNDS = """
extern emission fn send_mail(t: Str) = @py { return }
service Worker { emission[bus] fn go(t: Str) }
component W provides worker: Worker {
  provide worker { fn go(t) { emit send_mail(t) } }
}
"""


def test_the_control_is_refused_by_both_trees(admit):
    """Non-vacuity. The subset check has not stopped checking: an excess is
    still an excess, on the reference and on the gate."""
    with pytest.raises(RevlError):
        compile_source(OUT_OF_BOUNDS, "out_of_bounds.rvl")
    assert admit(OUT_OF_BOUNDS).startswith("G4|")


def test_the_gate_names_the_declared_token_as_the_excess(admit):
    """The spelling, on both trees. `pg_write` is host code; `db` is the
    authority a `capability <glob>` rule can select, and it is the token the
    refusal has to hand the author (issue #1234)."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(OUT_OF_BOUNDS, "out_of_bounds.rvl")
    assert "emits through `db`" in str(excinfo.value)
    verdict = admit(OUT_OF_BOUNDS)
    assert "emits through `db`" in verdict
    assert "emits through `pg_write`" not in verdict


def test_an_unscoped_extern_still_names_itself(admit):
    with pytest.raises(RevlError) as excinfo:
        compile_source(UNSCOPED_OUT_OF_BOUNDS, "unscoped.rvl")
    assert "emits through `send_mail`" in str(excinfo.value)
    assert "emits through `send_mail`" in admit(UNSCOPED_OUT_OF_BOUNDS)


def test_the_in_bounds_provider_is_admitted_by_both_trees(admit):
    """The document's own statement, restated here so a reader of this file
    sees both directions without leaving it."""
    assert compile_source(IN_BOUNDS, "in_bounds.rvl")
    assert admit(IN_BOUNDS) == ""
