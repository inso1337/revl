"""One decoding rule for every child process whose output revl reads (#3108).

A `subprocess.run` in text mode with no `encoding=` decodes a child's output
with the host's locale encoding and `errors="strict"`. On a Windows host whose preferred
encoding is a single-byte codepage, one byte a child prints that the codepage
cannot map (0x9d, for instance) raised `UnicodeDecodeError` out of the capture
instead of producing a result, exactly as it did inside `revl test` before
#2977. Nothing about what revl hands a child changes that: revl writes every
child's input as UTF-8, so every child's output is read back as UTF-8 too, and
a byte that is not valid UTF-8 is shown as U+FFFD rather than raised.

Every capture outside the test driver goes through `run`/`popen` here, so the
rule is stated once instead of at each call site. `src/revl/test.py` keeps its
own `_capture`, which adds the count and the note for an undecodable byte on
top of the same decoding (#2977).
"""

from __future__ import annotations

import subprocess

#: What a child's output is decoded as; revl writes every child's input as UTF-8.
ENCODING = "utf-8"

#: What an undecodable byte becomes. "replace" yields U+FFFD directly, which is
#: printable on a console that has no encoding for it either.
ERRORS = "replace"


def run(argv, **kwargs) -> subprocess.CompletedProcess:
    """`subprocess.run` with the child's output decoded as UTF-8, never strictly."""
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", ENCODING)
    kwargs.setdefault("errors", ERRORS)
    return subprocess.run(argv, **kwargs)


def popen(argv, **kwargs) -> subprocess.Popen:
    """`subprocess.Popen` with its pipes decoded as UTF-8, never strictly."""
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", ENCODING)
    kwargs.setdefault("errors", ERRORS)
    return subprocess.Popen(argv, **kwargs)
