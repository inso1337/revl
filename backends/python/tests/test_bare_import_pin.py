"""Regression guard for roadmap item 419a.

Every backend ships its own same-named ``emit.py`` (and this backend alone
ships a ``runtime.py``, which nothing else collides on). A suite elsewhere
that inserts its own backend dir onto ``sys.path`` — ``backends/wasm/
test_canonical_abi.py`` does this unconditionally at module-import time —
can win the race to bind the CANONICAL bare name ``emit`` in
``sys.modules`` before this directory's own test files run their bare
``import emit``. In a combined process (``pytest backends/wasm/
backends/python/tests/``) that silently handed this suite the WRONG
``emit.py`` (wasm's), so its tests exercised the wrong renderer and
mismatched their expected messages — 91 spurious failures, confirmed
independently by four agents on a clean ``origin/main`` before this fix.

``backends/python/tests/conftest.py``'s ``_load_and_pin`` guards against
this the same way ``backends/wasm/canonical.py`` already guards the wasm
side for items 98/150: load the sibling module by PATH under a private
name, then unconditionally overwrite ``sys.modules['emit']`` /
``sys.modules['runtime']`` with it. This test simulates the poisoning
explicitly (independent of collection order) and asserts a fresh pin still
recovers THIS backend's own copies.
"""

from __future__ import annotations

import sys
import types

from conftest import BACKEND, _load_and_pin


def test_pin_recovers_our_emit_even_when_bare_emit_is_poisoned():
    poison = types.ModuleType("emit")

    class _PoisonEmitError(Exception):
        pass

    poison.EmitError = _PoisonEmitError
    poison.emit = lambda ir: {"not": "the python emitter's str output"}

    saved = {
        name: sys.modules.get(name)
        for name in ("emit", "revl_python_tests_emit")
    }
    try:
        sys.modules.pop("revl_python_tests_emit", None)
        sys.modules["emit"] = poison

        module = _load_and_pin("emit.py", "revl_python_tests_emit")

        # It must have loaded OUR emit.py by path, not kept the poison.
        assert module is not poison
        assert module.__file__ == str(BACKEND / "emit.py")
        # And the canonical bare name must now be re-pinned to it too — the
        # exact thing a test file's own `import emit` depends on.
        assert sys.modules["emit"] is module
        assert sys.modules["emit"] is not poison
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


def test_pin_recovers_our_runtime_even_when_bare_runtime_is_poisoned():
    poison = types.ModuleType("runtime")
    poison.set_trace = lambda *_a, **_k: None

    saved = {
        name: sys.modules.get(name)
        for name in ("runtime", "revl_python_tests_runtime")
    }
    try:
        sys.modules.pop("revl_python_tests_runtime", None)
        sys.modules["runtime"] = poison

        module = _load_and_pin("runtime.py", "revl_python_tests_runtime")

        assert module is not poison
        assert module.__file__ == str(BACKEND / "runtime.py")
        assert sys.modules["runtime"] is module
        assert sys.modules["runtime"] is not poison
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
