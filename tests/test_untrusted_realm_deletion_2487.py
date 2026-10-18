"""An untrusted edit may not delete a realm placement (issue #2487).

The untrusted-author profile refuses source that NAMES a realm. Deleting an
`isolate <key> in realm("...")` clause names nothing, yet it moves a surviving
component's provision from its realm into the shared realm, where any
shared-realm component can bind it. The issue's reproduction does exactly that
over the MCP session with `examples/tenants.rvl`: one `revl_change` edit drops
both `tenant_a` clauses, and a later `Snoop` component binds the tenant's store.

The edit is refused by name for the untrusted author. Controls: the same
deletion by a trusted (operator) author is admitted, an untrusted edit that
keeps the clause is admitted, and withdrawing the whole provider is unchanged.
"""

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the change verbs boot a composition and need the cordis-py runtime: "
           "install it with `sh backends/python/setup.sh`")

CLAUSE = 'isolate kv in realm("tenant_a")'
SNOOP = ('component Snoop requires kv: Kv { effect kv.set("snoop", "read") '
         'undo kv.set("snoop", "") }')
REFUSAL = "cannot remove the realm placement"


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _messages(result: dict) -> str:
    return " ".join(str(d.get("message", "")) + " " + str(d.get("hint", ""))
                    for d in result.get("diagnostics") or [])


def _realms(component: str) -> dict:
    for comp in server_mod.SESSION.ir["components"]:
        if comp["name"] == component:
            return dict(comp.get("isolate") or {})
    raise AssertionError(f"{component} is not running")


@pytest.fixture(autouse=True)
def clean_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    old = server_mod.AUTHORING
    session = server_mod.SESSION
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    yield tmp_path
    if session.loaded:
        session.unload()
    session.proposals = {}
    session.pending_draft = None
    session.draft = None
    server_mod.AUTHORING = old


@pytest.fixture
def loaded(clean_session):
    path = clean_session / "tenants.rvl"
    shutil.copy(ROOT / "examples" / "tenants.rvl", path)
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True
    assert _realms("TenantAStore") == {"kv": "tenant_a"}
    return path


def _delete_clause(path) -> dict:
    return _call("revl_change", {"edit": {"target": str(path), "edits": [
        {"anchor": CLAUSE, "replacement": ""}]}})


def test_untrusted_deletion_of_a_realm_clause_is_refused_by_name(loaded):
    assert server_mod.AUTHORING.profile() is not None  # the untrusted author
    proposed = _delete_clause(loaded)
    assert proposed["ok"] is False and proposed.get("admitted") is False, proposed
    message = _messages(proposed)
    assert REFUSAL in message and "TenantAStore" in message, message
    assert "tenant_a" in message, message
    assert any(d.get("code") == "G9" for d in proposed["diagnostics"])
    # the refusal points at the provider in the edited file, not at line 0
    store_line = loaded.read_text().splitlines().index(
        "component TenantAStore provides kv: Kv {") + 1
    refusal = next(d for d in proposed["diagnostics"] if REFUSAL in d["message"])
    assert refusal.get("line") == store_line, refusal
    committed = _call("revl_change", {"commit": True})
    assert not committed.get("swapped"), committed
    assert _realms("TenantAStore") == {"kv": "tenant_a"}
    # the follow-up the issue used to read the tenant's store has nothing to bind
    snoop = _call("revl_change", {"add": {"source": SNOOP}, "commit": True})
    assert not snoop.get("swapped"), snoop


def test_trusted_author_may_delete_the_clause(loaded):
    server_mod.set_authoring_trust(host_code=True)
    assert server_mod.AUTHORING.profile() is None
    proposed = _delete_clause(loaded)
    assert proposed["ok"] is True, proposed
    committed = _call("revl_change", {"commit": True})
    assert committed.get("swapped") is True, committed
    assert _realms("TenantAStore") == {}


def test_untrusted_edit_that_keeps_the_clause_is_admitted(loaded):
    proposed = _call("revl_change", {"edit": {"target": str(loaded), "edits": [
        {"anchor": "quota: 5", "replacement": "quota: 6"}]}})
    assert proposed["ok"] is True, proposed
    committed = _call("revl_change", {"commit": True})
    assert committed.get("swapped") is True, committed
    assert _realms("TenantAApp") == {"kv": "tenant_a"}


def test_untrusted_withdraw_of_the_whole_provider_is_unchanged(loaded):
    """Withdrawing TenantAStore (and, by cascade, TenantAApp) removes the
    provision with the component, so nothing moves into the shared realm: no
    surviving component lost a placement, and the withdraw is admitted."""
    proposed = _call("revl_change", {"withdraw": {"component": "TenantAStore",
                                                  "cascade": True}})
    assert proposed["ok"] is True, proposed
    committed = _call("revl_change", {"commit": True})
    assert committed.get("swapped") is True, committed
    names = {c["name"] for c in server_mod.SESSION.ir["components"]}
    assert names == {"TenantBStore", "TenantBApp"}


def _state(component: str) -> list:
    session = server_mod.SESSION
    fiber = session._driver.fibers.get(component)
    return [res.__revl_state__() for res in session._frame_resources(fiber)]


def test_a_renamed_provider_without_handoff_starts_cold(clean_session):
    """Renaming the provider while dropping its clause moves the provision
    under a new name, which an untrusted author may not do (issue #2523). A
    trusted operator may, and the renamed component is a new provider: with
    no `handoff` on the successor it starts with an empty store. The original
    declares `handoff kv`, so its state is captured, and a marker written
    straight into its store must not reach the successor. (`who` is
    TenantAApp's own seed, re-run against the new provider.)"""
    path = clean_session / "tenants.rvl"
    text = (ROOT / "examples" / "tenants.rvl").read_text()
    path.write_text(text.replace(
        CLAUSE + "\n  let store",
        CLAUSE + "\n  handoff kv: Map[Str, Str]\n  let store", 1))
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True
    store = server_mod.SESSION._frame_resources(
        server_mod.SESSION._driver.fibers["TenantAStore"])[0]
    store.insert("secret", "tenant-a-data")
    assert _state("TenantAStore") == [{"who": "alice", "secret": "tenant-a-data"}]
    edit = {"target": str(path), "edits": [
        {"anchor": "component TenantAStore", "replacement": "component Renamed"},
        {"anchor": CLAUSE, "replacement": ""},
        {"anchor": "handoff kv: Map[Str, Str]", "replacement": ""}]}
    refused = _call("revl_change", {"commit": True, "edit": edit})
    assert not refused.get("swapped") and REFUSAL in _messages(refused), refused
    server_mod.set_authoring_trust(host_code=True)
    renamed = _call("revl_change", {"commit": True, "edit": edit})
    assert renamed.get("swapped") is True, renamed
    assert _realms("Renamed") == {}
    assert _state("Renamed") == [{"who": "alice"}]


def _tenant_a_without_clause() -> str:
    """`Kv`, TenantAStore and TenantAApp as text, both clauses left out: a
    `revl_swap` candidate that names no realm."""
    src = (ROOT / "examples" / "tenants.rvl").read_text()
    cut = (src[src.index("service Kv"):src.index("component TenantBStore")]
           + src[src.index("component TenantAApp"):src.index("component TenantBApp")])
    return cut.replace(CLAUSE, "")


def test_untrusted_swap_that_leaves_the_clause_out_is_refused(loaded):
    """The same move through `revl_swap` with inline source."""
    swapped = _call("revl_swap", {"source": _tenant_a_without_clause(),
                                  "replacing": ["TenantAStore", "TenantAApp"]})
    assert swapped.get("swapped") is False, swapped
    assert REFUSAL in _messages(swapped), swapped
    assert any(d.get("code") == "G9" for d in swapped["diagnostics"]), swapped
    assert _realms("TenantAStore") == {"kv": "tenant_a"}


def test_swap_refusal_names_the_component_file_and_line(loaded):
    """The refusal points at the provider in the candidate text sent (#2519),
    not at line 0."""
    candidate = _tenant_a_without_clause()
    swapped = _call("revl_swap", {"source": candidate,
                                  "replacing": ["TenantAStore", "TenantAApp"]})
    line = candidate.splitlines().index(
        "component TenantAStore provides kv: Kv {") + 1
    refusal = next(d for d in swapped["diagnostics"] if REFUSAL in d["message"])
    assert refusal.get("file") == "<candidate>.rvl", refusal
    assert refusal.get("line") == line, refusal


def test_trusted_swap_that_leaves_the_clause_out_is_admitted(loaded):
    server_mod.set_authoring_trust(host_code=True)
    swapped = _call("revl_swap", {"source": _tenant_a_without_clause(),
                                  "replacing": ["TenantAStore", "TenantAApp"]})
    assert swapped.get("swapped") is True, swapped
    assert _realms("TenantAStore") == {}


def test_swap_of_jailed_files_alone_is_admitted(loaded):
    """Files from the jailed roots are the operator's own: a file the operator
    changed on disk to drop the clause swaps in, as before."""
    loaded.write_text(loaded.read_text().replace(CLAUSE, ""))
    swapped = _call("revl_swap", {"files": [str(loaded)],
                                  "replacing": ["TenantAStore", "TenantAApp"]})
    assert swapped.get("swapped") is True, swapped
    assert _realms("TenantAStore") == {}


def test_unchanged_realm_scoped_swap_is_admitted(loaded):
    """Nothing moves: the server-side source swaps back in with its clauses."""
    swapped = _call("revl_swap", {"replacing": ["TenantAStore", "TenantAApp"]})
    assert swapped.get("swapped") is True, swapped
    assert _realms("TenantAStore") == {"kv": "tenant_a"}
