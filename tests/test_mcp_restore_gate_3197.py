"""`revl_restore` is gated on the composition it boots, not on the document.

A snapshot document is caller-written JSON. The operator gate for `restore`
used to read its targets from the document's own `manifest`, which the caller
writes, so an operator scoped to `tenant_a*` could name one such component
there while the document's `sources` compiled and booted a different
composition. The gate now derives its targets from the IR the snapshot's
sources compile to, the same way `revl_load` derives them from its candidate.

The `meta.components` cross-check is the other half: it was skipped whenever
the caller left that field out, so a snapshot carrying no `meta.components`
re-admitted whatever its sources compiled to. It is now unconditional.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server  # noqa: E402
from revl.mcp.operator import decide, parse_profile  # noqa: E402

# the emitting composition from tests/test_505_cold_load_activation_authority:
# components `Store` and `Migrator`, neither in a tenant realm.
EMITTING = """
service Db {
  fn query(sql: Str) -> Str
  emission fn execute(sql: Str) -> Int
}

service S { fn q() -> Str }

component Store provides db: Db {
  provide db {
    fn query(sql) = "row"
    fn execute(sql) = 1
  }
}

component Migrator requires db: Db provides s: S {
  emit db.execute("INSERT INTO migration_log VALUES (42)")
  provide s { fn q() = "ok" }
}
"""

PURE = """
service S { fn q() -> Str }
component Store provides s: S { provide s { fn q() = "ok" } }
"""

PROFILE = "operator alice may restore on tenant_a*"

TENANT_A = """
service S { fn q() -> Str }
component TenantACache provides s: S {
  isolate s in realm("tenant_a")
  provide s { fn q() = "ok" }
}
"""

TENANT_B = (TENANT_A.replace("TenantACache", "TenantBCache")
            .replace('realm("tenant_a")', 'realm("tenant_b")'))


class _FakeSession:
    """Enough of a session for the gate: it reads only `ir` and `operator`."""

    def __init__(self, operator):
        self.ir = None
        self.operator = operator


def _snapshot(source: str, claimed: str | None,
              meta_components: list[str] | None = None) -> dict:
    """A snapshot whose `sources` are `source`, while `manifest` and `meta`
    carry whatever components the caller wants the gate to see. A real
    `revl_snapshot` fills both from the compiled IR; a caller need not."""
    manifest = {"components": [{"name": claimed}] if claimed else []}
    snap = {"sources": {"source": source}, "manifest": manifest, "meta": {}}
    if meta_components is not None:
        snap["meta"]["components"] = meta_components
    return snap


# --------------------------------------------------------- the gate itself


def test_a_forged_manifest_does_not_widen_the_restore_gate():
    """The document claims a `tenant_a*` component while its sources compile
    to `Store` and `Migrator`. The gate must scope the restore to what boots,
    so `may restore on tenant_a*` refuses it."""
    alice = parse_profile(PROFILE).get("alice")
    snap = _snapshot(EMITTING, claimed="tenant_a_shell", meta_components=[])
    decision = decide(_FakeSession(alice), "revl_restore",
                      {"snapshot": snap})
    assert decision.gated is True
    assert decision.allowed is False, (
        "the gate scoped the restore to the manifest name the caller wrote, "
        "not to the composition the sources compile to")
    assert "Store" in decision.message or "Migrator" in decision.message
    assert decision.verb == "restore"


def test_an_honest_snapshot_is_gated_on_its_compiled_components():
    """The control: naming the real components in `manifest` and `meta` does
    not make them reachable either. The decision is the same refusal."""
    alice = parse_profile(PROFILE).get("alice")
    snap = _snapshot(EMITTING, claimed="Store",
                     meta_components=["Store", "Migrator"])
    decision = decide(_FakeSession(alice), "revl_restore",
                      {"snapshot": snap})
    assert decision.gated is True and decision.allowed is False
    assert "Store" in decision.message or "Migrator" in decision.message


def test_a_grant_on_the_compiled_component_still_authorizes():
    """Deriving targets from the compiled IR must not refuse a restore the
    operator's profile does cover: `may restore on Store` sees exactly the
    component the sources compile to."""
    alice = parse_profile("operator alice may restore on Store").get("alice")
    snap = _snapshot(PURE, claimed="Store", meta_components=["Store"])
    decision = decide(_FakeSession(alice), "revl_restore",
                      {"snapshot": snap})
    assert decision.gated is True and decision.allowed is True
    assert decision.subjects == ("Store",)


def test_the_gate_reads_realms_from_the_compiled_composition():
    """A realm-scoped grant matches the realm the compiled component is
    isolated into, and a component outside it is still refused."""
    alice = parse_profile(PROFILE).get("alice")
    inside = decide(_FakeSession(alice), "revl_restore",
                    {"snapshot": _snapshot(TENANT_A, claimed="tenant_a_shell",
                                           meta_components=[])})
    outside = decide(_FakeSession(alice), "revl_restore",
                     {"snapshot": _snapshot(TENANT_B, claimed="tenant_a_shell",
                                            meta_components=[])})
    assert inside.allowed is True, inside.message
    assert inside.subjects == ("TenantACache",)
    assert outside.allowed is False
    assert "TenantBCache" in outside.message


def test_the_gate_scopes_the_components_the_handler_compiles():
    """The gate derives targets from the snapshot's sources through the same
    `recompile_snapshot` the handler loads. The handler compiles under the
    session's authoring profile; a profile may refuse that compile but must
    not reshape a successful one, so a document both accept scopes to the same
    component set."""
    from revl.mcp import persist

    alice = parse_profile("operator alice may restore on Store").get("alice")
    snap = _snapshot(PURE, claimed="tenant_a_shell",
                     meta_components=["Store"])
    decision = decide(_FakeSession(alice), "revl_restore", {"snapshot": snap})
    handler_ir = persist.recompile_snapshot(
        snap, profile=server.AUTHORING.profile())
    handler_names = tuple(sorted(
        entry.get("name")
        for entry in (handler_ir.get("manifest") or {}).get("components") or []))
    assert decision.allowed is True
    assert decision.subjects == handler_names == ("Store",)


def test_a_snapshot_that_does_not_compile_fails_closed():
    """An undecidable target set scopes to the whole composition: a
    subject-scoped operator is refused, and the error stays the handler's to
    report to whoever holds the whole-composition grant."""
    snap = _snapshot("service Broken {", claimed="tenant_a_shell",
                     meta_components=[])
    alice = parse_profile(PROFILE).get("alice")
    root = parse_profile("operator root may restore on *").get("root")
    refused = decide(_FakeSession(alice), "revl_restore", {"snapshot": snap})
    allowed = decide(_FakeSession(root), "revl_restore", {"snapshot": snap})
    assert refused.gated is True and refused.allowed is False
    assert "the whole composition" in refused.message
    assert allowed.allowed is True


def test_a_snapshot_with_no_sources_fails_closed():
    """A document that names no sources has nothing to compile, so its target
    set is undecidable rather than empty."""
    snap = {"sources": {}, "manifest": {"components": []}, "meta": {}}
    alice = parse_profile(PROFILE).get("alice")
    decision = decide(_FakeSession(alice), "revl_restore", {"snapshot": snap})
    assert decision.gated is True and decision.allowed is False
    assert "the whole composition" in decision.message


# ------------------------------------------------------ through the server


@pytest.fixture
def server_with_a_recorder(tmp_path, monkeypatch):
    """A closed-by-default server whose session records the IR it is asked to
    load instead of booting it, so the test needs no cordis runtime."""
    from revl.mcp.session import Session

    before = server.AUTHORING
    session = Session()
    loaded: list = []

    def record_load(ir, config=None, **_kwargs):
        loaded.append(ir)
        return {"loaded": True}

    monkeypatch.setattr(session, "load", record_load)
    monkeypatch.setattr(server, "SESSION", session)
    server.set_runtime_available(True)
    server.set_authoring_trust(host_code=False, granted=None, providers=None,
                               roots=(str(tmp_path),))
    yield session, loaded
    server.AUTHORING = before
    server.set_runtime_available(None)


def _call(name: str, arguments: dict) -> dict:
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})
    return json.loads(response["result"]["content"][0]["text"])


def test_the_forged_snapshot_is_refused_before_anything_loads(
        server_with_a_recorder):
    """End to end: the operator gate runs before the handler, so the forged
    document never reaches a compile-and-load."""
    session, loaded = server_with_a_recorder
    session.operator = parse_profile(PROFILE).get("alice")
    payload = _call("revl_restore",
                    {"snapshot": _snapshot(PURE, claimed="tenant_a_shell",
                                           meta_components=[])})
    assert payload["ok"] is False, payload
    assert payload["authority"]["allowed"] is False, payload
    assert payload["authority"]["verb"] == "restore"
    assert "may not" in payload["diagnostics"][0]["message"]
    assert loaded == [], "the forged snapshot reached the load"


def test_a_snapshot_without_meta_components_is_refused(server_with_a_recorder):
    """The `meta.components` cross-check no longer has an `if claimed`
    short-circuit. A document that omits the field claims nothing, which does
    not match the components its sources compile to."""
    session, loaded = server_with_a_recorder
    payload = _call("revl_restore",
                    {"snapshot": _snapshot(PURE, claimed=None,
                                           meta_components=None)})
    assert payload["ok"] is False, payload
    assert payload["restored"] is False
    message = " ".join(d.get("message", "")
                       for d in payload.get("diagnostics") or [])
    assert "no longer reproduce the snapshotted composition" in message, payload
    assert loaded == []


# ------------------------------------- the gate and the boot read one text


STORE = """
service S { fn q() -> Str }
component Store provides s: S { provide s { fn q() = "ok" } }
"""

# realm-free, so the session's authoring profile admits it at the boot; the
# grant scopes by component name instead
TENANT_A_PLAIN = STORE.replace("Store", "TenantACache")
NAME_PROFILE = "operator alice may restore on TenantA*"


def _files_snapshot(path: Path, meta_components: list[str]) -> dict:
    """A files-mode snapshot that carries `files` but no `files_content`, so
    a compile of it reads the file from disk."""
    return {"sources": {"files": [str(path)]},
            "manifest": {"components": []},
            "meta": {"components": meta_components}}


def _write_between_gate_and_boot(monkeypatch, path: Path, text: str) -> None:
    """Rewrite `path` right after the operator gate decides, so the handler
    runs on a disk that differs from the one the gate read."""
    real_decide = server._operator.decide

    def decide_then_write(session, name, arguments):
        decision = real_decide(session, name, arguments)
        path.write_text(text)
        return decision

    monkeypatch.setattr(server._operator, "decide", decide_then_write)


def _loaded_names(loaded: list) -> list[str]:
    return sorted(entry.get("name")
                  for ir in loaded
                  for entry in (ir.get("manifest") or {}).get("components") or [])


@pytest.mark.parametrize("claimed", [["Store"], ["TenantACache"]])
def test_a_file_changed_after_the_gate_does_not_boot(
        server_with_a_recorder, tmp_path, monkeypatch, claimed):
    """The gate decides on `TenantACache`; the file then holds `Store`. The
    boot must load the text the gate decided on, so whatever loads equals
    `authority.subjects`, whichever set the document claims."""
    session, loaded = server_with_a_recorder
    session.operator = parse_profile(NAME_PROFILE).get("alice")
    path = tmp_path / "app.rvl"
    path.write_text(TENANT_A_PLAIN)
    _write_between_gate_and_boot(monkeypatch, path, STORE)
    payload = _call("revl_restore",
                    {"snapshot": _files_snapshot(path, claimed)})
    assert "Store" not in _loaded_names(loaded), payload
    if payload.get("ok"):
        assert _loaded_names(loaded) == payload["authority"]["subjects"]
    if claimed == ["TenantACache"]:
        assert payload["ok"] is True, payload
        assert _loaded_names(loaded) == ["TenantACache"]


def test_an_undecided_restore_reads_nothing_from_disk(
        server_with_a_recorder, tmp_path, monkeypatch):
    """The gate cannot compile the file, so it decides on the whole
    composition and a `*` operator proceeds. A file made valid before the boot
    must not load: with an operator bound the boot compile reads only text
    the gate read."""
    session, loaded = server_with_a_recorder
    session.operator = parse_profile(
        "operator root may restore on *").get("root")
    path = tmp_path / "app.rvl"
    path.write_text("service Broken {")
    _write_between_gate_and_boot(monkeypatch, path, STORE)
    payload = _call("revl_restore",
                    {"snapshot": _files_snapshot(path, ["Store"])})
    assert payload["ok"] is False, payload
    assert loaded == [], payload
    message = " ".join(d.get("message", "")
                       for d in payload.get("diagnostics") or [])
    assert "from disk" in message and "operator gate" in message, payload


def test_a_restore_with_no_operator_still_reads_the_disk(
        server_with_a_recorder, tmp_path):
    """The control: with no operator bound nothing is gated, and a files-mode
    snapshot without `files_content` restores from the disk as before."""
    _session, loaded = server_with_a_recorder
    path = tmp_path / "app.rvl"
    path.write_text(STORE)
    payload = _call("revl_restore",
                    {"snapshot": _files_snapshot(path, ["Store"])})
    assert payload["ok"] is True, payload
    assert _loaded_names(loaded) == ["Store"]
