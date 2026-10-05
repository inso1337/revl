"""Locating the on-disk assets the toolchain needs at runtime.

The `backends/` tree (per-tier emitters, runtimes, harnesses and their golden
data) lives beside `src/revl` in a source checkout, but is *shipped inside* the
`revl` package when installed from a wheel (see ``only-include`` /
``sources`` under ``[tool.hatch.build.targets.wheel]`` in ``pyproject.toml``). A single resolver keeps every call site agnostic to which
layout it is running under.
"""

from __future__ import annotations

import sys
from pathlib import Path

# `.../src/revl`
_PKG_DIR = Path(__file__).resolve().parent


def backends_root() -> Path:
    """Return the `backends/` directory of emitters and runtimes.

    Two layouts are supported, tried in order so a dev checkout is unchanged:

    1. **Source checkout** — `<repo>/backends`, the sibling of `src/revl`
       (historically resolved as ``Path(__file__).parents[2] / "backends"``).
    2. **Installed wheel** — `backends/` packaged under the module itself as
       ``site-packages/revl/backends``.

    The checkout location wins when present; otherwise the packaged copy is
    returned (even if absent, so callers surface the same "not found" errors
    they always did).
    """
    checkout = _PKG_DIR.parents[1] / "backends"  # <repo>/backends
    if checkout.is_dir():
        return checkout
    return _PKG_DIR / "backends"


def stdlib_root() -> Path:
    """Return the `stdlib/` directory of `.rvl` modules (roadmap 319).

    `use` resolves primarily relative to the importing file, so a module
    outside the revl checkout (the harness, say) cannot `use "stdlib/fs.rvl"`
    without this: the compiler's import resolver falls back to searching
    this directory's parent when the relative path does not resolve, so the
    literal `stdlib/...` path a module writes still lands correctly.

    Same two layouts as `backends_root()`, checkout wins:

    1. **Source checkout** — `<repo>/stdlib`, the sibling of `src/revl`.
    2. **Installed wheel** — `stdlib/` packaged under the module itself as
       ``site-packages/revl/stdlib`` (see the wheel target's ``sources``
       table in ``pyproject.toml``, which ships it the same way it already
       ships ``backends/``).
    """
    checkout = _PKG_DIR.parents[1] / "stdlib"  # <repo>/stdlib
    if checkout.is_dir():
        return checkout
    return _PKG_DIR / "stdlib"


def python_backend_first() -> Path:
    """Put the cordis-py backend directory FIRST on `sys.path`, and return it.

    Every backend directory ships a module named `emit`, and the reference
    tier is reached with a bare `import emit`. "Insert the python directory
    if it is absent" let whichever backend directory sat earlier on `sys.path`
    answer that import: pytest's default import mode prepends the directory of
    every test module it collects, so one session that collected
    `backends/java/` ran the JAVA emitter wherever revl meant the python one,
    and reported its refusals as "the py emitter refused" (issue #1449, 13
    tests in `tests/test_crash_recovery.py` and
    `tests/test_phase1_bracket_fault.py`).
    """
    backend = backends_root() / "python"
    entry = str(backend)
    if not sys.path or sys.path[0] != entry:
        while entry in sys.path:
            sys.path.remove(entry)
        sys.path.insert(0, entry)
    return backend


def python_backend_emitter():
    """The cordis-py backend's `emit` module, and never another backend's.

    `import emit` after `python_backend_first()`. If `emit` is already
    imported from some other file, that module is what a bare import returns,
    so this refuses loudly instead of handing it on.
    """
    backend = python_backend_first()
    import emit  # noqa: PLC0415 - backend import after path setup

    want = (backend / "emit.py").resolve()
    got = Path(getattr(emit, "__file__", "") or "").resolve()
    if got != want:
        raise ImportError(
            f"the module `emit` is {got}, not the python backend's emitter "
            f"{want}; another backend's emitter was imported under the bare "
            "name `emit` earlier in this process")
    return emit


def venv_python(venv: Path) -> Path:
    """The interpreter of the virtualenv at `venv`.

    A POSIX venv keeps it at `bin/python`; a Windows one at
    `Scripts/python.exe` (issue #1939). The Windows spelling is taken when it
    exists, so a POSIX venv is unchanged; otherwise `bin/python` is returned
    even if absent, so callers keep their own "not set up" errors.
    """
    windows = Path(venv) / "Scripts" / "python.exe"
    if windows.exists():
        return windows
    return Path(venv) / "bin" / "python"


def relpath_or_abs(path, start=None) -> str:
    """`os.path.relpath(path, start)`, or the absolute path when there is no
    relative one: on Windows a path on another drive than `start` (or the
    working directory) has none, and `relpath` raises ValueError (issue
    #1944)."""
    import os  # noqa: PLC0415

    try:
        return os.path.relpath(path, start) if start is not None else os.path.relpath(path)
    except ValueError:
        return os.path.abspath(path)
