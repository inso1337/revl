"""fillSpec `externs`: where an extern goes, and who may write one.

An author that knew extern SYNTAX could not tell from the spec where the
declaration belongs (the top level of the file, never inside a component or a
method), which existing externs a fill at this hole may call, or whether it
may declare one at all. The `externs` block says all three, and an untrusted
author (the MCP server's default) is told it may neither declare nor reach
one, instead of being offered a call the compile refuses (G8).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.mcp import fillspec  # noqa: E402

SOURCE = """type H = opaque
extern pure fn sha(text: Str) -> Str = @py { return text }
extern pure fn shut(h: H) = @py { return }
extern acquire fn open_it(n: Int) -> H undo shut(result) = @py { return 1 }
extern emission fn audit(line: Str) -> Int = @py { return 1 }
service Box { fn digest(text: Str) -> Str
              emission fn log(line: Str) -> Int }
component C provides box: Box {
  let h = effect hole[H] "acquire the handle" undo shut(h)
  provide box {
    fn digest(text) = hole[Str] "hash the text"
    fn log(line) = hole[Int] "log the line"
  }
}
"""


def _spec(message: str, untrusted: bool = False) -> dict:
    for ob in fillspec.enrich(compile_source(SOURCE), untrusted=untrusted):
        if ob["message"] == message:
            return ob["fillSpec"]
    raise AssertionError(f"no hole {message!r}")


def _callable(spec: dict) -> dict:
    return {e["name"]: e["callableHere"] for e in spec["externs"]["declared"]}


def test_the_block_says_where_an_extern_goes():
    ext = _spec("hash the text")["externs"]
    assert ext["mayDeclare"] is True
    assert "TOP-LEVEL declaration" in ext["placement"]
    assert "never inside a component body or a provide method" in ext["placement"]
    assert ext["template"].startswith("extern pure fn ")


def test_each_extern_is_listed_with_its_call_site_form():
    declared = {e["name"]: e for e in _spec("hash the text")["externs"]["declared"]}
    assert declared["sha"]["write"] == "sha(<text: Str>)"
    assert declared["sha"]["signature"] == "sha(text: Str) -> Str"
    assert declared["open_it"]["write"] == "effect open_it(<n: Int>)"
    assert declared["audit"]["write"] == "emit audit(<line: Str>)"


def test_what_a_fill_may_call_depends_on_the_position():
    # a pure method: a pure extern yes; an acquire or an emission one no
    assert _callable(_spec("hash the text")) == {
        "sha": True, "shut": True, "open_it": False, "audit": False}
    # an emission method: the emission extern is a permitted crossing
    assert _callable(_spec("log the line")) == {
        "sha": True, "shut": True, "open_it": False, "audit": True}
    # the acquisition slot of an `effect`: the acquire extern belongs here
    assert _callable(_spec("acquire the handle")) == {
        "sha": True, "shut": True, "open_it": True, "audit": False}


def test_an_untrusted_author_is_offered_no_extern():
    for message in ("hash the text", "log the line", "acquire the handle"):
        spec = _spec(message, untrusted=True)
        ext = spec["externs"]
        assert ext["mayDeclare"] is False
        assert "untrusted-author profile" in ext["reason"]
        assert "(G8)" in ext["reason"]
        assert not any(_callable(spec).values())
        # nor offered as a crossing
        assert not [c for c in spec["crossing"]["calls"]
                    if c["carrier"] == "extern"]


@pytest.mark.parametrize("where, compiles", [("top", True), ("body", False),
                                             ("method", False)])
def test_the_placement_the_spec_states_is_the_one_that_compiles(where,
                                                                 compiles):
    """The template, placed where the spec says, compiles; placed inside the
    component or a method, it does not."""
    template = _spec("hash the text")["externs"]["template"]
    decl = (template.replace("<name>", "crc").replace("<param>", "text")
            .replace("<Type>", "Str").replace("...", "return text"))
    base = ("service Box { fn digest(text: Str) -> Str }\n"
            "component C provides box: Box {\n"
            "  provide box { fn digest(text) = crc(text) }\n"
            "}\n")
    if where == "top":
        source = decl + "\n" + base
    elif where == "body":
        source = base.replace("component C provides box: Box {\n",
                              "component C provides box: Box {\n  "
                              + decl + "\n")
    else:
        source = base.replace("provide box { fn digest(text) = crc(text) }",
                              "provide box { fn digest(text) {\n    " + decl
                              + "\n    return crc(text) } }")
    if compiles:
        compile_source(source)
    else:
        with pytest.raises(RevlError):
            compile_source(source)


def test_the_mcp_check_states_the_servers_authoring_trust():
    """`revl_check` reports what THIS server's author may do: closed by
    default, open once the operator declares the agent a trusted author."""
    from revl.mcp import server
    plain = ("service Box { fn digest(text: Str) -> Str }\n"
             "component C provides box: Box {\n"
             '  provide box { fn digest(text) = hole[Str] "hash the text" }\n'
             "}\n")
    previous = server.AUTHORING
    try:
        server.set_authoring_trust(host_code=False)
        closed = server._tool_check({"source": plain})
        server.set_authoring_trust(host_code=True)
        opened = server._tool_check({"source": plain})
    finally:
        server.AUTHORING = previous
    (hole_closed,) = closed["holes"]
    (hole_opened,) = opened["holes"]
    assert hole_closed["fillSpec"]["externs"]["mayDeclare"] is False
    assert "untrusted-author profile" in hole_closed["fillSpec"]["externs"]["reason"]
    assert hole_opened["fillSpec"]["externs"]["mayDeclare"] is True


def test_a_jailed_file_candidate_is_operator_authored(tmp_path):
    """`compile_under_authoring` compiles a jailed `files` candidate with no
    transport-carried text as operator-authored, so its fillSpec says what
    the operator may do, not the agent."""
    from revl.mcp import server
    draft = tmp_path / "draft.rvl"
    draft.write_text("service Box { fn digest(text: Str) -> Str }\n"
                     "component C provides box: Box {\n"
                     '  provide box { fn digest(text) = hole[Str] "hash" }\n'
                     "}\n")
    previous = server.AUTHORING
    try:
        server.set_authoring_trust(host_code=False, roots=(str(tmp_path),))
        checked = server._tool_check({"files": [str(draft)]})
    finally:
        server.AUTHORING = previous
    (hole,) = checked["holes"]
    assert hole["fillSpec"]["externs"]["mayDeclare"] is True
