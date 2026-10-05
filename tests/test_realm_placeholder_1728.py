"""A realm placeholder an untrusted author writes and the operator binds
(issue #1728, docs/design/1728-realm-placeholder.md).

The untrusted-author profile refuses naming a realm (G9): a realm is an
authority address, and an author that picks its own picks which of the
operator's standing approvals cover its crossings. Measured on main
`5c3d21bf0`, that left an agent no way to author realm-scoped code at all:
`isolate kv in realm("tenant_a")` is refused G9, and `realm(?tenant)` did not
parse ("dynamic realm labels are not supported").

`isolate <key> in realm(?<name>)` is now the realm form an untrusted author may
write. The operator binds `<name>` to a realm: `--bind-realm NAME=REALM` on
`revl compile` and `revl mcp serve`, the `realm_bindings` field of the
admission profile, or `Gate.propose(..., realm_bindings=)` for an embedder.
The binding is applied before lowering, so G2 is checked over the bound realm
like any other.

The issue's exit tests, each below:
  * a placeholder compiles and admits under the untrusted profile once bound;
  * the same source with a literal realm is still refused G9;
  * two placeholders bound to the same realm are checked for G2;
  * an unbound placeholder refuses admission by name.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.admit_profile import AdmissionProfile  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

# examples/tenants.rvl with each realm literal replaced by a placeholder
TENANTS = (ROOT / "examples" / "tenants.rvl").read_text(encoding="utf-8")
PLACEHOLDERS = (TENANTS.replace('realm("tenant_a")', "realm(?a)")
                .replace('realm("tenant_b")', "realm(?b)"))

BOTH = (("a", "tenant_a"), ("b", "tenant_b"))


def _untrusted(bindings=()):
    return AdmissionProfile.untrusted_author((), bindings)


def _isolation(ir: dict) -> dict:
    return {c["name"]: c.get("isolate") for c in ir["manifest"]["components"]}


def test_the_fixture_has_placeholders_and_no_realm_literal():
    assert "realm(?a)" in PLACEHOLDERS and "realm(?b)" in PLACEHOLDERS
    assert 'realm("' not in PLACEHOLDERS


# ---------------------------------------------------------------------------
# the exit tests
# ---------------------------------------------------------------------------

def test_a_bound_placeholder_compiles_and_admits_for_an_untrusted_author():
    ir = compile_source(PLACEHOLDERS, "tenants.rvl", profile=_untrusted(BOTH))
    assert _isolation(ir) == _isolation(compile_source(TENANTS, "tenants.rvl"))
    assert _isolation(ir)["TenantAStore"] == {"kv": "tenant_a"}
    assert _isolation(ir)["TenantBApp"] == {"kv": "tenant_b"}


def test_a_literal_realm_is_still_refused_g9():
    with pytest.raises(RevlError) as caught:
        compile_source(TENANTS, "tenants.rvl", profile=_untrusted(BOTH))
    assert caught.value.code == "G9"
    assert "forbids naming a realm" in caught.value.message


def test_two_placeholders_bound_to_one_realm_are_checked_for_g2():
    one = (("a", "tenant_a"), ("b", "tenant_a"))
    with pytest.raises(RevlError) as caught:
        compile_source(PLACEHOLDERS, "tenants.rvl", profile=_untrusted(one))
    assert "provision conflict" in caught.value.message
    assert "`kv`" in caught.value.message and "tenant_a" in caught.value.message


@pytest.mark.parametrize("who", ["trusted", "untrusted-half-bound"])
def test_an_unbound_placeholder_refuses_by_name(who):
    profile = None if who == "trusted" else _untrusted((("a", "tenant_a"),))
    with pytest.raises(RevlError) as caught:
        compile_source(PLACEHOLDERS, "tenants.rvl", profile=profile)
    message = caught.value.message
    name = "?a" if profile is None else "?b"
    assert message.startswith(f"realm placeholder `{name}`"), message
    assert "is not bound" in message
    assert caught.value.code == "G2"


# ---------------------------------------------------------------------------
# where the binding comes from
# ---------------------------------------------------------------------------

def test_admission_against_a_running_composition_binds_too():
    """The untrusted per-turn shape: the candidate compiled against a running
    manifest, through `compile_files`."""
    running = compile_source(
        "service Ping { fn ping() -> Int }\n"
        "component Base provides ping: Ping { provide ping { fn ping() = 1 } }\n",
        "base.rvl")
    candidate = (
        "service KV { fn get(k: Str) -> Str }\n"
        "component Store provides kv: KV {\n"
        "  isolate kv in realm(?tenant)\n"
        "  provide kv { fn get(k) = k }\n"
        "}\n")
    ir = compile_source(candidate, "turn.rvl", manifest=running,
                        profile=_untrusted((("tenant", "tenant_a"),)))
    assert _isolation(ir)["Store"] == {"kv": "tenant_a"}


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


def test_the_mcp_server_binds_with_the_operators_flag(server):
    server.set_authoring_trust(host_code=False, realm_bindings=BOTH)
    checked = _call("revl_check", {"source": PLACEHOLDERS})
    assert checked["ok"] is True, checked
    literal = _call("revl_check", {"source": TENANTS})
    assert literal["ok"] is False
    assert literal["diagnostics"][0]["code"] == "G9"


def test_the_agent_cannot_bind_through_the_call(server):
    """A binding argument from the far end of the transport is not read: only
    the operator's flag binds."""
    server.set_authoring_trust(host_code=False, realm_bindings=())
    checked = _call("revl_check", {"source": PLACEHOLDERS,
                                   "realm_bindings": {"a": "tenant_a", "b": "tenant_b"},
                                   "realmBindings": {"a": "tenant_a", "b": "tenant_b"}})
    assert checked["ok"] is False
    assert checked["diagnostics"][0]["message"].startswith("realm placeholder `?a`")


_CANDIDATE = (
    "service KV { fn get(k: Str) -> Str }\n"
    "component Store provides kv: KV {\n"
    "  isolate kv in realm(?tenant)\n"
    "  provide kv { fn get(k) = k }\n"
    "}\n")


def test_gate_propose_refuses_an_unbound_placeholder():
    """The decision compile, with no runtime: what `propose` admits under."""
    from types import SimpleNamespace

    from revl.gate import Gate
    gate = Gate.__new__(Gate)
    gate._loaded = True
    gate._session = SimpleNamespace(halted=False)
    refused = gate.propose(_CANDIDATE)
    assert refused.admitted is False
    assert "realm placeholder `?tenant`" in refused.message


needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the swap half of propose needs the cordis-py runtime")


@needs_cordis
def test_gate_propose_binds_with_the_operators_map():
    from revl.gate import Gate
    gate = Gate()
    gate.load("service Ping { fn ping() -> Int }\n"
              "component Base provides ping: Ping { provide ping { fn ping() = 1 } }\n")
    bound = gate.propose(_CANDIDATE, realm_bindings={"tenant": "tenant_a"})
    assert bound.admitted is True, bound.message


def test_revl_compile_takes_the_flag(tmp_path):
    src = tmp_path / "tenants.rvl"
    src.write_text(PLACEHOLDERS, encoding="utf-8")
    base = [sys.executable, "-m", "revl", "compile", str(src)]
    refused = subprocess.run(base, capture_output=True, text=True, cwd=tmp_path)
    assert refused.returncode == 1 and "realm placeholder `?a`" in refused.stderr
    bound = subprocess.run(base + ["--bind-realm", "a=tenant_a",
                                   "--bind-realm", "b=tenant_b"],
                           capture_output=True, text=True, cwd=tmp_path)
    assert bound.returncode == 0, bound.stderr
    assert _isolation(json.loads(bound.stdout))["TenantAStore"] == {"kv": "tenant_a"}
    bad = subprocess.run(base + ["--bind-realm", "a"], capture_output=True,
                         text=True, cwd=tmp_path)
    assert bad.returncode == 2 and "NAME=REALM" in bad.stderr
