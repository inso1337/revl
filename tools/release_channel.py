#!/usr/bin/env python3
"""Classify a release version as stable or prerelease.

`publish.yml` gates the PyPI upload on this answer. A pre-release version may
be built and tagged, but it is not the version the index should serve: PyPI
has no delete, so the gate has to fail closed before the first upload.

A final release is digits and dots, optionally with a `.postN` segment
(`2.0.0`, `2.0.0.1`, `2.0.0.post1`); local build metadata (`+...`) does not
change the channel. Everything else is `prerelease`, including PEP 440's
`aN`, `bN`, `rcN` and `.devN` segments and any spelling this tool does not
recognize. An unrecognized version therefore skips the upload rather than
being published because it could not be classified.

    python3 tools/release_channel.py 2.0.0      # stable
    python3 tools/release_channel.py 2.0.0rc1   # prerelease
"""

from __future__ import annotations

import re
import sys

_FINAL = re.compile(r"[0-9]+(?:\.[0-9]+)*(?:\.post[0-9]+)?", re.IGNORECASE)


def channel(version: str) -> str:
    """Return ``stable`` or ``prerelease`` for a PEP 440 version string."""
    text = version.strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    if not text:
        raise ValueError("empty version")
    public = text.split("+", 1)[0]
    return "stable" if _FINAL.fullmatch(public) else "prerelease"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: tools/release_channel.py VERSION", file=sys.stderr)
        return 2
    try:
        print(channel(args[0]))
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
