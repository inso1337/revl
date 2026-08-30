"""The stdlib version stamp — drift detection for vendored stdlib copies
(roadmap item 389).

A consumer that vendors the stdlib holds byte-copies of ``stdlib/*.rvl``. Those
copies drift silently: when item 104 added ``value_is_object`` upstream, the
compiler started recommending an API a stale copy did not carry, and nothing
noticed. The stamp is the fix. ``stdlib/version.rvl`` ships a single pure-revl
``pub fn stdlib_version() -> Str`` whose literal is the stdlib's version; the
value travels with the file, so a vendored copy carries the version it was
copied at.

Two sides compare:

* **what a copy IS** — the ``stdlib_version()`` literal in *that copy* of
  ``stdlib/version.rvl``. :func:`read_stamp` reads it from a tree without
  running the program.
* **what the compiler EXPECTS** — :data:`EXPECTED_STDLIB_VERSION`, read once
  from the stdlib this compiler ships (``stdlib_root()``). It is derived, not
  hand-maintained, so the compiler and its own bundled stdlib can never silently
  disagree; the single edit that bumps the version is the literal in
  ``stdlib/version.rvl``.

:func:`check_drift` compares a loaded stdlib tree's stamp against the expected
version and returns a human-readable warning when they differ (or when the tree
predates the stamp entirely — the item-104 case, where the vendored copy has no
``version.rvl`` at all). ``revl doctor`` surfaces this against the stdlib the
import resolver would actually load.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ._paths import stdlib_root

# The literal a `stdlib_version()` body returns: `return "<v>"` in a `"..."` or a
# backtick `\`...\`` string. Kept deliberately loose on whitespace so a
# reformatted-but-equivalent copy still reads.
_STAMP_RE = re.compile(
    r"""pub\s+fn\s+stdlib_version\s*\(\s*\)\s*->\s*Str\s*\{"""
    r"""[^}]*?return\s*(?P<q>["`])(?P<version>.*?)(?P=q)""",
    re.DOTALL,
)

#: the basename of the stamp module inside a stdlib tree.
VERSION_MODULE = "version.rvl"


def parse_stamp(source: str) -> str | None:
    """Extract the version literal from ``stdlib/version.rvl`` source text.

    Returns the version string, or ``None`` when the text carries no
    ``stdlib_version()`` stamp (a stdlib copy that predates item 389).
    """
    match = _STAMP_RE.search(source)
    return match.group("version") if match else None


def read_stamp(stdlib_dir: str | os.PathLike[str]) -> str | None:
    """Read the version stamp out of a stdlib TREE (the directory holding the
    ``.rvl`` modules).

    Returns ``None`` when the tree has no ``version.rvl`` (a copy predating the
    stamp) or the file carries no readable stamp. Never raises on a missing or
    unreadable file — a drift probe over an untrusted vendored tree must not
    fault.
    """
    path = Path(stdlib_dir) / VERSION_MODULE
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_stamp(source)


def _read_expected() -> str:
    """The version this compiler expects: the stamp of the stdlib it ships.

    Derived from ``stdlib_root()`` so the compiler and its bundled stdlib share
    one source of truth. Falls back to ``"unknown"`` only if the bundled stamp
    somehow cannot be read — a broken install, never the normal path.
    """
    return read_stamp(stdlib_root()) or "unknown"


#: The stdlib version this compiler ships and expects a consumer's copy to match.
EXPECTED_STDLIB_VERSION: str = _read_expected()


def check_drift(
    loaded_stdlib_dir: str | os.PathLike[str],
    expected: str = EXPECTED_STDLIB_VERSION,
) -> str | None:
    """Compare a loaded stdlib tree's stamp against the expected version.

    Returns ``None`` when they match (no drift), else a one-line explanation of
    the drift suitable for a warning. The three drifting shapes:

    * the tree has **no stamp** — it predates item 389 (the item-104 case): its
      symbol set is unknown and may lack what the compiler recommends;
    * an **older** stamp — the copy is behind the compiler;
    * a **newer** stamp — the copy is ahead of the compiler (a stdlib from a
      newer release paired with an older compiler).
    """
    found = read_stamp(loaded_stdlib_dir)
    if found is None:
        return (
            f"loaded stdlib carries no version stamp but this compiler expects "
            f"{expected!r}: the vendored copy predates the stamp (item 389) and "
            f"may be missing public symbols the compiler recommends"
        )
    if found == expected:
        return None
    relation = _relation(found, expected)
    return (
        f"stdlib drift: loaded stdlib is version {found!r} but this compiler "
        f"expects {expected!r} ({relation}); re-vendor the stdlib to match"
    )


def _relation(found: str, expected: str) -> str:
    """Best-effort 'older'/'newer' when both stamps are integer counters; a
    plain 'differs' when either is not (the stamp is documented as a monotonic
    integer, but never assume a vendored copy honoured that)."""
    try:
        found_n, expected_n = int(found), int(expected)
    except ValueError:
        return "differs"
    if found_n < expected_n:
        return "the copy is older than the compiler expects"
    return "the copy is newer than the compiler expects"


def resolve_loaded_stdlib_dir() -> Path:
    """The stdlib directory the import resolver would load ``stdlib/*.rvl`` from.

    Mirrors ``compiler._default_search_path``: an entry on ``REVL_IMPORT_PATH``
    that carries a ``stdlib/version.rvl`` wins (this is how a consumer points the
    compiler at a vendored stdlib), else the compiler's own bundled stdlib. This
    is what lets ``revl doctor`` detect drift against the copy a real compile
    would actually resolve, not just the bundled one.
    """
    for entry in os.environ.get("REVL_IMPORT_PATH", "").split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        candidate = Path(entry) / "stdlib"
        if (candidate / VERSION_MODULE).exists():
            return candidate
    return stdlib_root()
