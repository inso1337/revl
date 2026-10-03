"""fillSpec `fillable`: a hole this author can never fill is flagged, not
handed out as work.

An untrusted author (the MCP server's default) may neither declare nor
reach an extern (G8). A hole whose type only host code can build, a
nominal handle no declaration builds, with nothing in reach that returns
one, is therefore unfillable by that author. The default scaffold makes one
(its `effect hole[<Service>Resource]` acquisition), and before this the spec
offered it like any other hole.

What the types decide is decided; what they do not is reported, not guessed:
a `Str` that "must be a hash" has a literal producer, so it stays fillable.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp import fillspec  # noqa: E402
from revl.scaffold import build_spec, scaffold_document  # noqa: E402

HANDLE = """service Box { fn open() -> Str }
component C provides box: Box {
  let h = effect hole[LogHandle] "open the log" undo h.close()
  provide box { fn open() = hole[Str] "say hello" }
}
"""


def _fills(source: str, untrusted: bool) -> dict:
    return {ob["message"]: ob["fillSpec"]["fillable"]
            for ob in fillspec.enrich(compile_source(source),
                                      untrusted=untrusted)}


def test_a_handle_hole_is_unfillable_for_an_untrusted_author():
    fill = _fills(HANDLE, untrusted=True)["open the log"]
    assert fill["byThisAuthor"] is False
    assert fill["decided"] is True and fill["needsHostCode"] is True
    assert fill["producers"] == []
    assert "untrusted-author profile, G8" in fill["reason"]


def test_the_same_hole_needs_host_code_from_a_trusted_author():
    fill = _fills(HANDLE, untrusted=False)["open the log"]
    assert fill["byThisAuthor"] is True
    assert fill["needsHostCode"] is True
    assert "top level of the file" in fill["reason"]


def test_a_handle_some_extern_returns_is_fillable_where_it_may_be_called():
    source = ("extern pure fn log_close(fd: LogHandle) = @py { return }\n"
              "extern acquire fn log_open(path: Str) -> LogHandle "
              "undo log_close(result) = @py { return 1 }\n" + HANDLE)
    fill = _fills(source, untrusted=False)["open the log"]
    assert fill["byThisAuthor"] is True and fill["needsHostCode"] is False
    assert {"kind": "extern", "write": "effect log_open(<path: Str>)"} \
        in fill["producers"]


def test_a_literal_type_lists_its_producers_and_stays_fillable():
    fill = _fills(HANDLE, untrusted=True)["say hello"]
    assert fill["byThisAuthor"] is True and fill["decided"] is True
    assert fill["producers"][0] == {"kind": "literal", "write": '"..."'}


def test_a_binding_and_a_service_are_producers():
    source = """service Db { fn q(sql: Str) -> Str }
service Box { fn get(k: Str) -> Str }
component C requires db: Db provides box: Box {
  provide box { fn get(k) = hole[Str] "look it up" }
}
"""
    producers = _fills(source, untrusted=True)["look it up"]["producers"]
    assert {"kind": "binding", "write": "k"} in producers
    assert {"kind": "service", "write": "db.q(sql: Str) -> Str"} in producers


def test_an_undecidable_type_is_reported_not_guessed():
    """A carrier the language builds by other means (a `Stream[T]`, say) is
    neither a literal type nor a bare handle: with no producer in reach the
    spec says it cannot decide, and does not refuse the hole."""
    fill = fillspec._fillable("Stream[Int]", [], [], [], {"declared": []},
                              {}, {}, untrusted=True)
    assert fill["byThisAuthor"] is True
    assert fill["decided"] is False


def test_the_default_scaffold_flags_its_resource_hole_for_an_untrusted_author():
    spec = build_spec(service="Audit", methods=["digest(text: Str) -> Str"])
    closed = scaffold_document(spec, untrusted=True)
    assert [u["expected"] for u in closed["unfillable"]] == ["AuditResource"]
    opened = scaffold_document(spec, untrusted=False)
    assert "unfillable" not in opened


def test_revl_check_flags_the_unfillable_holes_of_an_inline_candidate():
    from revl.mcp import server
    previous = server.AUTHORING
    try:
        server.set_authoring_trust(host_code=False)
        checked = server._tool_check({"source": HANDLE})
    finally:
        server.AUTHORING = previous
    assert [u["expected"] for u in checked["unfillable"]] == ["LogHandle"]
