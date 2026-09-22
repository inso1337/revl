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


# ----------------------------------------------- issue #1310: bound the reword

# What `selfhost/emit_*.rvl` writes thousands of times: a one-line template
# holding a host COMMENT, closed on its own line with revl still trailing it.
# `_closing_backtick_is_stray` flags it: `//` sits before the close and live
# text after it, although the template closes exactly where it means to. The flag is
# harmless on its own; what was not harmless was letting it reword any later
# failure in the file.
_FALSE_POSITIVE_COMMENT_TEMPLATE = """
fn emit_header() -> Str {
  let line = (`// generated by the emitter, do not edit`)
  let tail = `end`
  return line
}

fn much_later() -> Int {
  let acquire = 1
  return acquire
}
"""


def test_reserved_word_after_a_flagged_template_keeps_its_own_diagnostic():
    # issue #1310: `acquire` is a reserved keyword and the parser computes the
    # exact diagnostic for it. Before the tail bound, the flagged template on
    # line 3 replaced it with a template complaint at line 3.
    toks = lex(_FALSE_POSITIVE_COMMENT_TEMPLATE, "<test>")
    flagged = [t for t in toks if getattr(t, "stray_backtick", None)]
    assert flagged, "fixture no longer trips the heuristic; it must, to test the bound"
    assert flagged[0].stray_backtick == (3, 3)

    with pytest.raises(RevlError) as excinfo:
        Parser(_FALSE_POSITIVE_COMMENT_TEMPLATE, "<test>").parse()
    error = excinfo.value
    assert error.line == 9, f"reported line {error.line}, not the `let` on line 9"
    assert "reserved keyword" in (error.hint or "")
    assert "stray backtick" not in error.message


def test_no_reroute_when_no_template_follows_the_suspect():
    # A stray close needs the template's REAL close further on, so a suspect
    # with no later backtick in the file cannot have been stray. Nothing after
    # it may be reworded.
    src = """
fn emit_header() -> Str {
  let line = (`// generated by the emitter`)
  return line
}

fn much_later() -> Int {
  let acquire = 1
  return acquire
}
"""
    assert [t for t in lex(src, "<test>") if getattr(t, "stray_backtick", None)]
    with pytest.raises(RevlError) as excinfo:
        Parser(src, "<test>").parse()
    assert excinfo.value.line == 8
    assert "reserved keyword" in (excinfo.value.hint or "")


def test_reroute_still_fires_inside_the_flagged_template_tail():
    # The bound narrows the reword, it does not remove it: a failure in the
    # region the stray close could actually have corrupted is still reworded.
    src = "`// x `a` b`"
    with pytest.raises(RevlError) as excinfo:
        Parser(src, "<test>").parse()
    assert "stray backtick closed the template" in excinfo.value.message


# The defect is size-dependent. The same statement in a two-function file has
# always reported correctly, so the case that matters is the real emitter.
def test_real_selfhost_emitter_reports_the_reserved_word_at_its_own_line():
    emitter = ROOT / "selfhost" / "emit_go.rvl"
    if not emitter.exists():  # pragma: no cover - source checkout only
        pytest.skip("selfhost/emit_go.rvl not present")
    src = emitter.read_text()
    flagged = [t for t in lex(src, str(emitter))
               if getattr(t, "stray_backtick", None)]
    assert flagged, "emit_go.rvl no longer trips the heuristic; pick another emitter"
    probe = "\nfn _probe_1310() -> Int {\n  let acquire = 1\n  return acquire\n}\n"
    mutated = src.rstrip("\n") + "\n" + probe
    let_line = mutated.split("\n").index("  let acquire = 1") + 1
    with pytest.raises(RevlError) as excinfo:
        Parser(mutated, str(emitter)).parse()
    error = excinfo.value
    assert error.line == let_line
    assert "reserved keyword" in (error.hint or "")
