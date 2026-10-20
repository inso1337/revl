"""An untrusted edit may not delete a requirer's realm placement (issue #2585).

Issues #2487 and #2523 refuse an untrusted edit that deletes the `isolate <key>
in realm("...")` clause of a provider that stays admitted. The comparison
walked only `provides` keys, so the clause of a component that REQUIRES a key
was never compared. The policy counts require-side placements: a realm-scoped
allow or deny rule selects a component by every realm it is isolated into,
require keys included. A requirer that loses its clause leaves the realm the
operator's reach rules were written for, and rebinds to a shared-realm provider
of the same key when one exists, which an ordinary add supplies.

The edit is refused by name for the untrusted author, as for a provider.
Controls: a trusted (operator) author may delete the clause, and an untrusted
edit that keeps both clauses is admitted.
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

REFUSAL = "cannot remove the realm placement"
APP_OPEN = "component TenantAApp requires kv: Kv {\n"
APP_CLAUSE = '  isolate kv in realm("tenant_a")\n'
SHARED_STORE = """component SharedStore provides kv: Kv {
  let store = effect Map.new() undo store.drop()
  provide kv {
    fn get(k) = store.get(k)
    fn set(k, v) {
      effect store.insert(k, v)
      undo   store.remove(k)
    }
    fn unset(k) = store.remove(k)
  }
}

"""


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
    assert _realms("TenantAApp") == {"kv": "tenant_a"}
    return path


def _drop_requirer_clause() -> dict:
    """Delete only the requirer's clause: the anchor spans the component line,
    since the provider's clause reads the same."""
    return {"anchor": APP_OPEN + APP_CLAUSE, "replacement": APP_OPEN}


def _add_shared_provider() -> dict:
    """A provider of `kv` in the shared realm, which an untrusted author may
    add, inserted ahead of the second tenant's store."""
    anchor = "component TenantBStore provides kv: Kv {"
    return {"anchor": anchor, "replacement": SHARED_STORE + anchor}


def _edit(path, *edits: dict) -> dict:
    return {"target": str(path), "edits": list(edits)}


def _line_of(text: str, header: str) -> int:
    return text.splitlines().index(header) + 1


def test_untrusted_deletion_of_the_requirers_clause_is_refused_by_name(loaded):
    assert server_mod.AUTHORING.profile() is not None  # the untrusted author
    proposed = _call("revl_change", {"edit": _edit(loaded,
                                                   _drop_requirer_clause())})
    assert proposed["ok"] is False and proposed.get("admitted") is False, proposed
    message = _messages(proposed)
    assert REFUSAL in message and "TenantAApp" in message, message
    assert "tenant_a" in message and "`kv` requirement" in message, message
    refusal = next(d for d in proposed["diagnostics"] if REFUSAL in d["message"])
    assert refusal.get("code") == "G9", refusal
    # it points at the requirer, in the edited file
    assert str(refusal.get("file")).endswith(loaded.name), refusal
    assert refusal.get("line") == _line_of(
        loaded.read_text(encoding="utf-8"), "component TenantAApp requires kv: Kv {"), refusal
    committed = _call("revl_change", {"commit": True})
    assert not committed.get("swapped"), committed
    assert _realms("TenantAApp") == {"kv": "tenant_a"}


def test_untrusted_deletion_with_a_shared_provider_added_is_refused(loaded):
    """The add alone is fine; the requirer leaving tenant_a for the shared
    realm, where it would bind the new provider, is what is refused."""
    proposed = _call("revl_change", {"edit": _edit(
        loaded, _add_shared_provider(), _drop_requirer_clause())})
    assert proposed["ok"] is False and proposed.get("admitted") is False, proposed
    message = _messages(proposed)
    assert REFUSAL in message and "TenantAApp" in message, message
    assert any(d.get("code") == "G9" for d in proposed["diagnostics"]), proposed
    committed = _call("revl_change", {"commit": True})
    assert not committed.get("swapped"), committed
    assert _realms("TenantAApp") == {"kv": "tenant_a"}
    running = {c["name"] for c in server_mod.SESSION.ir["components"]}
    assert "SharedStore" not in running, running


def test_trusted_author_may_delete_the_requirers_clause(loaded):
    server_mod.set_authoring_trust(host_code=True)
    assert server_mod.AUTHORING.profile() is None
    proposed = _call("revl_change", {"edit": _edit(
        loaded, _add_shared_provider(), _drop_requirer_clause())})
    assert proposed["ok"] is True, proposed
    committed = _call("revl_change", {"commit": True})
    assert committed.get("swapped") is True, committed
    assert _realms("TenantAApp") == {}


def test_untrusted_edit_that_keeps_both_clauses_is_admitted(loaded):
    proposed = _call("revl_change", {"edit": _edit(loaded, {
        "anchor": 'kv.set("who", "alice")',
        "replacement": 'kv.set("who", "carol")'})})
    assert proposed["ok"] is True, proposed
    committed = _call("revl_change", {"commit": True})
    assert committed.get("swapped") is True, committed
    assert _realms("TenantAApp") == {"kv": "tenant_a"}
    assert _realms("TenantAStore") == {"kv": "tenant_a"}
