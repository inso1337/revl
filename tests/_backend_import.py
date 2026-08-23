"""Shared loader for backend emitter modules in tests.

Every backend directory ships its own ``emit.py``. A suite that does
``sys.path.insert(0, <backend>); import emit`` binds the CANONICAL name
``emit``, so two such suites running in ONE pytest process collide on
whoever got ``sys.modules['emit']`` first — one of them silently compares
its goldens against the wrong renderer (findings-map.md, post-final-run
addendum). Load by path under a unique module name instead; loaded
modules are cached in ``sys.modules`` so a combined run executes each
emitter exactly once.

Deliberate exception: tests/test_replay.py keeps the canonical
``import emit / import runtime`` — emitted modules do ``from runtime
import ...``, so an aliased copy would be a *different* module object and
its trace fixture would observe nothing.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def backend_emitter(backend: str):
    """The ``emit`` module of ``backends/<backend>/``, under a unique,
    per-path-cached module name."""
    name = f"revl_{backend}_emit"
    if name not in sys.modules:
        path = ROOT / "backends" / backend / "emit.py"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec.loader is not None, path
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return sys.modules[name]
