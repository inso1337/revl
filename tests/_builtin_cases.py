"""The builtin sum cases an emitted module IMPORTS from the shared runtime.

Issue #1932. The python emitter used to write ``class Ok`` / ``class Err`` into
every emitted module, and each module is its own :class:`types.ModuleType` — so
a ``Result`` produced by one module failed to ``match`` in another: the
consumer tested the value against its OWN ``Ok``, no arm matched, and the
lowered match raised ``TypeError: non-exhaustive match``. The classes now have
exactly one definition, in ``backends/python/runtime.py``, and every module
imports them.

That is why a test which stubs the ``runtime`` module — to exec emitted code
without the cordis-py runtime — must serve these two REAL classes. The PEP-562
placeholder ``lambda *a, **k: None`` the stubs use makes the lowered match
raise ``isinstance() arg 2 must be a type, a tuple of types, or a union`` and
makes ``Ok(x)`` silently ``None``.

The module is loaded once per session through ``tests/_load_by_path.py`` under
a name no other file answers to, so it does not depend on whatever
``sys.modules["runtime"]`` happens to hold when a test runs.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

from _load_by_path import load_by_path

ROOT = Path(__file__).resolve().parents[1]

#: The builtin sum cases an emitted module imports from the shared runtime.
CASES = ("Ok", "Err")


def builtin_cases() -> ModuleType:
    """``backends/python/runtime.py`` itself — the one definition of Ok/Err."""
    return load_by_path("revl_python_builtin_cases",
                        ROOT / "backends" / "python" / "runtime.py")


def serve_builtin_cases(stub: ModuleType) -> None:
    """Make a stub ``runtime`` module answer for ``Ok``/``Err`` as the real one
    does. Call it after the stub's ``__getattr__`` fallback is set."""
    real = builtin_cases()
    for name in CASES:
        setattr(stub, name, getattr(real, name))
