"""A derived inverse acts on the value its effect used (docs/closures.md, G7/A8).

`var` is function-local, so a provide-method body is the ONE place a mutable
local and an `effect … undo …` coexist. The inverse runs LATER — at teardown, or
on abort — so if it reads the local's CELL rather than a snapshot, a plain
reassignment after the effect registers silently changes what the accumulator
will undo:

    var k = key
    effect claim(k) undo release(k)
    k = "zz"                          # the inverse now releases "zz"

That is not "the inverse failed to run". It runs, successfully, against a value
the forward step never touched — so teardown MUTATES A BYSTANDER and leaves the
real acquisition behind, while G7 counts the entry as discharged, A8 reports the
revert as complete, and R4 reports no residue. Every report is clean and every
one of them is wrong.

docs/closures.md §Scope already promises the opposite ("the teardown accumulator
never holds a closure over a mutable cell … G7/A8 hold by construction"), and
docs/syntax-2.0.md §3.5 states the rule the promise rests on: a capture takes
the name's CURRENT VALUE, not the cell. An arrow literal gets that from the IR
`captures` list; a derived inverse is not an arrow, so it never did.

The assertions here are on OBSERVABLE behaviour — the ordered host calls a
component actually made — not on emitter strings or accumulator internals. The
emitted-shape checks that follow are per-tier scope, not the proof.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the teardown proof runs against a live cordis-py composition — "
           "install it with `sh backends/python/setup.sh`",
)

# `claim`/`release` append to an order log, so the FORWARD acquisition and the
# INVERSE that replays at teardown are both visible from outside the process —
# the observable surface the assertions read. `k` is reassigned after the effect
# has registered, which is the whole point.
_SOURCE = (
    "extern pure fn claim(k: Str) -> Unit = @py {\n"
    "    import os\n"
    "    with open(os.environ['REVL_INVERSE_LOG'], 'a', encoding='utf-8') as f:\n"
    "        f.write('claim:' + k + chr(10))\n"
    "    return\n"
    "}\n"
    "extern pure fn release(k: Str) -> Unit = @py {\n"
    "    import os\n"
    "    with open(os.environ['REVL_INVERSE_LOG'], 'a', encoding='utf-8') as f:\n"
    "        f.write('release:' + k + chr(10))\n"
    "    return\n"
    "}\n"
    "service Ops { fn take(key: Str) }\n"
    "component C provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn take(key) {\n"
    "      var k = key\n"
    "      effect claim(k)\n"
    "      undo   release(k)\n"
    "      k = \"zz\"\n"
    "    }\n"
    "  }\n"
    "}\n"
)

# The same body with the reassignment REMOVED: the inverse's value cannot drift,
# so this is what a correct run looks like whichever capture rule is in force.
_SOURCE_NO_REASSIGN = _SOURCE.replace('      k = "zz"\n', "")


@pytest.fixture
def inverse_log(tmp_path, monkeypatch):
    path = tmp_path / "inverse.log"
    monkeypatch.setenv("REVL_INVERSE_LOG", str(path))
    return path


def _ops(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _run(source: str, log: Path) -> list[str]:
    """Load, call `ops.take("alpha")`, tear down. Returns the ordered host calls."""
    from revl.mcp.session import Session

    session = Session()
    session.load(compile_source(source, "inverse.rvl"))
    try:
        session.call("ops", "take", ["alpha"])
    finally:
        session.unload()
    return _ops(log)


# ---------------------------------------------------------------------------
# The proof: the inverse acts on the value its effect used.
# ---------------------------------------------------------------------------

@needs_cordis
def test_inverse_releases_the_key_its_effect_claimed(inverse_log):
    """`take("alpha")` claims "alpha", so teardown must release "alpha"."""
    assert _run(_SOURCE, inverse_log) == ["claim:alpha", "release:alpha"]


@needs_cordis
def test_a_later_reassignment_does_not_move_the_inverse(inverse_log):
    """The reassignment is the only difference between the two bodies, and it is
    dead by the time the method returns — so it may not change one observable
    call. Pinning the WHOLE trace equal is what separates "acts on the right
    value" from "happens not to have run"."""
    with_reassign = _run(_SOURCE, inverse_log)
    inverse_log.unlink()
    without = _run(_SOURCE_NO_REASSIGN, inverse_log)
    assert with_reassign == without


@needs_cordis
def test_teardown_does_not_touch_a_bystander(inverse_log):
    """The sharp edge, stated on its own: the wrong-value inverse does not merely
    fail to release "alpha" — it RELEASES SOMETHING ELSE. A teardown that mutates
    a key the component never acquired is worse than one that leaves residue,
    because the residue report cannot see it."""
    ops = _run(_SOURCE, inverse_log)
    assert "release:zz" not in ops, (
        "teardown released a key the forward effect never claimed")
    assert "release:alpha" in ops, "the claimed key was never released"


# ---------------------------------------------------------------------------
# Per-tier scope. A tier whose closures capture by reference (python, typescript,
# go) had the same hole; java could not even compile the shape (a lambda may not
# read a non-effectively-final local); rust already snapshotted, by cloning each
# free name into its `move` closure ahead of the reassignment. The lowering now
# names the captures once, in the IR, and each emitter binds them by value.
# ---------------------------------------------------------------------------

def test_lowering_names_the_captured_local_on_the_step():
    """The one place the capture SET is decided — the emitters read it, none of
    them re-derives it."""
    ir = compile_source(_SOURCE, "inverse.rvl")
    [method] = [m for step in ir["components"][0]["body"]
                if step.get("step") == "provide"
                for m in step["methods"] if m["name"] == "take"]
    [effect] = [s for s in method["body"] if s.get("step") == "effect"]
    assert effect["undo_captures"] == ["k"]


def test_a_body_with_no_reassignable_read_is_unchanged():
    """The annotation appears only where an inverse actually reads a mutable
    local, so the IR of every other program is byte-identical."""
    ir = compile_source(_SOURCE_NO_REASSIGN.replace("var k = key", "let k = key"),
                        "inverse.rvl")
    [method] = [m for step in ir["components"][0]["body"]
                if step.get("step") == "provide"
                for m in step["methods"] if m["name"] == "take"]
    [effect] = [s for s in method["body"] if s.get("step") == "effect"]
    assert "undo_captures" not in effect


# The same body over a host `Map`, so it renders on every tier (the `@py` extern
# bodies above are python-only).
_PORTABLE = (
    "service Ops { fn get(key: Str) -> Opt[Str]  fn take(key: Str) }\n"
    "component C provides ops: Ops {\n"
    "  let store = effect Map.new() undo store.drop()\n"
    "  provide ops {\n"
    "    fn get(key) = store.get(key)\n"
    "    fn take(key) {\n"
    "      var k = key\n"
    "      effect store.insert(k, \"v\")\n"
    "      undo   store.remove(k)\n"
    "      k = \"zz\"\n"
    "    }\n"
    "  }\n"
    "}\n"
)


@pytest.mark.parametrize("backend, pinned", [
    # a python default argument
    ("python", "yield lambda k=k: store.remove(k)"),
    # a TypeScript IIFE around the arrow (a default parameter cannot spell it:
    # the initialiser's right-hand side resolves to the parameter and hits the TDZ)
    ("typescript", "((k: any) => () => store.remove(k))(k)"),
    # a Go inner-scope shadow, inside the effect closure
    ("go", "k := k"),
    # a Java `final` copy, which also restores the effectively-final property
    # javac requires of anything a lambda reads
    ("java", "final var __revl_inverse_1_k = k;"),
])
def test_every_reference_capturing_tier_binds_the_inverse_by_value(backend, pinned):
    """One tier per capture idiom. Rust is absent because it never had the defect:
    it clones each of the inverse's free names into the `move` closure at the
    effect site, ahead of any reassignment."""
    module = __import__(f"backends.{backend}.emit", fromlist=["emit"])
    rendered = module.emit(compile_source(_PORTABLE, "inverse.rvl"))
    assert pinned in rendered, rendered


def test_rust_already_snapshotted_and_is_untouched():
    """Pinned so a later change to the shared lowering cannot quietly move the one
    tier that was already correct."""
    from backends.rust import emit as rust_emit

    rendered = rust_emit.emit(compile_source(_PORTABLE, "inverse.rvl"))
    assert "let k_undo = k.clone();" in rendered, rendered
