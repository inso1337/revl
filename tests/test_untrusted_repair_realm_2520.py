"""An untrusted repair may not delete a realm placement (issue #2520).

`revl_repair` swaps its candidate in through `SwapRemediation`, which calls
`session.swap` directly. Before this fix that door skipped the realm-removal
check `revl_edit` / `revl_change` run (issue #2487), so a candidate that leaves
out a provider's `isolate kv in realm("tenant_a")` clause moved the provision
into the shared realm and came back `repaired`. The provider here has no realm
consumer, so no binding check (G2) stands in the way.

The repair is refused under G9 for the untrusted author, with the same message
`revl_change` gives, pointing at the provider in the candidate text. Controls:
a trusted operator's repair with the same candidate is admitted, an untrusted
repair that keeps the realm (through the placeholder the operator bound) is
admitted, and the operator's own source is admitted: jailed `files` alone, and
the CLI's repair (`over_the_transport=False`).
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import repair as repair_mod  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the repair loop boots a composition and needs the cordis-py "
           "runtime: install it with `sh backends/python/setup.sh`")

CLAUSE = 'isolate kv in realm("tenant_a")'
REFUSAL = "cannot remove the realm placement of `Vault`"

VAULT = '''service Kv {
  fn get(k: Str) -> Opt[Str]
  fn set(k: Str, v: Str)
  fn unset(k: Str)
}

component Vault provides kv: Kv {
  isolate kv in realm("tenant_a")
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
'''
WITHOUT_CLAUSE = VAULT.replace("  " + CLAUSE + "\n", "")

POLICY = {"eligible": [{"component": "*"}], "mayTouch": ["*"],
          "ackOnWiden": False}


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _repair_args(candidate: dict, **extra) -> dict:
    return {"component": "Vault", "candidate": candidate,
            "selfRepairPolicy": POLICY, **extra}


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
    path = clean_session / "vault.rvl"
    path.write_text(VAULT)
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True
    assert _realms("Vault") == {"kv": "tenant_a"}
    return path


def _assert_refused(result: dict) -> None:
    assert result["incident"]["status"] == repair_mod.STATUS_REJECTED, result
    assert result["incident"]["swapped"] is False, result
    assert REFUSAL in _messages(result), result
    assert any(d.get("code") == "G9" for d in result["diagnostics"]), result
    assert _realms("Vault") == {"kv": "tenant_a"}


def test_untrusted_repair_that_leaves_the_clause_out_is_refused(loaded):
    result = _call("revl_repair", _repair_args({"source": WITHOUT_CLAUSE}))
    _assert_refused(result)
    assert result["remediation"]["applied"] is False, result
    assert result["remediation"]["blockedBy"] == "realm", result


def test_repair_refusal_names_the_component_file_and_line(loaded):
    result = _call("revl_repair", _repair_args({"source": WITHOUT_CLAUSE}))
    line = WITHOUT_CLAUSE.splitlines().index(
        "component Vault provides kv: Kv {") + 1
    refusal = next(d for d in result["diagnostics"] if REFUSAL in d["message"])
    assert refusal.get("file") == "<candidate>.rvl", refusal
    assert refusal.get("line") == line, refusal


def test_untrusted_repair_carried_in_modules_is_refused(loaded):
    """A jailed path whose text rides in `modules` is the author's text, not
    the operator's file: it is checked like inline source."""
    result = _call("revl_repair", _repair_args(
        {"files": [str(loaded)], "modules": {str(loaded): WITHOUT_CLAUSE}}))
    _assert_refused(result)


def test_untrusted_rehearsal_is_refused_too(loaded):
    """`apply: false` must not report every gate green for the same move."""
    result = _call("revl_repair", _repair_args({"source": WITHOUT_CLAUSE},
                                               apply=False))
    _assert_refused(result)


def test_trusted_repair_that_leaves_the_clause_out_is_admitted(loaded):
    server_mod.set_authoring_trust(host_code=True)
    result = _call("revl_repair", _repair_args({"source": WITHOUT_CLAUSE}))
    assert result["incident"]["status"] == repair_mod.STATUS_REPAIRED, result
    assert _realms("Vault") == {}


def test_operator_cli_repair_that_leaves_the_clause_out_is_admitted(loaded):
    """`revl repair` from the CLI compiles the operator's own source."""
    result = repair_mod.run_repair(server_mod.SESSION,
                                   _repair_args({"source": WITHOUT_CLAUSE}),
                                   over_the_transport=False)
    assert result["incident"]["status"] == repair_mod.STATUS_REPAIRED, result
    assert _realms("Vault") == {}


def test_untrusted_repair_that_keeps_the_realm_is_admitted(loaded):
    """An untrusted author may not name a realm, but may write the placeholder
    the operator bound to the same realm: the provision stays where it was."""
    server_mod.set_authoring_trust(realm_bindings=(("tenant", "tenant_a"),))
    kept = VAULT.replace(CLAUSE, "isolate kv in realm(?tenant)")
    result = _call("revl_repair", _repair_args({"source": kept}))
    assert result["incident"]["status"] == repair_mod.STATUS_REPAIRED, result
    assert _realms("Vault") == {"kv": "tenant_a"}


def test_repair_from_jailed_files_alone_is_admitted(loaded):
    """Jailed files are the operator's own: a file the operator changed on
    disk to drop the clause repairs in, as `revl_swap` admits it."""
    loaded.write_text(WITHOUT_CLAUSE)
    result = _call("revl_repair", _repair_args({"files": [str(loaded)]}))
    assert result["incident"]["status"] == repair_mod.STATUS_REPAIRED, result
    assert _realms("Vault") == {}
