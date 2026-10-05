"""Realm placeholders inside a `realms(...)` route (issue #1728, the route
half; docs/design/1728-realm-placeholder.md).

#1773 let an untrusted author write `isolate kv in realm(?a)` and the operator
bind `?a`. A route, `isolate kv in realms(...)`, still took literals only, so an
author could not write the consumption side of a multi-tenant or sharded
composition: the literal is refused G9, and a placeholder did not parse.

Each entry of the route may now be `?<name>`, bound by the same operator-only
binding and refused by the same rules:
  * bound, the route compiles under the untrusted profile to the routes the
    literal source has;
  * a route naming any literal realm is still refused G9;
  * an unbound leg is refused by name, code G2;
  * two legs bound to one realm are refused G2, and a leg bound to a realm with
    no provider is refused by the item-162 per-realm check;
  * the agent cannot bind one through a call.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.admit_profile import AdmissionProfile  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.gate import Gate  # noqa: E402

SHARDS = """service Kv { fn get(k: Str) -> Opt[Str] }
service Api { fn read(k: Str) -> Opt[Str] }
component ShardA provides kv: Kv {
  isolate kv in realm(?a)
  provide kv { fn get(k) = None }
}
component ShardB provides kv: Kv {
  isolate kv in realm(?b)
  provide kv { fn get(k) = None }
}
component Front requires kv: Kv provides api: Api {
  isolate kv in realms(?x, ?y) strategy(round_robin)
  provide api { fn read(k) = kv.get(k) }
}
"""
LITERAL = (SHARDS.replace("realm(?a)", 'realm("w1")').replace("realm(?b)", 'realm("w2")')
           .replace("realms(?x, ?y)", 'realms("w1", "w2")'))
BOUND = (("a", "w1"), ("b", "w2"), ("x", "w1"), ("y", "w2"))


def _untrusted(bindings=()):
    return AdmissionProfile.untrusted_author((), bindings)


def _routes(ir: dict) -> dict:
    return {c["name"]: c["routes"] for c in ir["components"] if c.get("routes")}


def _refusal(source: str, bindings) -> RevlError:
    with pytest.raises(RevlError) as caught:
        compile_source(source, "shards.rvl", profile=_untrusted(bindings))
    return caught.value


def test_a_bound_route_compiles_for_an_untrusted_author_like_the_literal():
    ir = compile_source(SHARDS, "shards.rvl", profile=_untrusted(BOUND))
    assert _routes(ir) == _routes(compile_source(LITERAL, "shards.rvl"))
    assert _routes(ir)["Front"]["kv"] == {"realms": ["w1", "w2"],
                                          "strategy": "round_robin"}


def test_the_binding_decides_the_order_of_the_legs():
    swapped = (("a", "w1"), ("b", "w2"), ("x", "w2"), ("y", "w1"))
    ir = compile_source(SHARDS, "shards.rvl", profile=_untrusted(swapped))
    assert _routes(ir)["Front"]["kv"]["realms"] == ["w2", "w1"]


@pytest.mark.parametrize("route", ['realms("w1", "w2")', 'realms(?x, "w2")'])
def test_a_route_naming_any_literal_realm_is_still_refused_g9(route):
    error = _refusal(SHARDS.replace("realms(?x, ?y)", route), BOUND)
    assert error.code == "G9"
    assert "forbids naming a realm" in error.message


def test_an_unbound_leg_is_refused_by_name():
    error = _refusal(SHARDS, (("a", "w1"), ("b", "w2"), ("x", "w1")))
    assert error.code == "G2"
    assert error.message.startswith(
        "realm placeholder `?y` (on `isolate kv in realms(...)`) is not bound")


def test_an_unbound_leg_is_refused_in_a_trusted_compile_too():
    with pytest.raises(RevlError) as caught:
        compile_source(SHARDS.replace("realm(?a)", 'realm("w1")')
                       .replace("realm(?b)", 'realm("w2")'), "shards.rvl")
    assert caught.value.code == "G2"
    assert caught.value.message.startswith("realm placeholder `?x`")


@pytest.mark.parametrize("route", ["realms(?x, ?y)", "realms(?x, ?x)"])
def test_two_legs_bound_to_one_realm_are_refused_g2(route):
    one = (("a", "w1"), ("b", "w2"), ("x", "w1"), ("y", "w1"))
    error = _refusal(SHARDS.replace("realms(?x, ?y)", route), one)
    assert error.code == "G2"
    assert "routes to realm `w1` twice" in error.message


def test_a_leg_bound_to_a_realm_with_no_provider_is_refused():
    dangling = (("a", "w1"), ("b", "w2"), ("x", "w1"), ("y", "w3"))
    error = _refusal(SHARDS, dangling)
    assert "names realm `w3`, but no component provides `kv` in realm `w3`" \
        in error.message


def test_a_malformed_leg_is_a_parse_refusal():
    with pytest.raises(RevlError, match="a realm placeholder is `\\?` followed by a name"):
        compile_source(SHARDS.replace("realms(?x, ?y)", "realms(?x, ?)"), "shards.rvl")


def test_gate_propose_refuses_an_unbound_leg():
    from types import SimpleNamespace

    gate = Gate.__new__(Gate)
    gate._loaded = True
    gate._session = SimpleNamespace(halted=False)
    refused = gate.propose(SHARDS, realm_bindings={"a": "w1", "b": "w2"})
    assert refused.admitted is False
    assert "realm placeholder `?x`" in refused.message


def _call(tool: str, arguments: dict) -> dict:
    from revl.mcp.server import handle
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture
def server(monkeypatch):
    from revl.mcp import server as srv
    from revl.mcp.session import Session
    before = srv.AUTHORING
    monkeypatch.setattr(srv, "SESSION", Session())
    yield srv
    srv.AUTHORING = before


def test_the_operators_flag_binds_a_route_over_mcp(server):
    server.set_authoring_trust(host_code=False, realm_bindings=BOUND)
    checked = _call("revl_check", {"source": SHARDS})
    assert checked["ok"] is True, checked


def test_the_agent_cannot_bind_a_leg_through_the_call(server):
    server.set_authoring_trust(host_code=False, realm_bindings=BOUND[:2])
    checked = _call("revl_check", {"source": SHARDS,
                                   "realm_bindings": dict(BOUND),
                                   "realmBindings": dict(BOUND)})
    assert checked["ok"] is False
    assert checked["diagnostics"][0]["message"].startswith("realm placeholder `?x`")
