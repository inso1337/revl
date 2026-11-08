"""Issue #2855: apply a refusal's repair and return the repaired candidate.

A refusal carries the repair it knows (#2845), and the fixgen engine already
turns the unambiguous ones into a verified edit (#2684, `docs/fix-code.md`).
This is the action half: one opt-in verb takes the repair the refusal minted,
applies it, and returns the repaired candidate together with the re-check, so a
client turn is not spent reconstructing the edit by hand.

The verb is explicit and read-only: applying names what changed and what still
fails (the next refusal, when the patched candidate refuses again) and installs
nothing. A repair id the session never minted is `UNKNOWN_REPAIR`; one minted
before the unit of work moved is `STALE_REPAIR`.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp.server import handle  # noqa: E402

#: T2, a type error: `null` has no type, and in an optional context the one
#: mechanical rewrite is `None`. The fixgen engine verifies it.
T2_OPT = "fn first() -> Opt[Int] {\n  return null\n}\n"

#: A9, an admission rule: a provide block keyed `cache` while the component
#: declares exactly one provision, `db`, so the only key it can have meant is
#: `db`. The rename is mechanical and verified.
A9_SOLE = (
    "service Store {\n"
    "  fn get(k: Str) -> Str\n"
    "}\n"
    "component Cache provides db: Store {\n"
    "  provide cache {\n"
    "    fn get(k: Str) -> Str { return k }\n"
    "  }\n"
    "}\n"
)

#: A candidate that carries TWO independent faults. The first compile reports
#: the T2, whose repair applies; the re-check then names the T1 that remains.
NEXT_REFUSAL = (
    "fn first() -> Opt[Int] {\n"
    "  return null\n"
    "}\n"
    "fn bad(x: Str) -> Int { return x }\n"
)

#: A definite T1 mismatch whose minimal edit the compiler cannot name: the
#: prose repair stands, but there is no applyable repair to mint.
PLAIN_MISMATCH = "fn g(x: Str) -> Int { return x }\n"


#: A G4 missing-undo refusal whose `site-undo` repair the engine verified: the
#: record carries its `fix_code` on the repair entry, not on the record, so the
#: apply path must find it there. The candidate declares an extern, which the
#: untrusted-author profile refuses as host code, so the test compiles it under
#: the operator's trust.
G4_DECLARED_INVERSE = (
    "type H = Opaque\n"
    "extern pure fn close_h(h: H) -> Unit = @py { return }\n"
    "extern acquire fn open_h() -> H undo close_h(result) = @py { return 1 }\n"
    "component C {\n"
    "  let h = effect open_h()\n"
    "}\n"
)


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(autouse=True)
def _fresh_server_session():
    """The MCP surface shares one module-level session, undo stack and authoring
    trust. Start each test from the trust it found and put it back, so a loaded
    composition or a minted repair cannot leak into the next test."""
    from revl.mcp import server as server_mod

    saved = server_mod.AUTHORING
    server_mod.UNDO_STACK.clear()
    try:
        yield server_mod
    finally:
        if server_mod.SESSION.loaded:
            server_mod.SESSION.unload()
        server_mod.AUTHORING = saved
        server_mod.UNDO_STACK.clear()
        server_mod._remedy.forget()
        server_mod._refusal_repair.forget(server_mod.SESSION)


def test_a_type_error_refusal_carries_an_applyable_repair():
    refused = _call("revl_check", {"source": T2_OPT})
    assert refused["ok"] is False
    assert refused["diagnostics"][0]["code"] == "T2"
    fix = refused["diagnostics"][0]["fix_code"]
    assert "None" in fix["title"]
    assert refused["repairId"]
    # the refusal names the one call that applies the repair, so a caller that
    # does not have the verb in its core tier is still told it exists
    assert refused["next"] == {"tool": "revl_apply_repair",
                               "arguments": {"repairId": refused["repairId"]},
                               "ready": True}

    applied = _call("revl_apply_repair", {"repairId": refused["repairId"]})
    assert applied["ok"] is True
    assert applied["applied"]["code"] == "T2"
    patched = applied["repaired"]["source"]
    assert "null" not in patched and "None" in patched
    assert applied["check"]["ok"] is True


def test_an_admission_rule_refusal_carries_an_applyable_repair():
    refused = _call("revl_check", {"source": A9_SOLE})
    assert refused["ok"] is False
    assert refused["diagnostics"][0]["code"] == "A9"
    assert "provide cache" in refused["diagnostics"][0]["fix_code"]["title"] or \
        "db" in refused["diagnostics"][0]["fix_code"]["title"]

    applied = _call("revl_apply_repair", {"repairId": refused["repairId"]})
    assert applied["ok"] is True
    patched = applied["repaired"]["source"]
    assert "provide db" in patched and "provide cache" not in patched
    assert applied["check"]["ok"] is True


def test_a_g4_refusal_applies_the_site_undo_on_its_repair_entry():
    """G4 keeps its verified edit on the repair entry (issue #2684), and the
    apply verb must find it there as well as on the record."""
    from revl.mcp import server as server_mod

    saved = server_mod.AUTHORING
    server_mod.set_authoring_trust(host_code=True)
    try:
        refused = _call("revl_check", {"source": G4_DECLARED_INVERSE})
        assert refused["ok"] is False
        site = next(repair for repair in refused["diagnostics"][0]["repairs"]
                    if repair["kind"] == "site-undo")
        assert site["fix_code"]["edits"]
        applied = _call("revl_apply_repair",
                        {"repairId": refused["repairId"]})
        assert applied["ok"] is True
        assert " undo close_h(h)" in applied["repaired"]["source"]
        assert applied["check"]["ok"] is True
    finally:
        server_mod.AUTHORING = saved


def test_the_repaired_candidate_names_the_next_refusal():
    refused = _call("revl_check", {"source": NEXT_REFUSAL})
    applied = _call("revl_apply_repair", {"repairId": refused["repairId"]})
    assert applied["ok"] is True
    assert "null" not in applied["repaired"]["source"]
    check = applied["check"]
    assert check["ok"] is False
    assert [d["code"] for d in check["diagnostics"]] == ["T1"]
    # T1's minimal edit is not mechanical, so the re-check mints no id
    assert "repairId" not in check


def test_a_refusal_without_a_mechanical_repair_mints_no_id():
    refused = _call("revl_check", {"source": PLAIN_MISMATCH})
    assert refused["ok"] is False
    assert refused["repair"]["known"] is False
    assert "repairId" not in refused


def test_an_unknown_repair_id_is_a_named_refusal():
    refused = _call("revl_apply_repair", {"repairId": "0" * 64})
    assert refused["ok"] is False
    assert refused["code"] == "UNKNOWN_REPAIR"
    assert refused["refusal"]["code"] == "UNKNOWN_REPAIR"
    assert refused["diagnostics"][0]["category"] == "usage"


def test_a_stale_repair_id_is_a_named_refusal(_fresh_server_session):
    session = _fresh_server_session.SESSION
    refused = _call("revl_check", {"source": T2_OPT})
    repair_id = refused["repairId"]
    # a load, swap, rollback or undo moves the unit of work; the id was minted
    # in the unit before it, so it no longer names a repair of this one
    session._generation += 1
    stale = _call("revl_apply_repair", {"repairId": repair_id})
    assert stale["ok"] is False
    assert stale["code"] == "STALE_REPAIR"
    assert stale["refusal"]["code"] == "STALE_REPAIR"
    assert repair_id in stale["diagnostics"][0]["message"]


def test_applying_installs_nothing_and_the_id_is_reusable():
    refused = _call("revl_check", {"source": T2_OPT})
    repair_id = refused["repairId"]
    first = _call("revl_apply_repair", {"repairId": repair_id})
    second = _call("revl_apply_repair", {"repairId": repair_id})
    assert first["repaired"] == second["repaired"]
    assert first["check"]["ok"] is True and second["check"]["ok"] is True
    # the composition the session holds is untouched: applying returns a
    # candidate, it does not load or draft one
    state = _call("revl_state", {})
    assert state["loaded"] is False
