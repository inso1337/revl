"""An untrusted swap may not delete a requirer's realm placement (issue #2587).

Issue #2585 refuses an untrusted edit that deletes the `isolate kv in
realm("tenant_a")` clause of a component that REQUIRES `kv`, and its tests
drive the edit door. `revl_swap` runs the same check, and this module drives
it there.

What the swap door compares: the running generation against the candidate
compiled as a complete composition on its own. That compiled candidate is the
whole program `Session.swap` installs, so it is the program as it runs after
the swap, and nothing of the running one is merged into it. A component the
candidate leaves out is withdrawn by the swap and takes its placements with
it; a component it carries is compared like an edited one.

An untrusted author may not name a realm, so a full candidate carries each
realm through the placeholder the operator bound to it (`realm(?tenant_a)`),
which is the only realm form the authoring profile admits.

The candidate keeps every component, adds a shared-realm provider of `kv` (so
the requirer would bind there), and leaves out TenantAApp's clause: refused by
name. Controls: the same swap with the clause kept is admitted and swaps in, a
trusted (operator) author may drop the clause, and a partial candidate
withdraws the components it leaves out rather than moving them.
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
    reason="a swap boots a composition and needs the cordis-py runtime: "
           "install it with `sh backends/python/setup.sh`")

REFUSAL = "cannot remove the realm placement"
TENANTS = (ROOT / "examples" / "tenants.rvl").read_text(encoding="utf-8")
# the operator binds each placeholder to the realm the running file names
BINDINGS = (("tenant_a", "tenant_a"), ("tenant_b", "tenant_b"))
APP_HEADER = "component TenantAApp requires kv: Kv {"
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


def _running() -> set:
    return {comp["name"] for comp in server_mod.SESSION.ir["components"]}


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
    server_mod.set_authoring_trust(roots=(str(tmp_path),),
                                   realm_bindings=BINDINGS)
    yield tmp_path
    if session.loaded:
        session.unload()
    session.proposals = {}
    session.pending_draft = None
    session.draft = None
    server_mod.AUTHORING = old


@pytest.fixture
def loaded(clean_session):
    """The operator's tenants composition, loaded from a jailed file."""
    path = clean_session / "tenants.rvl"
    shutil.copy(ROOT / "examples" / "tenants.rvl", path)
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True
    assert _realms("TenantAApp") == {"kv": "tenant_a"}
    return path


def _as_untrusted_author(text: str) -> str:
    """The same text with each realm spelled as the placeholder bound to it."""
    return (text.replace('realm("tenant_a")', "realm(?tenant_a)")
                .replace('realm("tenant_b")', "realm(?tenant_b)"))


def _full_candidate(*, keep_app_clause: bool) -> str:
    """Every running component, a shared-realm provider of `kv` added, and
    TenantAApp's clause kept or left out, with the realms as written."""
    text = TENANTS
    if not keep_app_clause:
        text = text.replace(APP_HEADER + "\n" + APP_CLAUSE, APP_HEADER + "\n")
        assert APP_HEADER + "\n" + APP_CLAUSE not in text
    anchor = "component TenantBStore provides kv: Kv {"
    return text.replace(anchor, SHARED_STORE + anchor)


def _tenant_b_only() -> str:
    """`Kv` and the TenantB half, as the untrusted author writes it."""
    head = TENANTS[TENANTS.index("service Kv"):TENANTS.index("component TenantAStore")]
    store_b = TENANTS[TENANTS.index("component TenantBStore"):
                      TENANTS.index("component TenantAApp")]
    app_b = TENANTS[TENANTS.index("component TenantBApp"):]
    return _as_untrusted_author(head + store_b + app_b)


def _line_of(text: str, header: str) -> int:
    return text.splitlines().index(header) + 1


def test_untrusted_full_swap_dropping_the_requirers_clause_is_refused(loaded):
    assert server_mod.AUTHORING.profile() is not None  # the untrusted author
    candidate = _as_untrusted_author(_full_candidate(keep_app_clause=False))
    swapped = _call("revl_swap", {"source": candidate,
                                  "replacing": ["TenantAApp"]})
    assert swapped["ok"] is False, swapped
    assert swapped.get("admitted") is False, swapped
    assert swapped.get("swapped") is False, swapped
    message = _messages(swapped)
    assert "forbids naming a realm" not in message, message
    assert REFUSAL in message and "`TenantAApp`" in message, message
    assert "tenant_a" in message and "`kv` requirement" in message, message
    refusal = next(d for d in swapped["diagnostics"] if REFUSAL in d["message"])
    assert refusal.get("code") == "G9", refusal
    # it points at the requirer, in the candidate text sent
    assert refusal.get("file") == "<candidate>.rvl", refusal
    assert refusal.get("line") == _line_of(candidate, APP_HEADER), refusal
    assert _realms("TenantAApp") == {"kv": "tenant_a"}
    assert "SharedStore" not in _running()


def test_untrusted_full_swap_that_keeps_the_clause_is_admitted(loaded):
    candidate = _as_untrusted_author(_full_candidate(keep_app_clause=True))
    swapped = _call("revl_swap", {"source": candidate,
                                  "replacing": ["TenantAApp"]})
    assert swapped["ok"] is True, swapped
    assert swapped.get("swapped") is True, swapped
    assert _realms("TenantAApp") == {"kv": "tenant_a"}
    assert _realms("TenantAStore") == {"kv": "tenant_a"}
    assert "SharedStore" in _running()


def test_trusted_swap_may_drop_the_requirers_clause(loaded):
    server_mod.set_authoring_trust(host_code=True)
    assert server_mod.AUTHORING.profile() is None
    swapped = _call("revl_swap", {"source": _full_candidate(keep_app_clause=False),
                                  "replacing": ["TenantAApp"]})
    assert swapped["ok"] is True, swapped
    assert swapped.get("swapped") is True, swapped
    assert _realms("TenantAApp") == {}


def test_untrusted_partial_swap_withdraws_what_it_leaves_out(loaded):
    """The compared generation is the one that runs after the swap: the
    TenantA half the candidate leaves out stops running, so its placements
    are withdrawn with it, not moved, and the swap is admitted."""
    swapped = _call("revl_swap", {"source": _tenant_b_only(),
                                  "replacing": ["TenantAStore", "TenantAApp"]})
    assert swapped["ok"] is True, swapped
    assert swapped.get("swapped") is True, swapped
    assert _running() == {"TenantBStore", "TenantBApp"}
    assert _realms("TenantBApp") == {"kv": "tenant_b"}
