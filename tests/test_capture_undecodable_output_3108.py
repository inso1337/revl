"""Every child capture outside the test driver decodes as UTF-8 (#3108).

`src/revl/test.py` fixed its own captures for #2977: read UTF-8, and report a
byte that cannot be decoded instead of raising. The same `subprocess.run(...,
text=True)` with no `encoding=` remained throughout the rest of the package,
where it decoded with the host's preferred encoding and `errors="strict"`. On a
Windows host whose preferred encoding is a single-byte codepage, one byte a
child prints that the codepage cannot map (0x9d, for instance) raised
`UnicodeDecodeError` out of the capture, so the caller got an exception instead
of the child's result.

`src/revl/_childproc.py` is now the one place that decoding is decided:
`run`/`popen` name `encoding="utf-8"` and `errors="replace"`, so an undecodable
byte becomes U+FFFD and the result still reaches the caller. The tests here
drive that seam with a real child that really writes 0x9d, run the same capture
in a Python whose preferred encoding is forced to a non-UTF-8 codepage to show
the old shape raising there while the seam does not, and check the package
statically: no text-mode capture outside `_childproc.py` is left to the host,
and every module that used to capture goes through the seam.

The valid multi-byte U+2713 printed beside the bad byte is the control that
"the undecodable byte was replaced" is not confused with "the output was
mangled"; plain ASCII output must come back untouched too.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import _childproc  # noqa: E402

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

# Modules that captured a child's output with no `encoding=` before this fix:
# the sites issue #3108 names, in the shape they are cited there.
CAPTURE_MODULES = (
    "cert.py", "deploy.py", "dev.py", "doctor.py", "mcp/proxy.py",
    "mcp/server.py", "placement.py", "pool_dispatch.py", "run_go.py",
    "run_java.py", "run_rust.py", "run_ts.py", "run_wasm.py",
    "sandbox_runtime.py",
)

# Runs in a Python whose preferred encoding has been forced to a non-UTF-8
# codepage. It captures the same bad-byte child twice: once through the bare
# shape this fix removes (which must raise here, proving the codepage really is
# in charge of that capture) and once through the seam (which must not).
_FORCED_CODEPAGE_PROBE = r'''
import locale, subprocess, sys
print("preferred=" + locale.getpreferredencoding(False))
sys.path.insert(0, sys.argv[1])
from revl import _childproc
child = [sys.executable, "-c", sys.argv[2]]
try:
    subprocess.run(child, capture_output=True, text=True, timeout=120)
except UnicodeDecodeError:
    print("bare=raised")
else:
    print("bare=decoded")
result = _childproc.run(child, capture_output=True, timeout=120)
print("helper=" + ascii((result.returncode, result.stdout, result.stderr)))
'''


def _child(source: str) -> list:
    return [sys.executable, "-c", source]


def _assert_replaced(stdout: str, stderr: str) -> None:
    assert stdout.strip() == f"before{CHECK}{REPLACEMENT}after"
    assert stderr.strip() == f"err{REPLACEMENT}tail"


def test_run_capture_replaces_an_undecodable_byte():
    """`_childproc.run` returns the child's output, never raises on a byte."""
    result = _childproc.run(_child(_BAD_BYTE_CHILD), capture_output=True, timeout=120)

    assert result.returncode == 0
    assert result.stdout is not None and result.stderr is not None
    _assert_replaced(result.stdout, result.stderr)


def test_popen_capture_replaces_an_undecodable_byte():
    """`_childproc.popen`'s pipes are decoded the same way, never strictly."""
    process = _childproc.popen(_child(_BAD_BYTE_CHILD),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = process.communicate(timeout=120)

    assert process.returncode == 0
    _assert_replaced(stdout, stderr)


def test_run_capture_leaves_ascii_output_alone():
    """The control: a capture that replaced everything is not a fix."""
    result = _childproc.run(_child(_ASCII_CHILD), capture_output=True, timeout=120)

    assert result.returncode == 0
    assert result.stdout == "plain stdout\n"
    assert result.stderr == "plain stderr\n"
    assert REPLACEMENT not in result.stdout + result.stderr


def test_capture_ignores_a_non_utf8_preferred_encoding():
    """The host codec decides nothing: the seam decodes UTF-8 regardless.

    The child of this test runs with `LC_ALL=C`, Python's C-locale coercion and
    UTF-8 mode both off, so its *preferred* encoding is a single-byte codepage,
    the situation that broke the callers on Windows. The bare shape raises
    `UnicodeDecodeError` there; the seam returns the child's output with the
    undecodable byte shown as U+FFFD.
    """
    env = {key: value for key, value in os.environ.items()
           if key not in {"PYTHONIOENCODING", "PYTHONUTF8", "PYTHONCOERCECLOCALE",
                          "LC_ALL", "LANG"}}
    env.update({"LC_ALL": "C", "LANG": "C", "PYTHONCOERCECLOCALE": "0",
                "PYTHONUTF8": "0", "PYTHONPATH": str(ROOT / "src")})

    probe = subprocess.run(
        [sys.executable, "-c", _FORCED_CODEPAGE_PROBE,
         str(ROOT / "src"), _BAD_BYTE_CHILD],
        capture_output=True, text=True, timeout=240, env=env)

    assert probe.returncode == 0, probe.stderr
    lines = dict(line.split("=", 1) for line in probe.stdout.splitlines() if "=" in line)
    preferred = lines["preferred"]
    assert "utf8" not in preferred.replace("-", "").replace("_", "").lower(), (
        f"the probe did not get a non-UTF-8 preferred encoding: {preferred}")
    # On the unfixed shape the codepage is in charge and the bad byte raises.
    assert lines["bare"] == "raised"
    # The seam decodes UTF-8, so the child's result survives with U+FFFD.
    assert lines["helper"] == ascii(
        (0, f"before{CHECK}{REPLACEMENT}after\n", f"err{REPLACEMENT}tail\n"))


def _text_mode_calls():
    """Every `subprocess` call in `src/revl` that asks for text mode."""
    for path in sorted((ROOT / "src" / "revl").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"
                    and node.func.attr in {"run", "Popen", "check_output", "call"}):
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords
                        if keyword.arg}
            text = keywords.get("text") or keywords.get("universal_newlines")
            if isinstance(text, ast.Constant) and text.value is True:
                yield path, node, keywords


def test_no_text_capture_leaves_the_decoding_to_the_host():
    """The invariant is the package, not the modules this issue happened to list.

    A capture written as `subprocess.run(..., text=True)` would put the host's
    locale back in charge of whether a result survives, which is the defect in
    one line. Every text-mode subprocess call here must name its encoding and
    its error handling; `rg -n "text=True" src/revl | rg -v encoding` is the
    same statement in the issue's words, and it lists nothing.
    """
    offenders = []
    for path, node, keywords in _text_mode_calls():
        encoding = keywords.get("encoding")
        errors = keywords.get("errors")
        where = f"{path.relative_to(ROOT)}:{node.lineno}"
        if not (isinstance(encoding, ast.Constant) and encoding.value == "utf-8"):
            offenders.append(f"{where}: text mode does not name encoding=\"utf-8\"")
        if not isinstance(errors, ast.Constant):
            offenders.append(f"{where}: text mode does not name errors=")
    assert offenders == []


def test_every_module_that_captured_now_uses_the_shared_seam():
    """A module that captured a child now goes through `_childproc`'s decoding.

    This is what makes the seam's measured behaviour reach each capture path:
    the modules below have no text-mode `subprocess` call of their own left.
    """
    missing = []
    for relative in CAPTURE_MODULES:
        path = ROOT / "src" / "revl" / relative
        tree = ast.parse(path.read_text(encoding="utf-8"))
        uses_seam = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "_childproc"
            and node.func.attr in {"run", "popen"}
            for node in ast.walk(tree))
        if not uses_seam:
            missing.append(relative)
    assert missing == []


def test_the_seam_names_utf8_and_replacement():
    """The one decoding rule, stated where the static check can read it."""
    assert _childproc.ENCODING == "utf-8"
    assert _childproc.ERRORS in {"replace", "surrogateescape"}
