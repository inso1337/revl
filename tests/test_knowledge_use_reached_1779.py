"""A vendored truc's `pub` helpers, reached through `use`, carry their records
(issue #1779, the deferred remainder of #1769 / PR #1775).

#1775 serves a vendored truc's records when `trucs/<name>/component.rvl` is one
of the loaded `files`. A record anchored on a `pub fn` in a truc the composition
reaches through `use` had nowhere to anchor: anchoring, fingerprints and
`revl_source` all go through `symbols.locate`, which sees only the session's
buffers, and no buffer held the file.

The compile now reports what it READ — outside the IR, so the IR bytes are
identical whether or not a caller asked for the report — and the session holds
each reported `trucs/<name>/` file as a READ-ONLY buffer of the exact text that
compiled. `notes.load_vendored` walks those buffers under #1775's trust rules
unchanged: `publisher` only when the lock row says `signed` and the held text
hashes to the pin, `untrusted` otherwise.

The issue's exits:

* a record on a truc's `pub fn`, reached through `use`, is served on
  `revl_source` for that function and rides an edit of a caller — untrusted, or
  the publisher's when signed;
* `revl_edit` naming the `use`d file is refused, and no swap or export writes
  it;
* the compiled IR is byte-identical with and without the report.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compiler as compiler_mod  # noqa: E402
from revl import registry  # noqa: E402
from revl import registry_evidence  # noqa: E402
from revl.mcp import edit as edit_mod  # noqa: E402
from revl.mcp import notes as notes_mod  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402
from revl.truc import _host  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="serving a note needs a loaded composition, which needs the "
           "cordis-py runtime; install it with `sh backends/python/setup.sh`")

# the vendored entry: a `pub fn` helper beside the service it backs
GREETER = ('pub fn shout(name: Str) -> Str { return name.concat("!") }\n'
           "service Greet { fn hello(name: Str) -> Str }\n"
           "component Greeter provides greet: Greet {\n"
           "  provide greet { fn hello(name) = shout(name) }\n"
           "}\n")
# the consumer: it reaches `shout` through `use`, and never names the file in
# `files`, which is exactly the #1779 gap
CONSUMER = ('service App { fn hi(name: Str) -> Str }\n'
            'use "trucs/greeter/component.rvl" { shout }\n'
            "component AppProvider provides app: App {\n"
            "  provide app { fn hi(name) = shout(name) }\n"
            "}\n")
TRAP_ID = "k_0123456789ab"
TRAP_BODY = "shout does not trim the name"
KEY = "publisher-secret"
CLAIMED = "sha256:the-publishers"


def _trap() -> dict:
    return {"op": "note", "id": TRAP_ID, "kind": "trap",
            "anchor": {"path": "component.rvl", "symbol": "shout",
                       "fingerprint": CLAIMED},
            "body": TRAP_BODY, "evidence": [],
            "author": {"kind": "agent", "trust": "untrusted"},
            "created": "2026-10-03T00:00:00+00:00", "seq": 1}


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A consumer project whose `local` registry ships `greeter` with a trap."""
    monkeypatch.delenv("REVL_ATTEST_KEY", raising=False)
    monkeypatch.delenv("REVL_ATTEST_KEY_FILE", raising=False)
    reg = tmp_path / "registry"
    (reg / "components").mkdir(parents=True)
    (reg / "index.json").write_text(json.dumps({"indexVersion": "0", "components": {}}))
    registry.publish_release(reg, "greeter", GREETER, description="greets",
                             tags=["greet"], knowledge=[_trap()])
    proj = tmp_path / "app"
    proj.mkdir()
    # the entry sits at the project ROOT: `use` resolves relative to the using
    # file, and an MCP `files` argument is jailed to the sanctioned root
    (proj / "main.rvl").write_text(CONSUMER)
    (proj / "truc.toml").write_text(
        '[assembly]\nname = "demo"\nentry = ["main.rvl"]\n\n'
        f'[registries]\nlocal = {{ path = "{reg}" }}\n\n[trucs]\n')
    monkeypatch.chdir(proj)
    old = server_mod.AUTHORING
    old_session = server_mod.SESSION
    server_mod.set_authoring_trust(roots=(str(proj),))
    try:
        yield proj, reg
    finally:
        # a probe may have stood a stand-in session in (`_export_plan` reads
        # only its notes), and monkeypatch undoes that after this teardown
        server_mod.SESSION = old_session
        if old_session.loaded:
            old_session.unload()
        notes_mod.clear(old_session)
        old_session.draft = None
        old_session.proposals = {}
        server_mod.AUTHORING = old


def _add(proj: Path) -> dict:
    plan = json.dumps({"lockAdd": {"name": "greeter", "registry": "local"}})
    assert _host.commit_add(str(proj), plan) == "committed"
    lock = json.loads((proj / "truc.lock").read_text())
    (row,) = [r for r in lock["trucs"] if r["name"] == "greeter"]
    return row


def _sign(reg: Path, monkeypatch) -> None:
    registry_evidence.build_evidence(reg, key=KEY.encode(), signer="publisher")
    monkeypatch.setenv("REVL_ATTEST_KEY", KEY)


def _truc_path(proj: Path) -> str:
    return str(proj / "trucs" / "greeter" / "component.rvl")


def _report(proj: Path) -> dict:
    """The compile report of the consumer alone: the truc is reached through
    `use`, never named in `paths`."""
    report: dict = {}
    compiler_mod.compile_files([str(proj / "main.rvl")], report=report)
    return report


def _held(proj: Path) -> dict:
    """The working set a session loaded from `main.rvl` holds, built through the
    same two steps `_tool_load` uses: the compile, then the origin."""
    origin = server_mod._origin(
        {"files": [str(proj / "main.rvl")]},
        server_mod._dependency_buffers(_report(proj)))
    return edit_mod._files_source(origin)


def _load_use_reached(proj: Path) -> dict:
    """Load the consumer. The truc is NOT a `files` argument: the only reason
    the session knows it is that the compile reached it through `use`."""
    loaded = _call("revl_load", {"files": [str(proj / "main.rvl")]})
    assert loaded["ok"] is True, loaded
    return loaded


def _served(proj: Path, symbol: str = "shout") -> list:
    return _call("revl_source", {"symbol": symbol,
                                 "with": ["knowledge"]})["knowledge"]["notes"]


# ------------------------------------------------ what the compile reports


def test_the_compile_reports_the_text_it_read_for_a_use_reached_file(project):
    proj, _reg = project
    _add(proj)
    report = _report(proj)
    dependencies = report["dependencies"]
    assert len(dependencies) == 1, dependencies
    (path, text), = dependencies.items()
    assert os.path.realpath(path) == os.path.realpath(_truc_path(proj))
    # the text the compile READ, not a path to re-read later
    assert text == GREETER


def test_the_ir_is_byte_identical_with_and_without_the_report(project):
    proj, _reg = project
    _add(proj)
    main = str(proj / "main.rvl")
    plain = compiler_mod.compile_files([main])
    report: dict = {}
    asked = compiler_mod.compile_files([main], report=report)
    # the report is not vacuous, or the equality below would prove nothing
    assert report["dependencies"], report
    assert json.dumps(plain, sort_keys=True).encode() \
        == json.dumps(asked, sort_keys=True).encode()
    assert set(plain) == set(asked)
    assert "report" not in asked and "dependencies" not in asked


# ------------------------------------------------ read-only dependency buffers


def test_a_use_reached_file_is_a_read_only_buffer(project):
    proj, _reg = project
    _add(proj)
    vs = _held(proj)
    truc = _truc_path(proj)
    # it IS a buffer, holding the compiled bytes...
    assert list(vs[edit_mod.ORIGIN_DEPENDENCIES]) == [truc]
    assert vs[edit_mod.ORIGIN_DEPENDENCIES][truc] == GREETER
    # ...and not an editable one
    assert truc not in edit_mod._editable(vs)
    for named in (truc, "trucs/greeter/component.rvl", "component.rvl"):
        with pytest.raises(edit_mod.EditError, match="READ-ONLY"):
            edit_mod._resolve_buffer(vs, named)
    # and no write reaches it even when addressed as a buffer directly
    with pytest.raises(edit_mod.EditError, match="READ-ONLY"):
        edit_mod._get_text(vs, ("dependency", truc))
    with pytest.raises(edit_mod.EditError, match="READ-ONLY"):
        edit_mod._set_text(vs, ("dependency", truc),
                           "pub fn shout(n: Str) -> Str { return n }")
    assert vs[edit_mod.ORIGIN_DEPENDENCIES][truc] == GREETER


def test_no_swap_candidate_carries_a_use_reached_file(project):
    proj, _reg = project
    _add(proj)
    vs = _held(proj)
    # the compiler keeps reading the file from disk: it is in neither the
    # candidate's `files` nor its `modules`, so a swap cannot write it
    assert edit_mod.file_modules(vs) == {}
    arguments = edit_mod.candidate_arguments(vs)
    assert arguments == {"files": [str(proj / "main.rvl")], "replacing": []}
    assert GREETER not in json.dumps(arguments)


@needs_runtime
def test_no_export_plan_names_a_use_reached_file(project):
    proj, _reg = project
    _add(proj)
    _load_use_reached(proj)
    session = server_mod.SESSION
    vs = edit_mod._files_source(session.origin)
    # a PROJECT note makes the plan non-empty, so the truc's absence below is
    # a fact about the plan and not a fact about its size
    notes_mod.add(session, vs,
                  {"kind": "invariant", "body": "hi stays a pure concat"},
                  "AppProvider.app")
    plan = server_mod._export_plan(vs, {"with_knowledge": True})
    assert plan, "the plan must be non-vacuous"
    assert _truc_path(proj) not in [p for p, _ in plan]
    assert GREETER not in json.dumps(plan)
    # the vendored record is in the store and is still not rendered anywhere
    assert TRAP_ID in notes_mod.notes(session)
    assert TRAP_BODY not in json.dumps(plan)


# ------------------------------------------------ served, and riding


@needs_runtime
def test_a_record_on_a_use_reached_pub_fn_is_served_untrusted(project):
    proj, _reg = project
    _add(proj)
    loaded = _load_use_reached(proj)
    # the IR names none of the resolved paths: the report is the only record
    assert _truc_path(proj) not in json.dumps(loaded)
    # ...yet the session records the resolved path, though nothing named it
    assert list(server_mod.SESSION.origin[edit_mod.ORIGIN_DEPENDENCIES]) \
        == [_truc_path(proj)]
    (served,) = _served(proj)
    assert served["id"] == TRAP_ID and served["kind"] == "trap"
    assert served["vendored"] == "greeter" and served["status"] == "live"
    assert served["author"]["trust"] == "untrusted"
    assert served["anchor"]["symbol"] == "shout"
    assert served["anchor"]["path"] == _truc_path(proj)
    # re-based on what compiled, not on the fingerprint the record claims
    assert served["anchor"]["fingerprint"].startswith("sha256:")
    assert served["anchor"]["fingerprint"] != CLAIMED
    # an untrusted, evidence-free note withholds its body by default
    assert "body" not in served and "bodyWithheld" in served


@needs_runtime
def test_a_signed_record_on_a_use_reached_pub_fn_is_the_publishers(project,
                                                                   monkeypatch):
    proj, reg = project
    _sign(reg, monkeypatch)
    _add(proj)
    _load_use_reached(proj)
    (served,) = _served(proj)
    assert served["author"]["trust"] == "publisher"
    assert served["body"] == TRAP_BODY


@needs_runtime
def test_the_use_reached_file_is_addressable_by_its_path(project):
    proj, _reg = project
    _add(proj)
    _load_use_reached(proj)
    qualified = _call("revl_source",
                      {"symbol": "trucs/greeter/component.rvl:shout",
                       "with": ["knowledge"]})["knowledge"]["notes"]
    assert [n["id"] for n in qualified] == [TRAP_ID]


@needs_runtime
def test_a_use_reached_record_survives_an_edit_of_a_caller(project):
    proj, _reg = project
    _add(proj)
    _load_use_reached(proj)
    edited = _call("revl_edit", {
        "edits": [{"target": str(proj / "main.rvl"),
                   "anchor": "fn hi(name) = shout(name)",
                   "replacement": 'fn hi(name) = shout(name).concat("?")'}]})
    assert edited["ok"] is True and edited["swapped"] is True, edited
    # the caller changed; the vendored helper did not
    assert [t["symbol"] for t in edited["touched"]] == ["AppProvider.app.hi"]
    # the read-only buffer rode the swap holding the text that compiled, so the
    # record is still anchored, still live, and still measured against it
    # the record is still served by name afterwards; delivering it inside the
    # edit's own response is NOT asserted here (a #1779 follow-up)
    held = server_mod.SESSION.origin[edit_mod.ORIGIN_DEPENDENCIES]
    assert held[_truc_path(proj)] == GREETER
    (served,) = _served(proj)
    assert served["id"] == TRAP_ID and served["status"] == "live"
    assert served["anchor"]["fingerprint"] != CLAIMED


@needs_runtime
def test_an_edit_naming_the_use_d_file_is_refused_and_nothing_writes_it(project):
    proj, _reg = project
    _add(proj)
    _load_use_reached(proj)
    truc = Path(_truc_path(proj))
    before = truc.read_bytes()
    by_path = _call("revl_edit", {
        "edits": [{"target": _truc_path(proj),
                   "anchor": 'fn shout(name) = name.concat("!")',
                   "replacement": "fn shout(name) = name"}]})
    assert by_path["ok"] is False, by_path
    assert "READ-ONLY" in json.dumps(by_path), by_path
    by_symbol = _call("revl_edit", {
        "edits": [{"symbol": "shout",
                   "replacement": 'pub fn shout(name: Str) -> Str { return name }'}]})
    assert by_symbol["ok"] is False, by_symbol
    assert "READ-ONLY" in json.dumps(by_symbol), by_symbol
    # a swap re-admits what the session holds and still does not write it
    assert _call("revl_swap", {})["ok"] is True
    exported = _call("revl_export", {"with_knowledge": True})
    assert exported["ok"] is True, exported
    assert truc.read_bytes() == before
    # and the vendored note was never rendered into the project's own file
    assert TRAP_BODY not in (proj / "main.rvl").read_text()
