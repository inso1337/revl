r"""A backtick template has the same two escapes as a plain string (#3503).

`` `\\(` `` used to keep two backslashes where `"\\("` makes one, so moving a
string (a regex, say) into a template to interpolate one name silently changed
every backslash in it. Templates now take the same two escapes adapted to the
delimiter: `` \` `` is a literal backtick (and does not close the template) and
`\\` is one literal backslash; any other `\x` stays verbatim. docs/strings.md
states the rule. The selfhost lexer port mirrors it (selfhost/lexer.rvl
`scan_template`/`skip_backtick`), so the gate agrees.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from revl import compile_source  # noqa: E402
from revl.lexer import lex  # noqa: E402
from revl.test import RUNNERS  # noqa: E402


def _template_parts(source: str):
    for tok in lex(source, "t.rvl"):
        if tok.kind == "template":
            return tok.value
    raise AssertionError(f"no template token in {source!r}")


def test_backslash_backslash_is_one_backslash():
    assert _template_parts('let t = `\\\\(x`\n') == [("text", "\\(x")]


def test_escaped_backtick_embeds_and_does_not_close():
    assert _template_parts('let t = `a\\`b`\n') == [("text", "a`b")]


def test_other_backslash_sequences_stay_verbatim():
    assert _template_parts('let t = `a\\nb\\tc`\n') == [("text", "a\\nb\\tc")]


def test_interpolation_unaffected():
    assert _template_parts('let t = `x${1}y\\\\z`\n') == [
        ("text", "x"), ("expr", "1"), ("text", "y\\z")]


def test_plain_string_escapes_unchanged():
    for tok in lex('let t = "\\\\("\n', "t.rvl"):
        if tok.kind == "string":
            assert tok.value == "\\("
            return
    raise AssertionError("no string token")


def test_the_issue_repro_runs_on_py():
    src = ('test "template and plain string differ on a backslash" {\n'
           '  let name = "x"\n'
           '  let plain = "\\\\(".concat(name)\n'
           '  let tmpl = `\\\\(${name}`\n'
           '  assert plain.length() == 3\n'
           '  assert tmpl.length() == 3\n'
           '}\n')
    status, message = RUNNERS["py"](compile_source(src, "k.rvl"))
    assert status == "pass", message
