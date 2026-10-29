"""A child's output cannot crash `revl test`'s capture or its report (#2977).

`src/revl/test.py` writes every input it hands a child as UTF-8 and used to
read every child back with `subprocess.run(..., text=True)` and no `encoding=`:
the host's locale encoding, decoded strictly. A byte that decoding cannot map
then raised out of the capture, `stdout`/`stderr` were left as `None`, and the
`result.stdout + result.stderr` that follows raised `TypeError`, upstream of
every assertion, so the child's real output reached nobody.

Both capture paths are driven here with a real child that really does write a
byte the decoder cannot map: `0x9d` on its own, which is not valid UTF-8 and is
one of the bytes cp1252 leaves undefined (the codepage the Windows leg decoded
with when it reported two compositions as failures naming no test), and a valid
multi-byte U+2713 beside it, so "the undecodable byte was replaced" cannot be
confused with "the output was mangled". No locale is simulated and no decoding
is stubbed: the capture reads the child under the encoding actually in force. On
the unfixed module `test_ts_group_capture_survives_an_undecodable_byte` fails
for that reason, with `UnicodeDecodeError: 'utf-8' codec can't decode byte
0x9d` raised from the capture itself; the tests of the new `_capture` and
`_decoded` seams fail on it for the plainer reason that the seams do not exist
there, and the module-wide check fails because every capture in the unfixed
module leaves the decoding to the host.

The control matters as much: output that is plain ASCII must come back
unchanged, since a capture that replaced everything would satisfy the first
assertion too.

The last test covers the other half of the same defect, which decoding alone
does not fix. A console on a single-byte codepage cannot *encode* a tick or a
U+FFFD either, so printing the captured text raises `UnicodeEncodeError` and
destroys the result just as well. It carries its own before-and-after control,
since the stream is built in the test.
"""

from __future__ import annotations

import ast
import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import test as T  # noqa: E402

REPLACEMENT = "\ufffd"
CHECK = "\u2713"

# Writes `before`, a valid multi-byte character, the undecodable byte, then
# `after`, so an assertion can tell "the undecodable part is accounted for"
# from "the output was dropped", and from "the valid part was mangled".
_BAD_BYTE_CHILD = (
    r'import sys; '
    r'sys.stdout.buffer.write(b"before\xe2\x9c\x93\x9dafter\n"); '
    r'sys.stderr.buffer.write(b"err\x9dtail\n")'
)
_ASCII_CHILD = (
    r'import sys; '
    r'sys.stdout.buffer.write(b"plain stdout\n"); '
    r'sys.stderr.buffer.write(b"plain stderr\n")'
)


def _child(source: str) -> list:
    return [sys.executable, "-c", source]


def test_run_capture_survives_an_undecodable_byte(tmp_path, capsys):
    """The `subprocess.run` capture every tier shares reports, never raises."""
    result = T._capture(_child(_BAD_BYTE_CHILD), cwd=tmp_path, timeout=120)

    assert result.returncode == 0
    # What the crash destroyed: neither stream may be None after a run that
    # completed, and both keep the output either side of the bad byte.
    assert result.stdout is not None and result.stderr is not None
    assert result.stdout.strip() == f"before{CHECK}{REPLACEMENT}after"
    assert result.stderr.strip() == f"err{REPLACEMENT}tail"
    # And the undecodable bytes are stated and attributed to that child.
    note = capsys.readouterr().out
    assert "not valid UTF-8" in note
    assert "2 bytes" in note
    assert Path(sys.executable).name in note


def test_every_capture_in_the_module_asks_for_utf8_and_never_raises():
    """The invariant is the module, not one call site: no capture is left.

    A future capture written as `subprocess.run(..., text=True)` would put the
    host's locale back in charge of whether a result survives, which is the
    defect in one line. Every capture here must name its decoding.
    """
    source = (ROOT / "src" / "revl" / "test.py").read_text(encoding="utf-8")
    captures = [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr in {"run", "Popen"}
    ]
    assert captures
    for call in captures:
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        assert ast.literal_eval(keywords["encoding"]) == "utf-8"
        assert ast.literal_eval(keywords["errors"]) == "surrogateescape"


def test_run_capture_leaves_ascii_output_alone(tmp_path, capsys):
    """The control: a capture that replaced everything is not a fix."""
    result = T._capture(_child(_ASCII_CHILD), cwd=tmp_path, timeout=120)

    assert result.returncode == 0
    assert result.stdout == "plain stdout\n"
    assert result.stderr == "plain stderr\n"
    assert REPLACEMENT not in result.stdout + result.stderr
    assert capsys.readouterr().out == ""


def test_ts_group_capture_survives_an_undecodable_byte(tmp_path):
    """The ts tier's own capture helper, which reads vitest and plain node."""
    result = T._ts_foreground(_child(_BAD_BYTE_CHILD), tmp_path, 120)

    assert result.returncode == 0
    assert result.stdout is not None and result.stderr is not None
    assert result.stdout.strip() == f"before{CHECK}{REPLACEMENT}after"
    assert result.stderr.strip() == f"err{REPLACEMENT}tail"
    # The statement that raised `TypeError` once the decode had failed.
    assert (result.stdout + result.stderr).strip()


def test_ts_group_capture_leaves_ascii_output_alone(tmp_path):
    result = T._ts_foreground(_child(_ASCII_CHILD), tmp_path, 120)

    assert result.stdout == "plain stdout\n"
    assert result.stderr == "plain stderr\n"


def test_decoded_counts_only_undecodable_bytes():
    """A child printing U+FFFD itself is not reported as an undecodable byte.

    The surrogate step is what makes that distinction possible: a lone
    surrogate cannot be the decoding of any valid UTF-8 input.
    """
    assert T._decoded("") == ("", 0)
    assert T._decoded("plain") == ("plain", 0)
    assert T._decoded(f"the child said {REPLACEMENT}") == (
        f"the child said {REPLACEMENT}", 0)
    assert T._decoded("a\udc9db") == (f"a{REPLACEMENT}b", 1)


def test_report_survives_a_console_that_cannot_encode_the_output(monkeypatch):
    """Printing the captured text on a cp1252 console must not raise either."""
    # The output a tier reports after the capture decoded it correctly: a tick
    # the child printed, and the U+FFFD standing in for a byte it could not.
    reported = f"ok \u2713 and {REPLACEMENT}"

    # The failing direction first, on an identical stream: strict is the
    # default, and this is what the report does to itself without the fix.
    with pytest.raises(UnicodeEncodeError):
        io.TextIOWrapper(io.BytesIO(), encoding="cp1252").write(reported)

    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", newline="")
    monkeypatch.setattr(sys, "stdout", console)
    monkeypatch.setattr(sys, "stderr", console)
    T._reporting_cannot_crash()
    print(reported)
    console.flush()

    written = raw.getvalue().decode("cp1252")
    # Nothing was lost and nothing raised: each character the console cannot
    # encode names itself instead of taking the report down.
    assert "ok " in written
    assert "\\u2713" in written
    assert "\\ufffd" in written
