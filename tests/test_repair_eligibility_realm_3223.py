"""Self-repair eligibility reads the candidate's realms too (issue #3223).

`revl_repair` decided whether a component may repair itself unattended from the
realms of the RUNNING manifest alone. With `eligible: [{realm: edge}]` a
component running in `edge` passed, and a candidate that re-declared it in
`payments` swapped in: the component left the realm that made it eligible, and
only the boundary policy and the widening gate stood between the two.

The repair is now refused as ineligible when the candidate's own realms are not
selected by the policy, or when it adds a realm no rule selects. Trusted
authoring (`over_the_transport=False`, the CLI's `revl repair`) and a trusted
operator serve are covered the same way, since `_realm_removal_refusal` only
guards untrusted text. Controls: a candidate that stays in `edge` is repaired,
a candidate that moves to another realm a glob rule selects is repaired, and a
component rule is not tied to a realm.
"""

import importlib.util
import json
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

VAULT = '''service Kv {
  fn get(k: Str) -> Opt[Str]
  fn set(k: Str, v: Str)
  fn unset(k: Str)
}

component Vault provides kv: Kv {
  isolate kv in realm("edge")
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
TO_PAYMENTS = VAULT.replace('realm("edge")', 'realm("payments")')
TO_EDGE2 = VAULT.replace('realm("edge")', 'realm("edge2")')
NO_PLACEMENT = VAULT.replace('  isolate kv in realm("edge")\n', "")
CHANGED_BODY = VAULT.replace("    fn get(k) = store.get(k)",
                             "    // repaired\n    fn get(k) = store.get(k)")
TWO_PLACEMENTS = '''service Kv {
  fn get(k: Str) -> Opt[Str]
}

component Vault provides kv: Kv, lg: Kv {
  isolate kv in realm("edge")
  isolate lg in realm("edge")
  provide kv {
    fn get(k) = None
  }
  provide lg {
    fn get(k) = None
  }
}
'''
ADDS_PAYMENTS = TWO_PLACEMENTS.replace('isolate lg in realm("edge")',
                                       'isolate lg in realm("payments")')
GLOB_POLICY = {"eligible": [{"realm": "edge*"}], "mayTouch": ["*"],
               "ackOnWiden": False}

EDGE_POLICY = {"eligible": [{"realm": "edge"}], "mayTouch": ["*"],
               "ackOnWiden": False}
COMPONENT_POLICY = {"eligible": [{"component": "Vault"}], "mayTouch": ["*"],
                    "ackOnWiden": False}


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _realms() -> dict:
    for comp in server_mod.SESSION.ir["components"]:
        if comp["name"] == "Vault":
            return dict(comp.get("isolate") or {})
    raise AssertionError("Vault is not running")


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
    assert _realms() == {"kv": "edge"}
    return path


def _run(candidate_text: str, policy: dict, *, trusted: bool, **extra) -> dict:
    """`trusted` is the CLI's repair (no transport); otherwise a trusted
    operator's serve: over the transport, host code trusted."""
    arguments = {"component": "Vault", "candidate": {"source": candidate_text},
                 "selfRepairPolicy": policy, **extra}
    if trusted:
        return repair_mod.run_repair(server_mod.SESSION, arguments,
                                     over_the_transport=False)
    server_mod.set_authoring_trust(host_code=True, roots=(str(Path.cwd()),))
    return _call("revl_repair", arguments)


@pytest.mark.parametrize("trusted", [True, False], ids=["cli", "transport"])
def test_candidate_that_moves_the_component_out_of_the_realm_is_refused(
        loaded, trusted):
    result = _run(TO_PAYMENTS, EDGE_POLICY, trusted=trusted)
    assert result["incident"]["status"] == repair_mod.STATUS_INELIGIBLE, result
    assert result["incident"]["swapped"] is False, result
    assert _realms() == {"kv": "edge"}
    assert result["verdicts"]["eligibility"]["eligible"] is False, result
    assert result["verdicts"]["eligibility"]["realms"] == ["payments"], result
    assert result["verdicts"]["eligibility"]["unselectedRealms"] == [
        "payments"], result
    assert result["remediation"]["blockedBy"] == "eligibility", result


def test_refusal_dossier_does_not_credit_the_running_rule(loaded):
    result = _run(TO_PAYMENTS, EDGE_POLICY, trusted=True)
    eligibility = result["eligibility"]
    assert eligibility["eligible"] is False, eligibility
    assert eligibility["by"] is None, eligibility
    assert eligibility["byRunning"] == {"scope": "realm",
                                        "selector": "edge"}, eligibility
    assert eligibility["candidate"]["realms"] == ["payments"], eligibility
    assert not result["authority"].get("eligibleBy"), result["authority"]
    assert "realm edge" not in json.dumps(result["authority"]), result


def test_candidate_that_drops_its_placement_is_refused(loaded):
    result = _run(NO_PLACEMENT, EDGE_POLICY, trusted=True)
    assert result["incident"]["status"] == repair_mod.STATUS_INELIGIBLE, result
    assert result["verdicts"]["eligibility"]["realms"] == [], result
    assert _realms() == {"kv": "edge"}


@pytest.mark.parametrize("trusted", [True, False], ids=["cli", "transport"])
def test_candidate_that_stays_in_the_realm_is_repaired(loaded, trusted):
    result = _run(CHANGED_BODY, EDGE_POLICY, trusted=trusted)
    assert result["incident"]["status"] == repair_mod.STATUS_REPAIRED, result
    assert result["eligibility"]["candidate"]["eligible"] is True, result
    assert _realms() == {"kv": "edge"}


def test_candidate_moving_to_another_realm_a_glob_selects_is_repaired(loaded):
    result = _run(TO_EDGE2, GLOB_POLICY, trusted=True)
    assert result["incident"]["status"] == repair_mod.STATUS_REPAIRED, result
    assert _realms() == {"kv": "edge2"}


def test_component_rule_does_not_depend_on_the_realm(loaded):
    result = _run(TO_PAYMENTS, COMPONENT_POLICY, trusted=True)
    assert result["incident"]["status"] == repair_mod.STATUS_REPAIRED, result
    assert _realms() == {"kv": "payments"}


@pytest.mark.parametrize("trusted", [True, False], ids=["cli", "transport"])
def test_candidate_keeping_edge_that_adds_an_unselected_realm_is_refused(
        clean_session, trusted):
    path = clean_session / "vault.rvl"
    path.write_text(TWO_PLACEMENTS)
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True
    assert _realms() == {"kv": "edge", "lg": "edge"}
    result = _run(ADDS_PAYMENTS, EDGE_POLICY, trusted=trusted)
    assert result["incident"]["status"] == repair_mod.STATUS_INELIGIBLE, result
    assert result["incident"]["swapped"] is False, result
    verdict = result["verdicts"]["eligibility"]
    assert verdict["realms"] == ["edge", "payments"], verdict
    assert verdict["unselectedRealms"] == ["payments"], verdict
    assert verdict["eligible"] is False and verdict["by"] is None, verdict
    assert result["eligibility"]["candidate"]["by"] is None, result
    assert _realms() == {"kv": "edge", "lg": "edge"}
