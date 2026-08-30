"""Unit tests for tools/affected_tests.py — the pre-merge affected-test selector.

These assert the load-bearing SOUNDNESS behaviour so the selector cannot rot
into fail-open: a core-file change must pick the FULL gate, an unmapped file
must pick FULL, a single backend emitter must pick only that tier, and a single
stdlib module must pick only its tests.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "revl_affected_tests", ROOT / "tools" / "affected_tests.py"
)
at = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(at)


def sel(*changed):
    return at.select(list(changed), ROOT)


def test_scaffold_placeholder():
    # Replaced by real assertions in the next commit; fails until then so the
    # selector cannot land unproven.
    with pytest.raises(NotImplementedError):
        sel("backends/wasm/emit.py")
