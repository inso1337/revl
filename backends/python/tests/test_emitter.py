"""Emitter acceptance: the reference IR, the golden file, and rejections."""

from __future__ import annotations

import copy
import json
import re

import pytest

import emit
from conftest import BACKEND, load_module, reference_ir_path

GOLDEN = BACKEND / "golden" / "user_cache.py"
# A red golden is a REVIEW prompt, not a wall: the goldens are snapshot tests
# (docs/conformance.md, "Golden policy: snapshot, not freeze"), so regenerating
# and reviewing the diff is always an acceptable resolution. Every golden
# assertion says which command regenerates it.
_TAIL = ("If the change is intended: python3 tools/regen_goldens.py {t}, then review "
         "the diff. Goldens are snapshots, not a freeze (docs/conformance.md).")


def test_accepts_reference_ir_verbatim(reference_ir):
    """docs/backend-ir.md §Acceptance: examples/user_cache.ir.json MUST be
    accepted verbatim."""
    source = emit.emit(reference_ir)
    module = load_module(source)
    assert set(module.COMPONENTS) == {"PgDatabase", "UserCache"}
    assert module.PgDatabase["inject"] == []
    assert module.UserCache["inject"] == ["db"]
    assert module.SERVICES["Database"]["execute"]["emission"] is True


def test_golden_file_regenerates_identically(reference_ir):
    """The checked-in golden file is exactly what the emitter produces."""
    assert GOLDEN.read_text(encoding="utf-8") == emit.emit(reference_ir), (
        "backends/python/golden/user_cache.py drifted from the emitter. "
        + _TAIL.format(t="python"))


def test_emit_is_deterministic(reference_ir):
    assert emit.emit(reference_ir) == emit.emit(json.loads(reference_ir_path().read_text()))


def test_rejects_wrong_ir_version(reference_ir):
    bad = copy.deepcopy(reference_ir)
    bad["ir_version"] = 0
    with pytest.raises(emit.EmitError, match="ir_version"):
        emit.emit(bad)


def test_rejects_undeclared_req(reference_ir):
    bad = copy.deepcopy(reference_ir)
    del bad["components"][1]["requires"]["db"]
    with pytest.raises(emit.EmitError, match="not declared in requires"):
        emit.emit(bad)


def test_rejects_identifier_colliding_with_host_root(reference_ir):
    # `ctx`/`config`/`frame` are no longer reserved (item 156); the aliasable
    # runtime imports (`fmt`/`Frame`/`ConfigSchema`/…) no longer are either
    # (item 160). What remains guarded is the host-root triple Pool/Map/Job —
    # language builtins the emitter cannot alias apart from a same-named user
    # var. All three now reject uniformly (`Job` used to be unguarded).
    for name in ("Pool", "Map", "Job"):
        bad = copy.deepcopy(reference_ir)
        bad["components"][1]["body"][0]["bind"] = name
        with pytest.raises(emit.EmitError, match="scaffolding"):
            emit.emit(bad)


def test_accepts_ctx_as_identifier(reference_ir):
    """item 156: a body local named `ctx` no longer collides with the emitter
    scaffolding — it emits verbatim while the context handle is `_revl_ctx`."""
    ok = copy.deepcopy(reference_ir)
    ok["components"][1]["body"][0]["bind"] = "ctx"
    # the `let-effect` undo references the same binding by name
    ok["components"][1]["body"][0]["undo"]["target"]["id"] = "ctx"
    source = emit.emit(ok)  # must not raise
    assert "_revl_ctx" in source and "_revl_frame" in source


# item 179: the `let {…} = …` destructure temp used to be named from
# `id(node)` — a Python object identity, which is stable within one process but
# differs across a re-parse of the same IR. That made destructuring
# un-byte-reproducible (and forced `let_pattern` out of the self-host byte
# oracle, item 174). The temp is now named from a per-body monotonic counter,
# a deterministic property of emission order.
def _destructure_ir():
    return {
        "ir_version": emit.IR_VERSION,
        "functions": [
            {
                "name": "f",
                "params": [{"name": "r"}],
                "body": [
                    {"step": "let_pattern", "pattern": "record",
                     "names": ["a", "b"],
                     "value": {"kind": "var", "name": "r"}},
                    {"step": "let_pattern", "pattern": "list",
                     "names": ["c", "d"], "rest": None,
                     "value": {"kind": "var", "name": "r"}},
                    {"step": "return", "expr": {"kind": "var", "name": "a"}},
                ],
            }
        ],
    }


def test_destructure_temp_naming_is_deterministic():
    """The same IR emitted twice — and re-parsed via a JSON round-trip that
    hands every node a fresh `id()` — must produce identical bytes."""
    ir = _destructure_ir()
    first = emit.emit(ir)
    # a JSON round-trip rebuilds every node object with a different identity,
    # exactly reproducing a re-parse; `id(node)` naming diverged here.
    reparsed = emit.emit(json.loads(json.dumps(ir)))
    assert first == emit.emit(ir)  # same objects, second pass
    assert first == reparsed       # fresh objects, same bytes


def test_destructure_temps_are_sequential_and_collision_free():
    """Sibling destructures in one body get distinct, sequential temps under
    the reserved `__revl_` prefix (no user identifier can collide)."""
    source = emit.emit(_destructure_ir())
    assert "__revl_destructure_1 = r" in source
    assert "__revl_destructure_2 = r" in source
    # no `id(node)`-style large-integer temp leaked through
    assert "__revl_destructure_1" in source and "__revl_destructure_2" in source
    # exactly two temps exist and they are 1 and 2 — asserted on the NAMES, not
    # on a mention count: a record field read mentions its temp twice now that
    # the read is `_field_read`'s dict/attr dispatch rather than a bare `.a`
    assert sorted(set(re.findall(r"__revl_destructure_\d+", source))) == [
        "__revl_destructure_1", "__revl_destructure_2"]
    # the record arm reads through the dict the record VALUE is (docs/records.md
    # §7); `.a` on a record value raised AttributeError
    assert "a = (__revl_destructure_1['a'] if isinstance(__revl_destructure_1, dict)" in source
