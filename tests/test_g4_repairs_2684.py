"""Issue #2684: a G4 missing-undo refusal names the acquire and ranks repairs.

The reversibility refusal used to carry one fixed sentence for the whole code.
A structured record now names the exact acquisition (file, line, extern and
method), pairs the class the extern declares with the class the checker derived
for the call, and lists the repairs in order of preference. The one mechanical
repair (spell the inverse at the site) is verified by the fixgen engine before
it carries a `fix_code`; the prose repairs stay prose.

The record is the same one `revl_check` answers with: the MCP front door adds
the source text so the engine can verify, and does not reshape the fields.
"""

from __future__ import annotations

import pytest

from revl.compiler import compile_source
from revl.diagnostics import report
from revl.errors import RevlError
from revl.lsp.fixgen import apply_edits
from revl.mcp.server import handle


@pytest.fixture(autouse=True)
def _trusted_author():
    """These candidates declare externs, which the agent profile refuses as
    host code (G8). The refusal under test is G4, so the fixture gives the
    compile the operator's trust and puts the server's back afterwards."""
    from revl.mcp import server as server_mod
    saved = server_mod.AUTHORING
    server_mod.set_authoring_trust(host_code=True)
    try:
        yield
    finally:
        server_mod.AUTHORING = saved

# a host acquisition bound with no `undo`: the canonical missing-undo shape
G4_HOST = (
    "\n"
    "service S { fn go(u: Str) -> Int }\n"
    "\n"
    "component Leaky provides s: S {\n"
    "  config { url: Str }\n"
    '  let pool = effect Pool.open("u", 1)\n'
    "  provide s { fn go(u) = 1 }\n"
    "}\n"
)

# the same acquisition unbound: the prose repair still names the inverse, but
# the engine cannot spell it without a handle to release
G4_UNBOUND = (
    "\n"
    "service S { fn go(u: Str) -> Int }\n"
    "\n"
    "component Leaky provides s: S {\n"
    "  config { url: Str }\n"
    '  effect Pool.open("u", 1)\n'
    "  provide s { fn go(u) = 1 }\n"
    "}\n"
)

# an `acquire` extern that declares no inverse at all: the refusal is at the
# declaration, and the acquire is the extern itself
G4_DECLARATION = (
    "type H = Opaque\n"
    "extern acquire fn open_h() -> H = @py { return 1 }\n"
)

# a declared extern whose inverse exists but whose site does not spell it
# (item 420(d)): the mechanical repair is the declared inverse on the handle
G4_DECLARED_INVERSE = (
    "type H = Opaque\n"
    "extern pure fn close_h(h: H) -> Unit = @py { return }\n"
    "extern acquire fn open_h() -> H undo close_h(result) = @py { return 1 }\n"
    "component C {\n"
    "  let h = effect open_h()\n"
    "}\n"
)

# a `local` extern needs no bracket at all: its repair drops it
G4_LOCAL = (
    "extern local fn write_row(k: Str) = @py { return }\n"
    "component C {\n"
    '  effect write_row("k")\n'
    "}\n"
)

# the same declared-inverse shape, once on disk and once as an edited buffer
# that overrides the disk path in the compile
G4_FILES_DISK = (
    "type H = Opaque\n"
    "extern pure fn close_h(h: H) -> Unit = @py { return }\n"
    "extern acquire fn open_h() -> H undo close_h(result) = @py { return 1 }\n"
    "component C {\n"
    "  let h = effect open_h()\n"
    "}\n"
)
G4_FILES_EDITED = (
    "type H = Opaque\n"
    "extern pure fn release_h(h: H) -> Unit = @py { return }\n"
    "extern acquire fn open_g() -> H undo release_h(result) = @py { return 1 }\n"
    "component C {\n"
    "  let h = effect open_g()\n"
    "}\n"
)


def _check(source: str) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "revl_check",
                                  "arguments": {"source": source}}})
    return response["result"]["structuredContent"]


def _refusal(source: str) -> RevlError:
    try:
        compile_source(source, "<candidate>.rvl")
    except RevlError as error:
        return error
    raise AssertionError("expected a rejection")


def _g4(source: str) -> dict:
    payload = _check(source)
    assert payload["ok"] is False
    records = [d for d in payload["diagnostics"] if d["code"] == "G4"]
    assert len(records) == 1, payload["diagnostics"]
    return records[0]


def test_the_host_acquisition_record_names_the_acquire():
    record = _g4(G4_HOST)
    assert record["acquire"] == {"file": "<candidate>.rvl", "line": 6,
                                 "extern": "Pool", "method": "open"}
    assert record["declaredClass"] == {"declared": "host",
                                       "derived": "mutation"}


def test_the_repairs_are_ordered_and_the_mechanical_one_is_first():
    record = _g4(G4_HOST)
    kinds = [repair["kind"] for repair in record["repairs"]]
    assert len(record["repairs"]) >= 2
    assert kinds[0] == "site-undo"
    assert "witnessed" in kinds
    assert "service-emission" in kinds
    assert record["repairs"][0]["action"]
    assert record["repairs"][0]["fix_code"]["edits"]


def test_every_listed_fix_code_resolves_the_refusal():
    record = _g4(G4_HOST)
    for repair in record["repairs"]:
        if "fix_code" not in repair:
            continue
        patched = apply_edits(G4_HOST, repair["fix_code"]["edits"])
        after = _check(patched)
        assert after["ok"] is True, after
        assert "diagnostics" not in after


def test_an_unbound_acquisition_keeps_the_repairs_but_no_unverified_code():
    record = _g4(G4_UNBOUND)
    assert record["acquire"]["extern"] == "Pool"
    assert [repair["kind"] for repair in record["repairs"]][0] == "site-undo"
    # no handle to release: the engine declines rather than inventing a name
    assert "fix_code" not in record["repairs"][0]


def test_an_extern_without_an_undo_names_the_declaration():
    record = _g4(G4_DECLARATION)
    assert record["acquire"] == {"file": "<candidate>.rvl", "line": 2,
                                 "extern": "open_h", "method": None}
    assert record["declaredClass"] == {"declared": "acquire",
                                       "derived": "mutation"}
    assert [repair["kind"] for repair in record["repairs"]] == [
        "declare-inverse", "reclassify"]


def test_the_declared_inverse_is_spelled_on_the_bound_handle():
    record = _g4(G4_DECLARED_INVERSE)
    assert record["acquire"]["extern"] == "open_h"
    assert record["declaredClass"]["declared"] == "acquire"
    site = record["repairs"][0]
    assert site["kind"] == "site-undo"
    patch = site["fix_code"]["edits"][0]["newText"]
    assert patch == " undo close_h(h)"
    patched = apply_edits(G4_DECLARED_INVERSE, site["fix_code"]["edits"])
    assert _check(patched)["ok"] is True


def test_a_local_extern_points_at_dropping_the_bracket():
    record = _g4(G4_LOCAL)
    assert record["acquire"]["extern"] == "write_row"
    assert record["declaredClass"] == {"declared": "local",
                                       "derived": "mutation"}
    kinds = [repair["kind"] for repair in record["repairs"]]
    assert kinds[0] == "plain-call"


def test_the_mcp_record_is_the_classified_record_unchanged():
    # the MCP front door passes the compiled text to the fixgen engine and
    # hands the resulting record straight out: no reshaping, no parallel
    # vocabulary
    expected = report(_refusal(G4_HOST),
                      sources={"<candidate>.rvl": G4_HOST})["diagnostics"][0]
    assert _g4(G4_HOST) == expected


def test_an_edited_buffer_is_verified_against_the_text_that_compiled(tmp_path):
    # issue #2684 review: an edited buffer rides as a `modules` overlay on a
    # real path, so the disk copy is pre-edit text that never compiled. The
    # repair must be spelled from the buffer (open_g/release_h), never from
    # the disk copy (open_h/close_h).
    from revl.mcp import server as server_mod
    path = tmp_path / "cand.rvl"
    path.write_text(G4_FILES_DISK)
    server_mod.set_authoring_trust(host_code=True, roots=(str(tmp_path),))
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "revl_check",
                                  "arguments": {
                                      "files": [str(path)],
                                      "modules": {str(path): G4_FILES_EDITED}}}})
    record = response["result"]["structuredContent"]["diagnostics"][0]
    assert record["code"] == "G4"
    assert record["acquire"]["extern"] == "open_g"
    fix = record["repairs"][0]["fix_code"]
    assert "release_h(h)" in fix["edits"][0]["newText"]
    assert "close_h" not in fix["edits"][0]["newText"]
    patched = apply_edits(G4_FILES_EDITED, fix["edits"])
    assert _check(patched)["ok"] is True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
