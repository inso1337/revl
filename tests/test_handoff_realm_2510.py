"""Issue #2510: a provider's `handoff` state is matched to its successor by
(provided key, placement realm), never by the key alone.

Before the fix the swap path captured and restored hand-off state keyed by the
provided key only, so a successor in another realm that re-provided the key
and declared `handoff` inherited a realm-scoped store. Both of the issue's
reproductions ran as the untrusted author over the MCP session; the first
now runs as the operator, since the untrusted author's rename that drops the
clause is refused at admission (issue #2523). Both run on
examples/tenants.rvl with `handoff kv: Map[Str, Str]` added to TenantAStore.
A successor whose (key, realm) has no capture starts cold, and the swap
result's `handoff` block says so by name.

These drive a live composition, so they need the cordis-py runtime.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp.server import handle  # noqa: E402
from revl.mcp.session import Session  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a swap drives a live composition; install the runtime with "
           "`sh backends/python/setup.sh`")

ISOLATE_A = 'isolate kv in realm("tenant_a")'
HANDOFF = "  handoff kv: Map[Str, Str]"
A_LABEL = 'kv in realm("tenant_a")'


def _tenants_with_handoff() -> str:
    text = (ROOT / "examples" / "tenants.rvl").read_text(encoding="utf-8")
    assert text.count(ISOLATE_A) == 2  # TenantAStore's, then TenantAApp's
    head, tail = text.split(ISOLATE_A, 1)
    return head + ISOLATE_A + "\n" + HANDOFF + tail


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(autouse=True)
def _fresh_server_session():
    from revl.mcp import server as server_mod

    server_mod.UNDO_STACK.clear()
    authoring = server_mod.AUTHORING
    yield
    server_mod.AUTHORING = authoring
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    server_mod.UNDO_STACK.clear()


def _map(component: str):
    """`component`'s live `Map`, read off its provider frame."""
    from revl.mcp import server as server_mod

    session = server_mod.SESSION
    fiber = session._driver.fibers.get(component)
    resources = session._frame_resources(fiber)
    assert len(resources) == 1, (component, resources)
    return resources[0]


def _store(component: str) -> dict:
    """The live entries of `component`'s `Map`."""
    return dict(_map(component).data)


# An entry only TenantAStore ever held. TenantAApp's seed (`who = alice`) is
# re-run by a rebooted consumer against whichever provider it then reaches, so
# it cannot tell inherited state from a fresh seed; this entry can.
SECRET = ("tenant_a_secret", "s3cr3t")


def _loaded(tmp_path: Path, monkeypatch) -> str:
    # the sanctioned path root is the working directory (no operator flags)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "tenants.rvl"
    target.write_text(_tenants_with_handoff(), encoding="utf-8")
    loaded = _call("revl_load", {"files": [str(target)]})
    assert loaded["ok"] is True, loaded
    assert _store("TenantAStore") == {"who": "alice"}
    _map("TenantAStore").insert(*SECRET)
    return str(target)


def _edit(target: str, edits: list) -> dict:
    changed = _call("revl_change", {
        "edit": {"target": target, "edits": edits}, "commit": True})
    return changed


LEAK = "\n".join([
    "component Leak provides kv: Kv {",
    "  handoff kv: Map[Str, Str]",
    "  let store = effect Map.new() undo store.drop()",
    "  provide kv {",
    "    fn get(k) = store.get(k)",
    "    fn set(k, v) {",
    "      effect store.insert(k, v)",
    "      undo   store.remove(k)",
    "    }",
    "    fn unset(k) = store.remove(k)",
    "  }",
    "}",
])


@needs_runtime
def test_reproduction_1_rename_dropping_isolate_starts_cold(tmp_path, monkeypatch):
    """The untrusted author may no longer rename the provider while dropping
    its clause (issue #2523), so this runs as the operator, who may: the
    hand-off is still matched by (key, realm), not by who wrote the edit."""
    from revl.mcp import server as server_mod

    server_mod.set_authoring_trust(host_code=True)
    target = _loaded(tmp_path, monkeypatch)
    changed = _edit(target, [
        {"anchor": "component TenantAStore", "replacement": "component Renamed"},
        {"anchor": ISOLATE_A, "replacement": ""}])
    assert changed["ok"] is True and changed.get("committed") is True, \
        [d.get("message") for d in changed.get("diagnostics") or []]
    # the shared-realm successor does not inherit tenant_a's store ...
    assert SECRET[0] not in _store("Renamed")
    # ... and the swap result says it started cold, and why
    entry = changed["handoff"]["kv"]
    assert entry["component"] == "Renamed"
    assert entry["migrated"] is False
    assert entry["reason"] == "realm"
    assert A_LABEL not in changed["handoff"]


@needs_runtime
def test_reproduction_2_shared_realm_leak_starts_cold(tmp_path, monkeypatch):
    _loaded(tmp_path, monkeypatch)
    changed = _call("revl_change", {"add": {"source": LEAK}, "commit": True})
    assert changed["ok"] is True and changed.get("committed") is True, \
        [d.get("message") for d in changed.get("diagnostics") or []]
    assert _store("Leak") == {}
    # TenantAStore still holds tenant_a's data, in tenant_a
    assert _store("TenantAStore")[SECRET[0]] == SECRET[1]
    handoff = changed["handoff"]
    assert handoff["kv"]["component"] == "Leak"
    assert handoff["kv"]["migrated"] is False
    assert handoff["kv"]["reason"] == "realm"
    assert handoff[A_LABEL]["component"] == "TenantAStore"
    assert handoff[A_LABEL]["migrated"] is True


@needs_runtime
def test_control_same_realm_successor_still_migrates(tmp_path, monkeypatch):
    """A successor that keeps the isolate clause names a realm, which the
    untrusted-author profile refuses (`no_realm_placement`), so this control
    runs as the operator (host code)."""
    from revl.mcp import server as server_mod

    server_mod.set_authoring_trust(host_code=True)
    _loaded(tmp_path, monkeypatch)
    source = _tenants_with_handoff()
    body = source[source.index("component TenantAStore"):
                  source.index("component TenantBStore")]
    changed = _call("revl_change", {"commit": True, "replace": {
        "component": "TenantAStore",
        "source": body.replace("fn get(k) = store.get(k)",
                               "fn get(key) = store.get(key)")}})
    assert changed["ok"] is True and changed.get("committed") is True, \
        [d.get("message") for d in changed.get("diagnostics") or []]
    assert _store("TenantAStore")[SECRET[0]] == SECRET[1]
    assert changed["handoff"] == {A_LABEL: {
        "component": "TenantAStore", "migrated": True, "resources": 1,
        "realm": "tenant_a"}}


@needs_runtime
def test_control_successor_without_handoff_starts_cold(tmp_path, monkeypatch):
    """As the operator, for the reason reproduction 1 gives (issue #2523)."""
    from revl.mcp import server as server_mod

    server_mod.set_authoring_trust(host_code=True)
    target = _loaded(tmp_path, monkeypatch)
    changed = _edit(target, [
        {"anchor": "component TenantAStore", "replacement": "component Renamed"},
        {"anchor": ISOLATE_A, "replacement": ""},
        {"anchor": HANDOFF, "replacement": ""}])
    assert changed["ok"] is True and changed.get("committed") is True, \
        [d.get("message") for d in changed.get("diagnostics") or []]
    assert SECRET[0] not in _store("Renamed")
    assert changed.get("handoff") is None


class _Resource:
    def __init__(self, state=None) -> None:
        self.state = state
        self.restored = None

    def __revl_state__(self):
        return self.state

    def __revl_restore__(self, snap) -> None:
        self.restored = snap


def test_control_capture_two_successors_could_claim_goes_to_neither():
    """Two successors on the same (key, realm) both declaring `handoff`: the
    capture is ambiguous, so neither inherits it and both are reported cold.
    Driven on the capture and restore steps directly: G2 keeps a linked
    composition from holding two such providers, so this is the
    defence-in-depth branch."""
    old, first, second = _Resource({"who": "alice"}), _Resource(), _Resource()
    stub = SimpleNamespace(
        _driver=SimpleNamespace(runtime=SimpleNamespace(
            StateIncompatible=RuntimeError),
            fibers={"Old": old, "One": first, "Two": second}),
        _frame_resources=lambda fiber: [fiber] if fiber is not None else [])
    comp = {"handoff": {"key": "kv", "type": "Map[Str, Str]"},
            "provides": {"kv": "Kv"}}
    pre = Session._capture_provider_state(
        stub, {"components": [dict(comp, name="Old")]})
    assert pre
    new_ir = {"components": [dict(comp, name="One"), dict(comp, name="Two")]}
    report = Session._restore_provider_state(stub, new_ir, pre)
    assert first.restored is None and second.restored is None
    assert report["kv"]["migrated"] is False
    assert report["kv"]["reason"] == "ambiguous"
    assert report["kv"]["component"] == ["One", "Two"]


def test_template_successor_does_not_claim_a_provider_capture():
    """Capture skips templates (F5); restore skips them the same way, so a
    template that declares the hand-off is not a successor."""
    old, tmpl = _Resource({"who": "alice"}), _Resource()
    stub = SimpleNamespace(
        _driver=SimpleNamespace(runtime=SimpleNamespace(
            StateIncompatible=RuntimeError), fibers={"Old": old, "Tmpl": tmpl}),
        _frame_resources=lambda fiber: [fiber] if fiber is not None else [])
    comp = {"handoff": {"key": "kv", "type": "Map[Str, Str]"},
            "provides": {"kv": "Kv"}}
    pre = Session._capture_provider_state(
        stub, {"components": [dict(comp, name="Old")]})
    new_ir = {"components": [dict(comp, name="Tmpl")],
              "manifest": {"templates": ["Tmpl"]}}
    assert Session._restore_provider_state(stub, new_ir, pre) is None
    assert tmpl.restored is None


def _mixed(*, isolated: bool, board_reads: str,
           realm: str = 'realm("tenant_a")') -> str:
    """One frame, two stores: `secret` serves `kv` (isolated in `realm` when
    `isolated`), `open` serves the shared `board`, and the hand-off is on
    `board`. The capture takes the whole frame, `secret` included."""
    clause = f"  isolate kv in {realm}\n" if isolated else ""
    return (
        "component Mixed provides kv: Kv, board: Kv {\n"
        f"{clause}"
        "  handoff board: Map[Str, Str]\n"
        "  let secret = effect Map.new() undo secret.drop()\n"
        "  let open = effect Map.new() undo open.drop()\n"
        "  provide kv {\n"
        "    fn get(k) = secret.get(k)\n"
        "    fn set(k, v) { effect secret.insert(k, v) undo secret.remove(k) }\n"
        "    fn unset(k) = secret.remove(k)\n"
        "  }\n"
        "  provide board {\n"
        f"    fn get(k) = {board_reads}.get(k)\n"
        "    fn set(k, v) { effect open.insert(k, v) undo open.remove(k) }\n"
        "    fn unset(k) = open.remove(k)\n"
        "  }\n"
        "}\n")


MIXED_REST = (
    "service Kv {\n  fn get(k: Str) -> Opt[Str]\n  fn set(k: Str, v: Str)\n"
    "  fn unset(k: Str)\n}\n\n")


@needs_runtime
def test_placement_change_on_another_key_starts_cold(tmp_path, monkeypatch):
    """The hand-off key (`board`) stays shared while the frame's other key
    (`kv`) leaves tenant_a. The frame's `secret` store is tenant_a's, so the
    capture must not cross: the slot is the whole placement, not the hand-off
    key's realm. Run as the operator so the placement change is admitted on
    any edit-path profile; the matcher, not admission, is under test."""
    from revl.mcp import server as server_mod

    server_mod.set_authoring_trust(host_code=True)
    monkeypatch.chdir(tmp_path)
    loaded = _call("revl_load", {
        "source": MIXED_REST + _mixed(isolated=True, board_reads="open")})
    assert loaded["ok"] is True, loaded
    _map_at("Mixed", 0).insert(*SECRET)
    changed = _call("revl_change", {"commit": True, "replace": {
        "component": "Mixed",
        "source": _mixed(isolated=False, board_reads="secret")}})
    assert changed["ok"] is True and changed.get("committed") is True, \
        [d.get("message") for d in changed.get("diagnostics") or []]
    read = _call("revl_call", {"key": "board", "method": "get",
                               "args": [SECRET[0]]})
    assert read["ok"] is True and read.get("result") != SECRET[1], read
    assert SECRET[0] not in dict(_map_at("Mixed", 0).data)
    entry = changed["handoff"]["board"]
    assert entry["migrated"] is False and entry["reason"] == "placement"


def _map_at(component: str, pos: int):
    from revl.mcp import server as server_mod

    session = server_mod.SESSION
    resources = session._frame_resources(session._driver.fibers.get(component))
    return resources[pos]


TENANT_A_APP = (
    "component TenantAApp requires kv: Kv {\n"
    '  isolate kv in realm("tenant_a")\n'
    '  effect kv.set("who", "alice") undo kv.unset("who")\n'
    "}\n")

# the operator binds the untrusted author's `realm(?tenant)` to tenant_a
BINDING = (("tenant", "tenant_a"),)


def _untrusted_bound() -> None:
    from revl.mcp import server as server_mod

    server_mod.set_authoring_trust(host_code=False, realm_bindings=BINDING)


def _operator_file(tmp_path: Path, monkeypatch, text: str) -> str:
    """Load `text` from a jailed file: operator-authored, while the transport
    stays untrusted for everything the agent sends after."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "running.rvl"
    target.write_text(text, encoding="utf-8")
    loaded = _call("revl_load", {"files": [str(target)]})
    assert loaded["ok"] is True, loaded
    return str(target)


def _board_get(key: str) -> dict:
    read = _call("revl_call", {"key": "board", "method": "get", "args": [key]})
    assert read["ok"] is True, read
    return read


def _committed(changed: dict) -> None:
    assert changed["ok"] is True and changed.get("committed") is True, \
        [d.get("message") for d in changed.get("diagnostics") or []]


def _tenant_a_store(extra_provides: str = "", extra_body: str = "") -> str:
    """TenantAStore as an untrusted author writes it: `realm(?tenant)`."""
    return (
        f"component TenantAStore provides kv: Kv{extra_provides} {{\n"
        "  isolate kv in realm(?tenant)\n"
        "  handoff kv: Map[Str, Str]\n"
        "  let store = effect Map.new() undo store.drop()\n"
        "  provide kv {\n"
        "    fn get(k) = store.get(k)\n"
        "    fn set(k, v) { effect store.insert(k, v) undo store.remove(k) }\n"
        "    fn unset(k) = store.remove(k)\n"
        "  }\n"
        f"{extra_body}"
        "}\n")


BOARD_OVER_STORE = (
    "  provide board {\n"
    "    fn get(k) = store.get(k)\n"
    "    fn set(k, v) { effect store.insert(k, v) undo store.remove(k) }\n"
    "    fn unset(k) = store.remove(k)\n"
    "  }\n")


@needs_runtime
def test_untrusted_successor_adding_a_shared_key_starts_cold(
        tmp_path, monkeypatch):
    """An untrusted replace keeps kv in tenant_a (through the operator's
    binding) but adds a shared `board` over the same store. The shared key is
    part of the placement, so the frame is placed differently: cold."""
    _untrusted_bound()
    _operator_file(tmp_path, monkeypatch, _tenants_with_handoff())
    _map("TenantAStore").insert(*SECRET)
    changed = _call("revl_change", {"commit": True, "replace": {
        "component": "TenantAStore",
        "source": _tenant_a_store(", board: Kv", BOARD_OVER_STORE)}})
    _committed(changed)
    assert _board_get(SECRET[0]).get("result") != SECRET[1]
    entry = changed["handoff"][A_LABEL]
    assert entry["migrated"] is False and entry["reason"] == "placement"


@needs_runtime
def test_untrusted_same_shape_rewiring_of_a_multi_realm_frame_starts_cold(
        tmp_path, monkeypatch):
    """The operator's frame isolates kv in tenant_a and hands off a shared
    `board`. An untrusted successor with the identical placement (through
    `realm(?tenant)`) points `board` at `secret`. Resources match by
    position, so an equal placement proves nothing: a frame spanning realms
    does not hand its state to an agent-authored successor."""
    _untrusted_bound()
    _operator_file(tmp_path, monkeypatch, MIXED_REST + TENANT_A_APP
                   + _mixed(isolated=True, board_reads="open"))
    _map_at("Mixed", 0).insert(*SECRET)
    changed = _call("revl_change", {"commit": True, "replace": {
        "component": "Mixed",
        "source": _mixed(isolated=True, board_reads="secret",
                         realm="realm(?tenant)")}})
    _committed(changed)
    assert _board_get(SECRET[0]).get("result") != SECRET[1]
    entry = changed["handoff"]["board"]
    assert entry["migrated"] is False and entry["reason"] == "multi-realm"


@needs_runtime
def test_untrusted_edit_of_the_mixed_frame_does_not_leak(tmp_path, monkeypatch):
    """The Mixed case as the untrusted author over revl_change, TenantAApp
    kept: drop the isolate clause and point `board` at `secret`. Either the
    swap is refused or the successor starts cold; the secret never reaches
    the shared `board`."""
    _untrusted_bound()
    target = _operator_file(tmp_path, monkeypatch, MIXED_REST + TENANT_A_APP
                            + _mixed(isolated=True, board_reads="open"))
    _map_at("Mixed", 0).insert(*SECRET)
    changed = _call("revl_change", {"commit": True, "edit": {
        "target": target, "edits": [
            {"anchor": '  isolate kv in realm("tenant_a")\n  handoff board',
             "replacement": "  handoff board"},
            {"anchor": "fn get(k) = open.get(k)",
             "replacement": "fn get(k) = secret.get(k)"}]}})
    if changed.get("committed"):
        assert _board_get(SECRET[0]).get("result") != SECRET[1]
        assert changed["handoff"]["board"]["migrated"] is False
    else:
        assert _map_at("Mixed", 0).data.get(SECRET[0]) == SECRET[1]


@needs_runtime
def test_control_untrusted_single_realm_successor_still_migrates(
        tmp_path, monkeypatch):
    _untrusted_bound()
    _operator_file(tmp_path, monkeypatch, _tenants_with_handoff())
    _map("TenantAStore").insert(*SECRET)
    changed = _call("revl_change", {"commit": True, "replace": {
        "component": "TenantAStore", "source": _tenant_a_store()}})
    _committed(changed)
    assert _store("TenantAStore")[SECRET[0]] == SECRET[1]
    assert changed["handoff"][A_LABEL]["migrated"] is True


@needs_runtime
def test_control_operator_multi_realm_successor_still_migrates(
        tmp_path, monkeypatch):
    from revl.mcp import server as server_mod

    server_mod.set_authoring_trust(host_code=True)
    monkeypatch.chdir(tmp_path)
    loaded = _call("revl_load", {"source": MIXED_REST + TENANT_A_APP
                                 + _mixed(isolated=True, board_reads="open")})
    assert loaded["ok"] is True, loaded
    _map_at("Mixed", 0).insert(*SECRET)
    changed = _call("revl_change", {"commit": True, "replace": {
        "component": "Mixed",
        "source": _mixed(isolated=True, board_reads="open").replace(
            "fn unset(k) = open.remove(k)", "fn unset(key) = open.remove(key)")}})
    _committed(changed)
    assert _map_at("Mixed", 0).data[SECRET[0]] == SECRET[1]
    assert changed["handoff"]["board"]["migrated"] is True
