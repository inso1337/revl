"""Author text that reaches an emitted COMMENT stays inside it.

`_config_interface` renders a `config` field's default into a
`/** default: ... */` doc line. That is the only place this emitter puts
author-controlled text into a lexical context with NO ESCAPE SYNTAX: a
comment cannot be escaped into, only ended. `json.dumps` escapes what a
STRING LITERAL needs (quote, backslash, control characters) and nothing a
comment needs, so a default whose text contains `*/` used to end the doc
line and leave the rest of the default in CODE POSITION in the emitted
module — valid TypeScript that `tsc` accepts and node executes, produced
from pure data in the `.rvl` source.

`_comment_text` is the fix: it breaks both sequences that can end a comment
(a line terminator, and `*/`) before the text reaches one. These tests pin
the property rather than the byte shape — the comment must still be a single
comment, and the default must still be entirely inside it.

Toolchain-free except for `test_escaped_module_typechecks`, which runs the
emitted module through `tools/tscheck.mjs` and skips when the backend's
`node_modules` is not installed.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from itertools import product
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

# A marker no emitter scaffolding would ever produce, so finding it outside
# the comment is proof the text escaped rather than a coincidence.
MARKER = "revl_comment_escape_marker"

# The shape that ended the doc comment: close it, finish the interface, put a
# statement in code position, then reopen a comment so what follows still
# parses. The whole thing is one ordinary `Str` default.
BREAKOUT = f"*/ }} const {MARKER} = 1; interface Unused {{ /*"

# Every line terminator a reader might honour, `str.splitlines`'s set for the
# ones that matter here: CR, LF, NEL, LINE SEPARATOR, PARAGRAPH SEPARATOR.
LINE_TERMINATORS = [chr(0x0d), chr(0x0a), chr(0x85), chr(0x2028), chr(0x2029)]


def _load():
    spec = importlib.util.spec_from_file_location(
        "revl_ts_emit_comment", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _emit_with_default(default: str) -> str:
    src = (
        "service Greeter {\n"
        "  fn greet() -> Str\n"
        "}\n"
        "\n"
        "component Cfg provides greeter: Greeter {\n"
        f"  config {{ motd: Str = {json.dumps(default)} }}\n"
        "  provide greeter {\n"
        "    fn greet() = config.motd\n"
        "  }\n"
        "}\n"
    )
    return _load().emit(compile_source(src))


def _doc_comment(out: str) -> str:
    """The `/** default: ... */` comment's contents, as the TS lexer would
    take them: everything from the opener to the FIRST `*/` after it. If the
    author's text ended the comment early, that is what this returns."""
    start = out.index("/** default:")
    end = out.index("*/", start)
    return out[start:end]


def test_breakout_default_stays_inside_the_comment():
    """The comment the TS lexer sees still contains the whole default."""
    out = _emit_with_default(BREAKOUT)
    comment = _doc_comment(out)
    # The marker sits past the payload's `*/`. Unescaped, the comment ended
    # there and this substring never reached the marker at all.
    assert MARKER in comment
    # A doc comment is one line: nothing in the default may start a new one.
    assert "\n" not in comment


def test_doc_comment_ends_exactly_where_the_emitter_ended_it():
    """The first `*/` after the opener is the emitter's own terminator at the
    end of the doc line — not one the default supplied earlier, which is what
    would put the rest of the line in code position."""
    out = _emit_with_default(BREAKOUT)
    start = out.index("/** default:")
    assert out.index("*/", start) + 2 == out.index("\n", start)


def test_breakout_default_never_reaches_code_position():
    """The only lines carrying the payload text are the doc comment and the
    config spec's string literal — never a statement of its own."""
    out = _emit_with_default(BREAKOUT)
    carriers = [line for line in out.splitlines() if MARKER in line]
    assert carriers
    for line in carriers:
        assert (line.strip().startswith("/** default:")
                or "applyConfigDefaults" in line), line


def test_config_spec_literal_is_unchanged_by_the_comment_escape():
    """The escape narrows the COMMENT only. The default's runtime value — the
    string literal `applyConfigDefaults` is handed — still carries the exact
    author bytes, so no program's behaviour changes."""
    out = _emit_with_default(BREAKOUT)
    spec = next(l for l in out.splitlines() if "applyConfigDefaults" in l)
    assert json.dumps(BREAKOUT) in spec


def test_ordinary_default_is_byte_identical():
    """A default with no comment-ending sequence is untouched, so no existing
    module's bytes move."""
    out = _emit_with_default("hello world")
    assert '  /** default: "hello world" */' in out


@pytest.mark.parametrize("terminator", LINE_TERMINATORS)
def test_comment_text_collapses_every_line_terminator(terminator):
    """A `//` comment ends at a newline, so no line terminator survives."""
    assert _load()._comment_text(f"a{terminator}b") == "a b"


def test_comment_text_breaks_the_block_delimiters():
    m = _load()
    assert m._comment_text("*/") == "*\\/"
    assert m._comment_text("/*") == "/\\*"
    assert m._comment_text("**/") == "**\\/"
    # the middle `/` opens as well as closes, so it is broken on both sides
    assert m._comment_text("*/*/") == "*\\/\\*\\/"
    # a non-delimiter `*` or `/` is left alone: the escape is minimal
    assert m._comment_text("a * b / c") == "a * b / c"


def test_comment_text_cannot_be_defeated_by_any_short_input():
    """Exhaustive over the alphabet that can build a delimiter. The
    substitution only ever INSERTS a backslash, so no input may survive it
    still able to end a comment — including by the inserted character joining
    two others into a delimiter the pass had not seen."""
    escape = _load()._comment_text
    alphabet = "*/\\\n a"
    for length in range(1, 6):
        for parts in product(alphabet, repeat=length):
            escaped = escape("".join(parts))
            assert "*/" not in escaped
            assert not any(c in escaped for c in LINE_TERMINATORS)


def test_escaped_module_typechecks():
    """The escaped module is still valid TypeScript. Emitted cases each need
    their own program and must sit directly under `backends/typescript` so the
    emitted `import ... from '../runtime.ts'` resolves — the same recipe
    `tools/validate.py` uses."""
    if shutil.which("node") is None:
        pytest.skip("node not available")
    if not (BACKEND / "node_modules" / "typescript").exists():
        pytest.skip("backends/typescript/node_modules not installed")
    out = _emit_with_default(BREAKOUT)
    with tempfile.TemporaryDirectory(dir=BACKEND, prefix=".comment-escape-") as tmp:
        directory = Path(tmp)
        (directory / "case_0.ts").write_text(out, encoding="utf-8")
        payload = json.dumps({
            "dir": str(directory),
            "files": ["case_0.ts"],
            "tsconfig": str(BACKEND / "tsconfig.json"),
        })
        result = subprocess.run(
            ["node", str(ROOT / "tools" / "tscheck.mjs")],
            input=payload, capture_output=True, text=True, cwd=BACKEND,
            timeout=300,
        )
    assert result.returncode == 0, result.stderr
    diagnostics = json.loads(result.stdout)
    assert not diagnostics.get("case_0.ts"), diagnostics
