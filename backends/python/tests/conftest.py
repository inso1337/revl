"""Shared fixtures for the revl cordis-py backend tests."""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
import sys
import types

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import runtime as runtime_mod  # noqa: E402

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
