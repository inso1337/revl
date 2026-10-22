"""Issue #2639: `read_text()` must not rely on the platform default encoding.

`Path.read_text()` with no encoding uses the platform default: UTF-8 on macOS
and Linux, the ANSI code page (cp1252) on Windows. A UTF-8 file read that way
decodes its bytes to different characters on Windows. The first failure was
`backends/wasm/golden/functions.wat`, which carries U+2014 as the bytes
`e2 80 94` at byte offset 60: `tests/test_goldens.py` read the golden with no
encoding and compared it to emitted text, so on Windows the compare failed and
the test advised `tools/regen_goldens.py`, which would have rewritten the
golden with the mangled bytes. The same golden has a second reader,
`backends/wasm/test_v3_emit.py`, in a separate CI job, so a fix to the first
reader alone would have left the class failing there.

The rule here is the defect, not the spelling. A call is accepted when it names
`encoding` as a keyword, and also when it passes the encoding positionally
(`read_text("utf-8")`, Python 3.11/3.12) because pathlib takes the encoding as
its first positional parameter. A call with a positional argument is therefore
not relying on the default. The one keyword-less call in this tree that is not
pathlib's method is `prober.read_text(path)` in `src/revl/doctor.py`, and it
takes its own positional argument. The finding is a `.read_text` call with no
positional argument and no `encoding` keyword, reported as `file:line`.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {".git", ".claude", "node_modules", ".venv", "venv", "__pycache__",
             ".mypy_cache", ".pytest_cache", ".ruff_cache", "target", "dist",
             "build"}

REASON = (
    "read_text() without an encoding uses the platform default, cp1252 on "
    "Windows, so a UTF-8 file decodes differently there. The wasm golden "
    "carries U+2014 as e2 80 94 at byte offset 60, and it has two readers "
    "(tests/test_goldens.py and backends/wasm/test_v3_emit.py, a separate CI "
    'job). Name the encoding, e.g. read_text(encoding="utf-8").')


def source_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                yield Path(dirpath) / filename


def scan(root: Path):
    """(violations as `file:line: reason`, number of read_text calls seen)."""
    found = []
    calls = 0
    for path in source_files(root):
        data = path.read_bytes()
        # A call site cannot exist without these bytes; skipping the parse for
        # the rest keeps the tree scan cheap.
        if b"read_text" not in data:
            continue
        tree = ast.parse(data, str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "read_text"):
                continue
            calls += 1
            if any(keyword.arg == "encoding" for keyword in node.keywords):
                continue
            if node.args:
                # pathlib's first positional parameter is the encoding, so a
                # positional call is not relying on the default.
                continue
            found.append(f"{path.relative_to(root)}:{node.lineno}: {REASON}")
    return found, calls


def test_the_tree_has_no_read_text_without_an_encoding():
    found, calls = scan(ROOT)
    assert found == [], "\n".join(found)
    # Vacuity: a green verdict over a walk that read nothing means nothing.
    assert calls > 1000, f"only {calls} read_text calls; the walk is broken"


def test_a_new_read_text_without_an_encoding_fails_with_file_and_line(tmp_path):
    (tmp_path / "a.py").write_text(
        "from pathlib import Path\n"
        "\n"
        "\n"
        "def load(path: Path) -> str:\n"
        "    return path.read_text()\n",
        encoding="utf-8")
    found, calls = scan(tmp_path)
    assert calls == 1
    assert len(found) == 1, found
    assert found[0].startswith("a.py:5: "), found[0]
    assert "cp1252" in found[0] and "offset 60" in found[0]


def test_an_explicit_encoding_is_accepted(tmp_path):
    (tmp_path / "a.py").write_text(
        'def load(path, prober):\n'
        '    a = path.read_text(encoding="utf-8")\n'
        '    b = path.read_text("utf-8")\n'
        '    c = prober.read_text(path)\n'
        '    return a, b, c\n',
        encoding="utf-8")
    found, calls = scan(tmp_path)
    assert calls == 3
    assert found == []
