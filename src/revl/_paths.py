"""Locating the on-disk assets the toolchain needs at runtime.

The `backends/` tree (per-tier emitters, runtimes, harnesses and their golden
data) lives beside `src/revl` in a source checkout, but is *shipped inside* the
`revl` package when installed from a wheel (see the ``force-include`` in
``pyproject.toml``). A single resolver keeps every call site agnostic to which
layout it is running under.
"""

from __future__ import annotations

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
