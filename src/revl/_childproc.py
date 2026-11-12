"""One decoding rule for every child process whose output revl reads (#3108).

A `subprocess.run` in text mode with no `encoding=` decodes a child's output
with the host's locale encoding and `errors="strict"`. On a Windows host whose preferred
encoding is a single-byte codepage, one byte a child prints that the codepage
cannot map (0x9d, for instance) raised `UnicodeDecodeError` out of the capture
instead of producing a result, exactly as it did inside `revl test` before
#2977. Nothing about what revl hands a child changes that: revl writes every
child's input as UTF-8, so every child's output is read back as UTF-8 too, and
a byte that is not valid UTF-8 is shown as U+FFFD rather than raised. A Python
child before 3.15 on Windows would still write its pipes in the locale
codepage, so the seam also sets `PYTHONIOENCODING=utf-8:surrogateescape` in the
child's environment unless the caller already set it (a bare `utf-8` would make
the child's stdio strict, where the C locale and UTF-8 mode use
surrogateescape, so a lone surrogate would raise in the child); other programs ignore it.

Every capture outside the test driver goes through `run`/`popen` here, so the
rule is stated once instead of at each call site. `src/revl/test.py` keeps its
own `_capture`, which adds the count and the note for an undecodable byte on
top of the same decoding (#2977).
"""

from __future__ import annotations

import os
import subprocess

#: What a child's output is decoded as; revl writes every child's input as UTF-8.
ENCODING = "utf-8"

#: What an undecodable byte becomes. "replace" yields U+FFFD directly, which is
#: printable on a console that has no encoding for it either.
ERRORS = "replace"

#: The variable that makes a Python child write its pipes as UTF-8, with
#: surrogateescape so a raw byte the child holds reaches the parent as that byte. A Python
#: before 3.15 on Windows writes them in the locale codepage unless told
#: otherwise, so the seam tells it. Every other program ignores it.
PYTHON_STDIO_ENV = "PYTHONIOENCODING"


def _child_env(env):
    """The child's environment with `PYTHONIOENCODING` set when the caller did
    not set it. Never mutates the caller's mapping: `None` (inherit) becomes a
    copy of this process's environment, a mapping becomes a copy of itself."""
    merged = dict(os.environ if env is None else env)
    merged.setdefault(PYTHON_STDIO_ENV, f"{ENCODING}:surrogateescape")
    return merged


def run(argv, **kwargs) -> subprocess.CompletedProcess:
    """`subprocess.run` with the child's output decoded as UTF-8, never strictly."""
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", ENCODING)
    kwargs.setdefault("errors", ERRORS)
    kwargs["env"] = _child_env(kwargs.get("env"))
    return subprocess.run(argv, **kwargs)


def popen(argv, **kwargs) -> subprocess.Popen:
    """`subprocess.Popen` with its pipes decoded as UTF-8, never strictly."""
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", ENCODING)
    kwargs.setdefault("errors", ERRORS)
    kwargs["env"] = _child_env(kwargs.get("env"))
    return subprocess.Popen(argv, **kwargs)
