"""Shared fixtures for the revl cordis-py backend tests."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import re
import sys
import types

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _load_and_pin(filename: str, cache_name: str) -> types.ModuleType:
    """Load a sibling backend module (``emit.py`` / ``runtime.py``) by PATH
    under a private, per-path-cached name, then PIN the canonical bare name
    (``emit`` / ``runtime``) to it.

    Every backend ships its own same-named ``emit.py`` (wasm's, go's, ...).
    A bare ``import emit`` — which this suite's own test files do, and which
    the ``exec``'d source in ``load_module`` relies on via ``from runtime
    import ...`` — binds whichever module WON the race to put its directory
    first on ``sys.path``. ``backends/wasm/test_canonical_abi.py`` does an
    unconditional ``sys.path.insert(0, backends/wasm)`` at module-import
    time; in a combined process (``pytest backends/wasm/
    backends/python/tests/``, roadmap item 419a) that races ahead of this
    conftest's own insert above and wins, so an unprotected bare ``import
    emit`` in a test file here silently returns wasm's ``emit.py`` instead
    of ours — same class of bug as items 98/150, which
    ``backends/wasm/canonical.py`` already guards against on the wasm side
    (see its ``_load_wasm_emit``).

    Loading under a private name and then EXPLICITLY overwriting
    ``sys.modules[<bare name>]`` (not just setting it if absent) makes this
    order-independent: whichever backend's suite raced onto ``sys.path``
    first, this conftest — which pytest loads before any test file in this
    directory is collected — always re-binds the bare name to OUR copy
    before anything here reads it."""
    cached = sys.modules.get(cache_name)
    if cached is None:
        spec = importlib.util.spec_from_file_location(cache_name, BACKEND / filename)
        assert spec is not None and spec.loader is not None, BACKEND / filename
        cached = importlib.util.module_from_spec(spec)
        sys.modules[cache_name] = cached
        spec.loader.exec_module(cached)
    bare_name = filename.removesuffix(".py")
    sys.modules[bare_name] = cached
    return cached


runtime_mod = _load_and_pin("runtime.py", "revl_python_tests_runtime")
_load_and_pin("emit.py", "revl_python_tests_emit")

# the reference IR: prefer the revl worktree copy, fall back to the vendored
# byte-identical copy checked in next to the tests
_IR_CANDIDATES = (
    BACKEND.parent.parent / "examples" / "user_cache.ir.json",
    BACKEND / "tests" / "user_cache.ir.json",
)


def reference_ir_path() -> pathlib.Path:
    for candidate in _IR_CANDIDATES:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("reference IR not found")


@pytest.fixture
def reference_ir() -> dict:
    return json.loads(reference_ir_path().read_text(encoding="utf-8"))


def load_module(source: str, name: str = "emitted_module") -> types.ModuleType:
    """Exec emitted source as a module (backend dir is on sys.path, so the
    emitted ``from runtime import …`` resolves)."""
    module = types.ModuleType(name)
    exec(compile(source, f"{name}.py", "exec"), module.__dict__)
    return module


@pytest.fixture
def trace():
    """Chronological host-builtin operation log (see runtime.set_trace)."""
    events: list[str] = []
    runtime_mod.set_trace(events.append)
    yield events
    runtime_mod.set_trace(None)


def ops(events: list[str]) -> list[str]:
    """Trace events with instance serials stripped: 'map#3.drop' -> 'map.drop'."""
    return [re.sub(r"#\d+", "", event) for event in events]


async def flush() -> None:
    """Settle all pending event-loop work (cordis idiom, see its conftest)."""
    for _ in range(20):
        await asyncio.sleep(0)


def hook_snapshot(root) -> dict:
    """Non-empty event-hook counts (cordis's get_hook_snapshot idiom)."""
    return {name: len(callbacks) for name, callbacks in root.events._hooks.items() if callbacks}


class Errors:
    """Captures root.logger.error — teardown failures are logged, not raised."""

    def __init__(self, root) -> None:
        self.calls: list = []
        root.logger.error = lambda *args, **kwargs: self.calls.append(args)
