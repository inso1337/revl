"""Issue #3597: two doors out of redoing work a host already holds.

Part 1 — `Session.offer_render(ir, source, code)` is the public way to hand the
session a render it already made: the next load/swap of the same document hits
the render cache (issue #3383) instead of re-emitting, and reuses the offered
code object instead of re-compiling. The pair is keyed on the document and on
the session's own emitter, so an offer for another document is never used.
Before this, a host had to reach the private
`session._renders.put(_RenderCache.key(emitter, ir), source, code)`.

Part 2 — `_slo_discover` pre-checks each origin file for `slo` AS A WORD, not
as the three letters, so `slow`/`slot`/`myslo` no longer cost a parse each,
while a file that can declare an `slo` block (the keyword is an ident token,
always word-bounded) still reaches the parser and is still found.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp.session import Session  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session tools need the cordis-py runtime — run this file under "
           "`backends/python/.venv/bin/pytest`",
)

CACHE = """
service Cache { fn size() -> Int }
component MemCache provides cache: Cache {
  provide cache { fn size() = 0 }
}
"""

OTHER = """
service Store { fn count() -> Int }
component MemStore provides store: Store {
  provide store { fn count() = 7 }
}
"""


@pytest.fixture(autouse=True)
def _clean_render_cache():
    from revl.mcp import server as server_mod
    server_mod.SESSION._renders._entries.clear()
    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    server_mod.SESSION._renders._entries.clear()


def _render_spy(monkeypatch):
    calls = []
    original = Session._render_or_refuse

    def spy(self, *args, **kwargs):
        calls.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Session, "_render_or_refuse", spy)
    return calls


def _call(tool, arguments):
    from revl.mcp.server import handle
    return handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": tool, "arguments": arguments}})


@needs_runtime
def test_an_offered_render_skips_the_emit_and_reuses_the_offered_code(monkeypatch):
    from revl import run as run_mod
    from revl._paths import python_backend_emitter
    from revl.mcp import server as server_mod

    emitter = python_backend_emitter()
    ir = compile_source(CACHE, "m.rvl")
    source = emitter.emit(ir)
    code = compile(source, "<offered>", "exec")

    session = server_mod.SESSION
    assert session.offer_render(ir, source, code) is True

    calls = _render_spy(monkeypatch)
    renamed = []
    real_renamed = run_mod.renamed_code

    def renamed_spy(code_obj, filename):
        renamed.append(code_obj)
        return real_renamed(code_obj, filename)

    monkeypatch.setattr(run_mod, "renamed_code", renamed_spy)
    session.load(ir)
    assert calls == []                    # the cache hit skipped the emit
    # and the offered code was reused: renamed_code's first (outermost) call
    # is the offered top-level code object, then it recurses into its consts
    assert renamed and renamed[0] is code
    response = _call("revl_call", {"key": "cache", "method": "size"})
    assert response["result"]["structuredContent"]["result"] == 0


@needs_runtime
def test_an_unoffered_load_still_emits(monkeypatch):
    from revl.mcp import server as server_mod

    calls = _render_spy(monkeypatch)
    server_mod.SESSION.load(compile_source(CACHE, "m.rvl"))
    assert calls == [1]


@needs_runtime
def test_an_offer_for_another_document_is_not_used(monkeypatch):
    from revl._paths import python_backend_emitter
    from revl.mcp import server as server_mod

    emitter = python_backend_emitter()
    offered = compile_source(CACHE, "m.rvl")
    session = server_mod.SESSION
    assert session.offer_render(offered, emitter.emit(offered),
                                compile(emitter.emit(offered), "<o>", "exec")) is True

    calls = _render_spy(monkeypatch)
    session.load(compile_source(OTHER, "other.rvl"))
    assert calls == [1]                   # a different document misses the cache
    response = _call("revl_call", {"key": "store", "method": "count"})
    assert response["result"]["structuredContent"]["result"] == 7


@needs_runtime
def test_a_source_only_offer_still_skips_the_emit(monkeypatch):
    from revl._paths import python_backend_emitter
    from revl.mcp import server as server_mod

    emitter = python_backend_emitter()
    ir = compile_source(CACHE, "m.rvl")
    source = emitter.emit(ir)
    session = server_mod.SESSION
    assert session.offer_render(ir, source) is True   # no code object offered

    calls = _render_spy(monkeypatch)
    session.load(ir)
    assert calls == []
    response = _call("revl_call", {"key": "cache", "method": "size"})
    assert response["result"]["structuredContent"]["result"] == 0


def test_an_unhashable_document_is_not_offered():
    class Opaque:
        pass

    session = Session()
    assert session.offer_render({"opaque": Opaque()}, "x", emitter=object()) is False


# ------------------------------------------- part 2: the SLO discovery pre-check

def test_discovery_does_not_parse_files_where_slo_is_only_a_substring(
        tmp_path, monkeypatch):
    decoy = tmp_path / "slow.rvl"
    decoy.write_text("fn slow() = 1  // slot, sloth, myslo, enslo\n")
    session = Session()
    bound = []
    monkeypatch.setattr(Session, "_slo_bind",
                        lambda self, document, root=None: bound.append(document) or False)
    session._slo_discover({"files": [str(decoy)]})
    assert bound == []                    # `slow`/`slot`/... never reached the parser
    assert session._slo_composition is None


def test_a_file_with_the_slo_word_is_still_parsed(tmp_path, monkeypatch):
    mod = tmp_path / "mod.rvl"
    mod.write_text("let slo = 3\n")       # the word, but no composition
    session = Session()
    bound = []
    monkeypatch.setattr(Session, "_slo_bind",
                        lambda self, document, root=None: bound.append(document) or False)
    session._slo_discover({"files": [str(mod)]})
    assert bound == [str(mod)]
    assert session._slo_composition is None


def test_discovery_still_finds_a_contract_behind_substring_decoys(tmp_path):
    decoy = tmp_path / "slow.rvl"
    decoy.write_text("fn slow() = 1  // slot\n")
    doc = tmp_path / "base.rvl"
    doc.write_text(
        'composition base {\n'
        '  slo { p95_latency: 2s }\n'
        '  row @c from "c.rvl" provides c\n'
        '}\n')
    session = Session()
    session._slo_discover({"files": [str(decoy), str(doc)]})
    assert session._slo_composition == str(doc)
    assert session._slo_contract()        # the contract was bound, not just found


def test_a_parse_that_raises_still_leaves_the_session_with_no_contract(tmp_path):
    bad = tmp_path / "bad.rvl"
    bad.write_text("composition broken { slo { \n")   # the word, then a parse error
    session = Session()
    session._slo_discover({"files": [str(bad)]})       # must not raise
    assert session._slo_composition is None
