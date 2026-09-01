"""Item 365: a stray backtick inside a backtick template's host body.

A revl backtick template most often carries a host language (JS/HTML/CSS). The
template has no backtick escape, so a host `//` line comment or `/* … */` block
comment that itself contains a backtick — ``// read the `answer` field`` — closes
the template at that first embedded backtick. The tail then reparses as revl and,
before this item, the diagnostic named whatever identifier the tail happened to
hold (`` `answer` is not declared ``), far from the real mistake.

These tests pin the DIAGNOSTIC-QUALITY fix: the error now points back at the
template boundary and names the stray backtick, and — the additivity half —
every correctly-formed template still lexes and parses exactly as before.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.lexer import lex  # noqa: E402
from revl.parser import Parser  # noqa: E402

# The load-bearing workload shape: a component method returns a page whose host
# JS carries a `//` comment that mentions a `backtick`-quoted field.
_STRAY_LINE_COMMENT = """
service Page { emission fn render() -> Str }
component P provides page: Page {
  provide page {
    fn render() {
      let html = `
        <script>
          // read the `answer` field
          console.log(data.answer)
        </script>`
      return html
    }
  }
}
"""

_STRAY_BLOCK_COMMENT = """
service Page { emission fn render() -> Str }
component P provides page: Page {
  provide page {
    fn render() {
      let html = `<script>
        /* use the `answer` key */ let z = 1
      </script>`
      return html
    }
  }
}
"""


def test_stray_backtick_in_line_comment_points_at_template():
    with pytest.raises(RevlError) as excinfo:
        compile_source(_STRAY_LINE_COMMENT)
    msg = str(excinfo.value)
    # points at the stray backtick's line (8), names the template's open (6)
    assert "<string>:8:" in msg
    assert "stray backtick closed the template opened on line 6" in msg
    # actionable: revl has no backtick escape, so name the interpolation escape
    assert 'no backtick escape' in msg
    assert '${"`"}' in msg
    # NOT the old distant identifier-naming failure
    assert "is not declared" not in msg
    assert "expected a statement" not in msg


def test_stray_backtick_in_block_comment_points_at_template():
    with pytest.raises(RevlError) as excinfo:
        compile_source(_STRAY_BLOCK_COMMENT)
    msg = str(excinfo.value)
    assert "stray backtick closed the template opened on line 6" in msg
    assert "is not declared" not in msg


# ------------------------------------------------------------- additivity

_GOOD_TEMPLATES = """
service Page { emission fn render() -> Str }
component P provides page: Page {
  provide page {
    fn render() {
      let a = `see http://example.com/x`
      let b = `<style>/* note */ body{color:red}</style>`
      let c = `line one
        // a trailing host comment line
      `
      let d = `<script>// embed a backtick: ${"`"} ok</script>`
      return a
    }
  }
}
"""


def test_correctly_formed_templates_still_compile():
    # every template here contains `//` or `/* */` yet closes cleanly; none may
    # be mistaken for a stray-backtick close.
    assert compile_source(_GOOD_TEMPLATES) is not None


@pytest.mark.parametrize("src", [
    "`http://x`",
    "`plain`",
    "`a=${x}`",
    "`/* c */ end`",
    "`x // y`",
    '`<script>// embed ${"`"} here</script>`',
])
def test_valid_template_carries_no_stray_marker(src):
    toks = [t for t in lex(src, "<test>") if t.kind != "eof"]
    assert toks[0].kind == "template"
    # the side-band marker the parser reads is absent for well-formed templates,
    # so token stream + lexing are byte-for-byte unchanged.
    assert getattr(toks[0], "stray_backtick", None) is None


def test_stray_marker_present_only_on_early_close():
    # a bare template line where a `//` comment's backtick closes early leaves
    # host text trailing on the same line -> flagged.
    src = "`// x `a` b`"
    toks = [t for t in lex(src, "<test>") if t.kind != "eof"]
    assert toks[0].kind == "template"
    assert getattr(toks[0], "stray_backtick", None) is not None


def test_diagnostic_only_reroutes_a_failing_parse():
    # the good program parses with no exception at all; the reroute is on the
    # error path only, so a valid template is never turned into a rejection.
    Parser(_GOOD_TEMPLATES, "<test>").parse()
