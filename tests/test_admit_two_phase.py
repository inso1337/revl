"""Two-phase admission, Slice 0: the surface epoch and the content digests.

`docs/design/460-two-phase-admission-forward-recovery.md` §3, §7. Slice 0 lands
the CAS primitives the later slices will decide on, COMPUTED but not yet DRIVING
any recovery (the compute-but-do-not-yet-decide discipline): a per-session
`_surface_epoch` that moves on every class-map install, the `(baseManifestHash,
classMapDigest)` content digests recomputed from the live composition, and a
`_cas_surface` helper that refuses on drift. No WAL record and no decision is
written yet — Slices 1-3 add those.

The exit test the slice plan names:

  * the epoch increments once per `load`, `swap`, `undo`, `rollback` and
    `_wire_turn`, and NOT on `call`;
  * the digest is stable across two builds of the same class map and differs
    when a provider's class changes — INCLUDING the §3 load-bearing case where
    the manifest hash is unchanged but a granted provider's crossing class moved;
  * the helper refuses on either half moving (the in-process `(generation,
    surfaceEpoch)` pair or the across-restart content digests).

The epoch/`_wire_turn`/`swap` half needs a live cordis composition and is gated
on it; the digest and CAS-helper half is pure over the compiler + the class map
and runs everywhere.
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
    reason="the epoch moves are proven against a live cordis-py composition "
           "(load/swap/undo/wire) — install it with `sh backends/python/setup.sh`",
)


# --------------------------------------------------------------------------- #
# Sources. `_SRC_C` is a granted tool whose sole crossing is an immediate
# emission — class (c). `_SRC_NOOP` is the SAME composition (same component,
# same key, same manifest) whose provider no longer crosses — class folds away.
# `_SRC_OTHER` renames the component, so the manifest itself moves.
# --------------------------------------------------------------------------- #

_SRC_C = (
    "extern emission fn announce(sink: Str, msg: Str) = @py { return }\n"
    "service Ops { emission fn shout(sink: Str, msg: Str) }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops { fn shout(sink, msg) { emit announce(sink, msg) } }\n"
    "}\n"
)

_SRC_NOOP = (
    "extern emission fn announce(sink: Str, msg: Str) = @py { return }\n"
    "service Ops { fn shout(sink: Str, msg: Str) }\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops { fn shout(sink, msg) { } }\n"
    "}\n"
)

_SRC_OTHER = (
    "extern emission fn announce(sink: Str, msg: Str) = @py { return }\n"
    "service Ops { emission fn shout(sink: Str, msg: Str) }\n"
    "component Herald provides ops: Ops {\n"
    "  provide ops { fn shout(sink, msg) { emit announce(sink, msg) } }\n"
    "}\n"
)

# An untrusted per-turn source with NO host code of its own — it only forwards
# to the granted `ops`, the shape `test_admit_approval_gate` gates.
_TURN_FORWARD = (
    "service Turn { emission fn run(sink: Str, msg: Str) }\n"
    "component TurnComp requires ops: Ops provides turn: Turn {\n"
    "  provide turn {\n"
    '    fn run(sink, msg) { emit ops.shout(sink, msg) }\n'
    "  }\n"
    "}\n"
)


def _compile(src):
    from revl import compile_files
    p = os.path.abspath("base.rvl")
    return compile_files([p], sources={p: src})


def _class_map(src):
    from revl.mcp.approval import ClassMap
    return ClassMap(_compile(src))


# --------------------------------------------------------------------------- #
# classMapDigest — stable across two builds, moves on a class change.
# --------------------------------------------------------------------------- #

def test_class_map_digest_is_stable_across_two_builds_of_the_same_map():
    from revl.mcp.session import _class_map_digest_of
    a = _class_map_digest_of(_class_map(_SRC_C))
    b = _class_map_digest_of(_class_map(_SRC_C))
    assert a is not None
    assert a == b, "the digest is not stable over two builds of the same map"


def test_class_map_digest_moves_when_a_providers_class_changes_manifest_held():
    """§3's load-bearing case: the manifest hash is UNCHANGED, but a granted
    provider's crossing class moved (class (c) -> folds away), so the class-map
    digest is the only half that catches it. This is exactly the surface a
    decision must not carry across."""
    from revl.mcp.session import _base_manifest_hash_of, _class_map_digest_of
    ir_c = _compile(_SRC_C)
    ir_n = _compile(_SRC_NOOP)
    # the manifest is identical: same component, same key, same load order.
    assert _base_manifest_hash_of(ir_c) == _base_manifest_hash_of(ir_n)
    # the class map is NOT: the provider's fold moved from (c) to none.
    from revl.mcp.approval import ClassMap
    assert ClassMap(ir_c)._reach["Agent:ops.shout"]["class"] == "c"
    assert ClassMap(ir_n)._reach["Agent:ops.shout"]["class"] is None
    assert _class_map_digest_of(ClassMap(ir_c)) \
        != _class_map_digest_of(ClassMap(ir_n)), \
        "the class-map digest did not move on a provider reclassification"


# --------------------------------------------------------------------------- #
# baseManifestHash — stable across two builds, moves when the manifest moves.
# --------------------------------------------------------------------------- #

def test_base_manifest_hash_is_stable_and_moves_with_the_manifest():
    from revl.mcp.session import _base_manifest_hash_of
    a = _base_manifest_hash_of(_compile(_SRC_C))
    b = _base_manifest_hash_of(_compile(_SRC_C))
    assert a is not None and a == b, "manifest hash unstable over two builds"
    # a different component name is a different manifest.
    assert _base_manifest_hash_of(_compile(_SRC_OTHER)) != a


def test_digests_are_none_without_a_manifest_or_class_map():
    from revl.mcp.session import _base_manifest_hash_of, _class_map_digest_of
    assert _base_manifest_hash_of(None) is None
    assert _base_manifest_hash_of({}) is None
    assert _class_map_digest_of(None) is None


# --------------------------------------------------------------------------- #
# _cas_surface — refuses on either half moving.
# --------------------------------------------------------------------------- #

def _surface_session(src):
    """A Session with the surface fields set by hand — no runtime, so the CAS
    helper is exercised without a live cordis composition."""
    from revl.mcp.session import Session
    from revl.mcp.approval import ClassMap
    s = Session()
    s.ir = _compile(src)
    s._class_map = ClassMap(s.ir)
    s._generation = 3
    s._surface_epoch = 5
    return s


def test_cas_surface_passes_when_nothing_moved():
    s = _surface_session(_SRC_C)
    # the expected block a decision would record, then an immediate re-check.
    s._cas_surface(s._surface_expected())


def test_cas_surface_refuses_when_the_surface_epoch_moved():
    from revl.mcp.session import SessionError
    s = _surface_session(_SRC_C)
    expected = s._surface_expected()
    s._surface_epoch += 1
    with pytest.raises(SessionError) as caught:
        s._cas_surface(expected)
    assert "surfaceEpoch" in str(caught.value)


def test_cas_surface_refuses_when_the_generation_moved():
    from revl.mcp.session import SessionError
    s = _surface_session(_SRC_C)
    expected = s._surface_expected()
    s._generation += 1
    with pytest.raises(SessionError):
        s._cas_surface(expected)


def test_cas_surface_refuses_when_the_class_map_digest_moved():
    """The across-restart half: the in-process pair is held fixed, but the class
    map was rebuilt over a reclassified provider (manifest held), so the content
    digest moved and the CAS must refuse — a decision never finalizes onto a
    surface it did not see."""
    from revl.mcp.session import SessionError
    from revl.mcp.approval import ClassMap
    s = _surface_session(_SRC_C)
    expected = s._surface_expected()
    # same generation and epoch, same manifest, but the class map moved.
    s._class_map = ClassMap(_compile(_SRC_NOOP))
    assert s._surface_expected()["baseManifestHash"] \
        == expected["baseManifestHash"], "guard: the manifest must be held fixed"
    with pytest.raises(SessionError) as caught:
        s._cas_surface(expected)
    assert "classMapDigest" in str(caught.value)


# --------------------------------------------------------------------------- #
# The epoch moves — the cordis-gated half.
# --------------------------------------------------------------------------- #

def _base_path(tmp_path):
    """A REAL on-disk base source. `_record_generation` builds a generation's
    re-admittable snapshot by materializing the recorded `origin` files off disk
    (`persist._materialize`), so the base cannot be a purely in-memory virtual
    path if the generation is to survive an `undo` (item 597). Idempotent: the
    content is constant, so re-writing across calls is harmless."""
    p = tmp_path / "base.rvl"
    p.write_text(_SRC_C)
    return str(p)


def _base_ir(base_path):
    from revl import compile_files
    from revl._paths import stdlib_root
    admit_path = str(stdlib_root() / "admit.rvl")
    return compile_files([base_path, admit_path], sources={base_path: _SRC_C})


def _base_origin(base_path):
    """The admission inputs that let a loaded/swapped generation record a
    re-admittable snapshot: the co-root files (the on-disk base plus the stdlib
    `admit.rvl`), materialized from disk at snapshot time exactly as a live
    `revl_load` records them. Without this the generation snapshots to None and
    `Session.undo()` correctly refuses it (item 597)."""
    from revl._paths import stdlib_root
    return {"files": [base_path, str(stdlib_root() / "admit.rvl")]}


def _gated_session(tmp_path):
    from revl.mcp.session import Session
    base_path = _base_path(tmp_path)
    session = Session()
    session.approval_policy = "auto"
    session._wal_path = str(tmp_path / "session.wal")
    session.load(copy.deepcopy(_base_ir(base_path)), record=True,
                 origin=_base_origin(base_path))
    return session


@needs_cordis
def test_surface_epoch_moves_on_every_install_and_not_on_call(tmp_path):
    from revl.mcp.approval import ApprovalRequired
    session = _gated_session(tmp_path)
    base_path = _base_path(tmp_path)

    # load installed the class map: epoch 0 -> 1, alongside generation 1.
    assert session._surface_epoch == 1
    assert session._generation == 1
    st = session.state()
    assert st["surfaceEpoch"] == 1
    assert st["baseManifestHash"] is not None
    assert st["classMapDigest"] is not None

    # a call decides against the live surface; it never installs one. The
    # class-(c) crossing prompts, but the epoch does not move either way.
    before = session._surface_epoch
    digests_before = session._surface_digests()
    with pytest.raises(ApprovalRequired):
        session.call("ops", "shout", [str(tmp_path / "c.log"), "x"])
    assert session._surface_epoch == before, "a call moved the surface epoch"
    assert session._surface_digests() == digests_before

    # wiring an admitted turn REBUILDS the class map but does NOT move the
    # generation (426 §5.2) — the epoch is the only counter that catches it.
    assert session.admit(_TURN_FORWARD, granted=["Ops"]).admitted
    assert session._surface_epoch == 2, "wiring a turn did not move the epoch"
    assert session._generation == 1, "wiring a turn must not move the generation"
    # the turn widened the surface, so both digests moved.
    assert session._surface_digests() != digests_before

    # a swap installs a new generation's class map: epoch and generation both move.
    session.swap(copy.deepcopy(_base_ir(base_path)), origin=_base_origin(base_path))
    assert session._generation == 2
    assert session._surface_epoch == 3

    # undo routes through swap: one more install, one more of each. Generation 1
    # was source-backed (see `_gated_session`), so its snapshot re-admits through
    # the gate rather than being refused for missing sources (item 597).
    session.undo()
    assert session._generation == 3
    assert session._surface_epoch == 4


@needs_cordis
def test_undo_refuses_a_generation_loaded_without_recorded_sources(tmp_path):
    """Item 597 guard, retained coverage. The success path above now source-backs
    its generations so the undo/epoch assertions are exercised; keep the
    COMPLEMENTARY guarantee that `Session.undo()` still REFUSES a generation
    loaded without re-admittable sources (`snapshot=None`) rather than bypassing
    the admission gate. This must not be weakened."""
    from revl.mcp.session import Session, SessionError
    base_path = _base_path(tmp_path)
    session = Session()
    session.approval_policy = "auto"
    session._wal_path = str(tmp_path / "session.wal")
    # loaded WITHOUT `origin`: generation 1 records no re-admittable snapshot.
    session.load(copy.deepcopy(_base_ir(base_path)), record=True)
    session.swap(copy.deepcopy(_base_ir(base_path)))
    with pytest.raises(SessionError) as caught:
        session.undo()
    assert "without recorded sources" in str(caught.value)
