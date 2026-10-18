"""An untrusted edit may not move a realm placement by renaming the provider
(issue #2523).

Issue #2487 refuses an untrusted edit that deletes the `isolate <key> in
realm("...")` clause of a provider that stays admitted. That check compared
placements by component name, so renaming the provider in the same edit left
no survivor to compare and the provision moved into the shared realm anyway,
where a later shared-realm component binds the key. The comparison is now per
(key, realm) across all components, so the rename is refused through every
door that admits an edit. Controls: a trusted operator may rename, a provider
withdrawn together with its key is admitted, and renaming the key itself
is refused by the realm-set check (issue #2586).
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

KV = """service Kv {
  fn get(k: Str) -> Opt[Str]
  fn set(k: Str, v: Str)
  fn unset(k: Str)
}
"""
STORE = """component %s provides %s: Kv {
  %s
  let store = effect Map.new() undo store.drop()
  provide %s {
    fn get(k) = store.get(k)
    fn set(k, v) {
      effect store.insert(k, v)
      undo   store.remove(k)
    }
    fn unset(k) = store.remove(k)
  }
}
"""
APP = """component App requires %s: Kv {
  %s
  effect %s.set("who", "alice") undo %s.unset("who")
}
"""
CLAUSE = 'isolate kv in realm("billing")'
BODY = STORE % ("Vault", "kv", CLAUSE, "kv") + APP % ("kv", CLAUSE, "kv", "kv")
SNOOP = ('component Snoop requires kv: Kv { effect kv.set("snoop", "read") '
         'undo kv.set("snoop", "") }')


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _messages(result: dict) -> str:
    return " ".join(str(d.get("message", "")) + " " + str(d.get("hint", ""))
                    for d in result.get("diagnostics") or [])


def _placements() -> dict:
    return {c["name"]: dict(c.get("isolate") or {})
            for c in server_mod.SESSION.ir["components"]}


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
def vault(clean_session):
    path = clean_session / "vault.rvl"
    path.write_text(KV + BODY)
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True
    assert _placements() == {"Vault": {"kv": "billing"}, "App": {"kv": "billing"}}
    return path


@pytest.fixture
def tenants(clean_session):
    path = clean_session / "tenants.rvl"
    shutil.copy(ROOT / "examples" / "tenants.rvl", path)
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True
    assert _placements()["TenantAStore"] == {"kv": "tenant_a"}
    return path


def _rename_and_drop(path) -> list:
    """Rename the provider and drop both realm clauses (the anchor matches the
    provider's and the consumer's clause alike)."""
    return [{"anchor": "component Vault provides", "replacement":
             "component Vault2 provides"},
            {"anchor": CLAUSE, "replacement": ""}]


def _assert_refused_by_name(result: dict, path, provider: str, was: str,
                            realm: str) -> None:
    assert result.get("swapped") is not True, result
    assert result.get("admitted") is not True, result
    message = _messages(result)
    assert REFUSAL in message, message
    assert f"`{provider}`" in message and f"`{was}`" in message, message
    assert f'realm("{realm}")' in message, message
    refusal = next(d for d in result["diagnostics"] if REFUSAL in d["message"])
    assert refusal.get("code") == "G9", refusal
    # it points at the component that now provides the key, in its own file,
    # on the line that component starts on (the renamed line of the original)
    assert str(refusal.get("file")).endswith(path.name), refusal
    original = f"component {was} provides kv: Kv {{"
    line = path.read_text().splitlines().index(original) + 1
    assert refusal.get("line") == line, refusal


def test_untrusted_rename_and_drop_through_revl_edit_is_refused(vault):
    assert server_mod.AUTHORING.profile() is not None  # the untrusted author
    result = _call("revl_edit", {"target": str(vault),
                                 "edits": _rename_and_drop(vault)})
    _assert_refused_by_name(result, vault, "Vault2", "Vault", "billing")
    assert _placements() == {"Vault": {"kv": "billing"}, "App": {"kv": "billing"}}
    # the second consumer the issue used to bind the moved key finds nothing
    # in the shared realm
    snoop = _call("revl_change", {"add": {"source": SNOOP}, "commit": True})
    assert not snoop.get("swapped"), snoop
    assert "Snoop" not in _placements()


def test_untrusted_rename_and_drop_through_revl_change_is_refused(tenants):
    proposed = _call("revl_change", {"edit": {"target": str(tenants), "edits": [
        {"anchor": "component TenantAStore", "replacement": "component Renamed"},
        {"anchor": 'isolate kv in realm("tenant_a")', "replacement": ""}]}})
    assert proposed["ok"] is False, proposed
    _assert_refused_by_name(proposed, tenants, "Renamed", "TenantAStore",
                            "tenant_a")
    committed = _call("revl_change", {"commit": True})
    assert not committed.get("swapped"), committed
    assert _placements()["TenantAStore"] == {"kv": "tenant_a"}
    snoop = _call("revl_change", {"add": {"source": SNOOP}, "commit": True})
    assert not snoop.get("swapped"), snoop


def test_trusted_operator_may_rename_and_drop(vault):
    server_mod.set_authoring_trust(host_code=True)
    assert server_mod.AUTHORING.profile() is None
    result = _call("revl_edit", {"target": str(vault),
                                 "edits": _rename_and_drop(vault)})
    assert result.get("swapped") is True, result
    assert _placements() == {"Vault2": {}, "App": {}}


def test_untrusted_rename_that_keeps_the_realm_is_the_placement_refusal(vault):
    """Renaming the provider while keeping its clause moves nothing, so this
    check stays silent; the renamed component is new text that names a realm,
    which the placement check refuses as before. An untrusted author has no
    admitted way to rename a realm-placed provider."""
    result = _call("revl_edit", {"target": str(vault), "edits": [
        {"anchor": "component Vault provides", "replacement":
         "component Vault2 provides"}]})
    assert result.get("swapped") is not True, result
    message = _messages(result)
    assert "forbids naming a realm" in message, message
    assert REFUSAL not in message, message
    assert _placements() == {"Vault": {"kv": "billing"}, "App": {"kv": "billing"}}


def test_provider_withdrawn_with_its_key_is_admitted(tenants):
    """Withdrawing TenantAStore leaves `kv` provided only by TenantBStore,
    unchanged in tenant_b: the key is still provided, but not by any
    provision the candidate added, so nothing moved out of tenant_a."""
    proposed = _call("revl_change", {"withdraw": {"component": "TenantAStore",
                                                  "cascade": True}})
    assert proposed["ok"] is True, proposed
    committed = _call("revl_change", {"commit": True})
    assert committed.get("swapped") is True, committed
    assert _placements() == {"TenantBStore": {"kv": "tenant_b"},
                             "TenantBApp": {"kv": "tenant_b"}}


def test_key_rename_that_leaves_the_realm_is_refused_by_the_shrink_check(vault):
    """Renaming the key withdraws `kv`, so no provision of it moved and the
    per-key walks compare nothing. Under the untrusted profile a component may
    not leave a realm by key rename, because realm-scoped reach rules select
    a component by its realm set (policy `component_realms`): `App` and
    `Vault` both end in the shared realm. This is the #2586 decision, which
    reverses the earlier admission of this edit and can be reversed at review.
    The check stops at the first component, `App`, and the per-key refusal
    text is absent."""
    renamed = KV + STORE % ("Vault", "kv2", "", "kv2") + \
        APP % ("kv2", "", "kv2", "kv2")
    result = _call("revl_edit", {"target": str(vault), "edits": [
        {"anchor": BODY, "replacement": renamed[len(KV):]}]})
    assert result.get("swapped") is not True, result
    message = _messages(result)
    assert "cannot shrink the realm set of `App`" in message, message
    assert REFUSAL not in message, message
    assert _placements() == {"Vault": {"kv": "billing"}, "App": {"kv": "billing"}}


def test_key_rename_a_realm_consumer_still_requires_is_not_admitted(vault):
    """A renamed key that the billing consumer still requires is not the
    per-key refusal: binding is per (key, realm), so the consumer's `kv` in
    billing would be left unbound. The realm-set check (#2586) refuses it
    first, since `Vault` leaves billing; the assertions hold either way:
    nothing swaps in, the refusal text is not the placement one, and the
    running placement is unchanged."""
    renamed = STORE % ("Vault", "kv2", "", "kv2") + APP % ("kv", CLAUSE, "kv", "kv")
    result = _call("revl_edit", {"target": str(vault), "edits": [
        {"anchor": BODY, "replacement": renamed}]})
    assert result.get("swapped") is not True, result
    assert REFUSAL not in _messages(result), result
    assert _placements() == {"Vault": {"kv": "billing"}, "App": {"kv": "billing"}}
