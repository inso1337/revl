"""revl_edit edits a composition loaded from files, and loads one itself (#1690).

An agent working only through MCP could not change a composition it had loaded
from `files`: `revl_edit` patched inline buffers only and answered "there is no
inline `source` buffer to edit", while `revl_snapshot` already returned those
files' text. In an agent benchmark both MCP-only arms failed a change task on
that refusal. These tests hold the four exits the issue names, plus the gates
the new path has to keep:

* a files-loaded, multi-file composition edited through `revl_edit` admits and
  swaps, and the disk is never written;
* one call edits two files, and a `use` between them resolves to the edited
  text;
* a patch that breaks a guarantee is refused, with the running system, the
  working buffers and the disk untouched;
* with nothing loaded, `revl_edit` with `files` loads and edits in one call;
* edited file text arrived over the transport, so it compiles under the
  authoring profile: a host extern an edit adds is refused;
* the lease and quarantine gates see the edited files, not the disk.

The gate tests are a pure decision over a recorded session and need no
runtime; the rest boot one.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_files  # noqa: E402
from revl.mcp import edit as E  # noqa: E402
from revl.mcp import leases as L  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.leases import LeaseBook  # noqa: E402
from revl.mcp.operator import Operator  # noqa: E402
from revl.mcp.server import handle  # noqa: E402
from revl.policy import parse_policy  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session tools need the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh`, then run this file under "
           "`backends/python/.venv/bin/pytest`",
)

SERVICE = "service Tool { fn describe() -> Str }\n"
LIB = 'pub fn label() -> Str { return "v1" }\n'
MAIN = ('use "lib.rvl" { label }\n'
        "component T provides tool: Tool {\n"
        "  provide tool { fn describe() = label() }\n"
        "}\n")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _describe() -> str:
    return _call("revl_call", {"key": "tool", "method": "describe"})["result"]


@pytest.fixture
def composition(tmp_path):
    """Three files in a sanctioned root: a service, a library, and a component
    that `use`s the library. Yields the paths in load order."""
    paths = []
    for name, text in (("svc.rvl", SERVICE), ("lib.rvl", LIB), ("main.rvl", MAIN)):
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        paths.append(str(path))
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    try:
        yield paths
    finally:
        if server_mod.SESSION.loaded:
            server_mod.SESSION.unload()
        server_mod.SESSION.draft = None
        server_mod.AUTHORING = old


def _disk(paths) -> list[str]:
    return [Path(p).read_text(encoding="utf-8") for p in paths]


@needs_runtime
def test_a_files_loaded_composition_is_edited_and_swapped(composition):
    svc, lib, main = composition
    assert _call("revl_load", {"files": composition})["ok"] is True
    assert _describe() == "v1"

    result = _call("revl_edit", {"target": lib, "edits": [
        {"anchor": '"v1"', "replacement": '"v1-edited"'}]})
    assert result["ok"] is True, result
    assert result["admitted"] is True and result["swapped"] is True
    assert result["applied"][0]["target"] == lib
    assert _describe() == "v1-edited"
    # the disk is never written; the session holds the edited text
    assert _disk(composition) == [SERVICE, LIB, MAIN]
    held = _call("revl_snapshot", {})["snapshot"]["sources"]["files_content"]
    assert '"v1-edited"' in held[lib]
    # and the next edit starts from what is running, not from the disk
    again = _call("revl_edit", {"target": lib, "edits": [
        {"anchor": '"v1-edited"', "replacement": '"v1-twice"'}]})
    assert again["swapped"] is True, again
    assert _describe() == "v1-twice"


@needs_runtime
def test_one_call_edits_two_files_and_the_import_between_them_resolves(composition):
    """`label2` exists only in the edited lib.rvl, and the edited main.rvl
    imports it: the compile has to read lib.rvl from the edited buffer."""
    svc, lib, main = composition
    _call("revl_load", {"files": composition})
    result = _call("revl_edit", {"edits": [
        {"target": lib, "anchor": "}\n",
         "replacement": '}\npub fn label2() -> Str { return "v2" }\n'},
        {"target": main, "anchor": "{ label }", "replacement": "{ label, label2 }"},
        {"target": main, "anchor": "= label()", "replacement": "= label2()"},
    ]})
    assert result["ok"] is True, result
    assert result["swapped"] is True
    assert [a["target"] for a in result["applied"]] == [lib, main, main]
    assert _describe() == "v2"
    assert _disk(composition) == [SERVICE, LIB, MAIN]


@needs_runtime
def test_a_patch_that_breaks_a_guarantee_leaves_everything_untouched(composition):
    svc, lib, main = composition
    _call("revl_load", {"files": composition})
    refused = _call("revl_edit", {"target": main, "edits": [
        {"anchor": "= label()", "replacement": "= 42"}]})
    assert refused["ok"] is False
    assert refused["edited"] is False and refused["swapped"] is False
    assert refused["diagnostics"][0]["code"] == "T1"
    assert _describe() == "v1"
    assert _disk(composition) == [SERVICE, LIB, MAIN]
    # the working buffer did not advance: the original anchor still matches
    fixed = _call("revl_edit", {"target": main, "edits": [
        {"anchor": "= label()", "replacement": '= "fixed"'}]})
    assert fixed["swapped"] is True, fixed
    assert _describe() == "fixed"


@needs_runtime
def test_with_nothing_loaded_files_load_and_edit_in_one_call(composition):
    svc, lib, main = composition
    assert not server_mod.SESSION.loaded
    result = _call("revl_edit", {"files": composition, "target": lib, "edits": [
        {"anchor": '"v1"', "replacement": '"loaded-and-edited"'}]})
    assert result["ok"] is True, result
    assert result["loaded"] is True and result["swapped"] is True
    assert _describe() == "loaded-and-edited"
    assert _disk(composition) == [SERVICE, LIB, MAIN]


@needs_runtime
def test_with_nothing_loaded_source_and_modules_load_and_edit_in_one_call(
        composition):
    """The inline form of the same call, whose `use` is satisfied only by the
    call's own `modules`: the load it performs carries them."""
    source = SERVICE + MAIN
    result = _call("revl_edit", {"source": source, "modules": {"lib.rvl": LIB},
                                 "edits": [{"anchor": "= label()",
                                            "replacement": '= "inline"'}]})
    assert result["ok"] is True, result
    assert result["loaded"] is True and result["swapped"] is True
    assert _describe() == "inline"


@needs_runtime
def test_files_with_a_composition_loaded_is_refused_not_reloaded(composition):
    _call("revl_load", {"files": composition})
    result = _call("revl_edit", {"files": composition, "edits": [
        {"anchor": '"v1"', "replacement": '"x"'}]})
    assert result["ok"] is False and result["swapped"] is False
    assert "already loaded" in result["diagnostics"][0]["message"]
    assert _describe() == "v1"


@needs_runtime
def test_several_files_need_a_target(composition):
    _call("revl_load", {"files": composition})
    result = _call("revl_edit", {"edits": [{"anchor": '"v1"', "replacement": '"x"'}]})
    assert result["ok"] is False
    message = result["diagnostics"][0]["message"]
    assert "loaded from 3 files" in message and composition[1] in message


@needs_runtime
def test_one_loaded_file_is_the_default_target(tmp_path):
    path = tmp_path / "one.rvl"
    path.write_text(SERVICE + MAIN.replace('use "lib.rvl" { label }\n', "")
                    .replace("= label()", '= "one"'), encoding="utf-8")
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    try:
        _call("revl_load", {"files": [str(path)]})
        result = _call("revl_edit", {"edits": [
            {"anchor": '"one"', "replacement": '"two"'}]})
        assert result["swapped"] is True, result
        assert _describe() == "two"
    finally:
        if server_mod.SESSION.loaded:
            server_mod.SESSION.unload()
        server_mod.AUTHORING = old


@needs_runtime
def test_an_edit_that_adds_host_code_compiles_under_the_authoring_profile(composition):
    """The loaded files compiled as the operator's own; an edited buffer is text
    that came over the transport, so a host extern it adds is refused as it is
    for `revl_swap` with inline source. The replacement alone does not parse,
    so the check that refuses it is the compile, not the pre-handler scan."""
    svc, lib, main = composition
    _call("revl_load", {"files": composition})
    refused = _call("revl_edit", {"target": main, "edits": [
        {"anchor": "component T",
         "replacement": "extern pure fn boom() -> Int = @py { return 1 }\n"
                        "component T"}]})
    assert refused["ok"] is False and refused["swapped"] is False
    assert "untrusted-author profile" in refused["diagnostics"][0]["message"]
    assert _describe() == "v1"


@needs_runtime
def test_swap_by_name_re_admits_a_files_loaded_composition(composition):
    _call("revl_load", {"files": composition})
    result = _call("revl_swap", {})
    assert result["ok"] is True and result["swapped"] is True, result
    assert result["fromServerSide"] is True


# ------------------------------------------- the gates see the edited files


class _FilesSession:
    """Enough of a session for `apply_edit` on a files-loaded composition: the
    compiled `ir`, the loaded paths as `origin`, a real `LeaseBook`, the bound
    operator/policy, and a `swap` that records instead of booting."""

    def __init__(self, paths, operator=None, sandbox=None):
        self.ir = compile_files(list(paths))
        self.origin = {"files": list(paths)}
        self.draft = None
        self.loaded = True
        self.operator = operator
        self.sandbox = sandbox
        self.leases = LeaseBook()
        self.swaps = []

    def swap(self, ir, origin=None):
        self.swaps.append((ir, origin))
        return {"generation": len(self.swaps)}


def _edit_lib(lib):
    return {"target": lib, "edits": [{"anchor": '"v1"', "replacement": '"v9"'}]}


def test_the_gates_see_the_edited_files_not_the_disk(composition, monkeypatch):
    svc, lib, main = composition
    seen = []
    monkeypatch.setattr(server_mod._quarantine, "gate_swap",
                        lambda session, arguments: seen.append(arguments))
    sess = _FilesSession(composition, Operator("alice"),
                         sandbox=parse_policy("quarantine required"))
    out = E.apply_edit(sess, _edit_lib(lib))
    assert out["swapped"] is True, out
    (arguments,) = seen
    assert arguments["files"] == composition
    assert '"v9"' in arguments["modules"][os.path.abspath(lib)]
    assert L._swap_targets(sess, arguments) == ["T"]
    # what swapped in carries the edited text, for the next snapshot and edit
    _ir, origin = sess.swaps[0]
    assert '"v9"' in origin["files_content"][lib]
    assert _disk(composition) == [SERVICE, LIB, MAIN]


def test_an_enforced_lease_refuses_a_files_edit_and_nothing_swaps(composition):
    svc, lib, main = composition
    sess = _FilesSession(composition, Operator("alice"),
                         sandbox=parse_policy("leases enforced"))
    sess.leases.claim("T", "bob", ttl=600)
    out = E.apply_edit(sess, _edit_lib(lib))
    assert out["ok"] is False and out["swapped"] is False
    assert out["lease"]["heldBy"] == "bob"
    assert sess.swaps == [] and sess.draft is None
