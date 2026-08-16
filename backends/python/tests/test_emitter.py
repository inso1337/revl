"""Emitter acceptance: the reference IR, the golden file, and rejections."""

from __future__ import annotations

import copy
import json
import pathlib

import pytest

import emit
from conftest import BACKEND, load_module, reference_ir_path

GOLDEN = BACKEND / "golden" / "user_cache.py"


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
    assert GOLDEN.read_text(encoding="utf-8") == emit.emit(reference_ir)


def test_emit_is_deterministic(reference_ir):
    assert emit.emit(reference_ir) == emit.emit(json.loads(reference_ir_path().read_text()))


def test_rejects_wrong_ir_version(reference_ir):
    bad = copy.deepcopy(reference_ir)
    bad["ir_version"] = 1
    with pytest.raises(emit.EmitError, match="ir_version"):
        emit.emit(bad)


def test_rejects_undeclared_req(reference_ir):
    bad = copy.deepcopy(reference_ir)
    del bad["components"][1]["requires"]["db"]
    with pytest.raises(emit.EmitError, match="not declared in requires"):
        emit.emit(bad)


def test_rejects_identifier_colliding_with_scaffolding(reference_ir):
    bad = copy.deepcopy(reference_ir)
    bad["components"][1]["body"][0]["bind"] = "ctx"
    with pytest.raises(emit.EmitError, match="scaffolding"):
        emit.emit(bad)
