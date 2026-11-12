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

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp.server import handle  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="a real `revl_load` boots the composition, which needs cordis-py "
           "(backends/python/.venv)")

#: A composition a real `revl_load` boots, to move the unit of work.
LOADABLE = "fn ok() -> Int { return 1 }\n"

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
    assert applied["payloadKey"] == "repaired"
    assert applied["verdict"] == "passed"
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
    assert refused["payloadKey"] == "repaired"
    assert refused["verdict"] == "refused"
    assert refused["refusal"]["code"] == "UNKNOWN_REPAIR"
    assert refused["diagnostics"][0]["category"] == "usage"


@needs_cordis
def test_a_stale_repair_id_is_a_named_refusal():
    refused = _call("revl_check", {"source": T2_OPT})
    repair_id = refused["repairId"]
    # a load, swap, rollback or undo moves the unit of work; the id was minted
    # in the unit before it, so it no longer names a repair of this one. A
    # real load, so the test covers the session wiring and not only `resolve`
    loaded = _call("revl_load", {"source": LOADABLE})
    assert loaded["ok"] is True, loaded
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


#: A T2 in an inline candidate that `use`s a module present only as an
#: overlay. A compile without the overlays stops at the `use` line, so the
#: repair is found only from the refusal the authoring compile raised.
OVERLAY_MODULE = "pub fn helper(u: Str) -> Int { return 1 }\n"
T2_USES_OVERLAY = (
    'use "./t2_overlay_2855.rvl" { helper }\n'
    "fn first() -> Opt[Int] {\n"
    "  return null\n"
    "}\n"
)

#: The same refusal inside an edited `files` buffer: the disk copy is clean
#: pre-edit text, the buffer rides as a `modules` overlay over it and itself
#: `use`s an overlay-only sibling.
T2_FILES_DISK = "fn first() -> Opt[Int] {\n  return None\n}\n"


def test_a_refusing_check_never_compiles_outside_the_authoring_door(
        monkeypatch):
    """Review of #3248, finding 1: building the repair must not recompile the
    candidate through the editor's unprofiled `compile_source`. Every compile
    a refusing `revl_check` runs goes through the authoring profile."""
    from revl.lsp import analysis
    from revl.mcp import server as server_mod

    server_mod.set_authoring_trust(host_code=False)
    unprofiled = []
    real = analysis.compile_source

    def spy(*args, **kwargs):
        unprofiled.append((args[1] if len(args) > 1 else None,
                           kwargs.get("profile")))
        return real(*args, **kwargs)

    real_server = server_mod.compile_source
    profiles = []

    def server_spy(*args, **kwargs):
        profiles.append(kwargs.get("profile"))
        return real_server(*args, **kwargs)

    monkeypatch.setattr(analysis, "compile_source", spy)
    monkeypatch.setattr(server_mod, "compile_source", server_spy)
    for source in (T2_OPT, A9_SOLE):
        refused = _call("revl_check", {"source": source})
        assert refused["ok"] is False
        assert refused["repairId"]
    assert unprofiled == []
    # the check, then the verification of each repair, all profiled
    assert len(profiles) >= 4
    assert all(profile is not None for profile in profiles)


def test_a_repair_beside_an_overlay_module_is_minted_and_applies():
    """Finding 1: the ranged diagnostic is the refusal the authoring compile
    raised, so an overlay-only `use` target does not hide the repair."""
    modules = {"t2_overlay_2855.rvl": OVERLAY_MODULE}
    refused = _call("revl_check", {"source": T2_USES_OVERLAY,
                                   "modules": modules})
    assert refused["ok"] is False
    assert refused["diagnostics"][0]["code"] == "T2"
    assert refused["repairId"]
    applied = _call("revl_apply_repair", {"repairId": refused["repairId"]})
    assert applied["ok"] is True
    assert applied["repaired"]["modules"] == modules
    assert "null" not in applied["repaired"]["source"]
    assert applied["check"]["ok"] is True


def test_a_repair_in_a_files_overlay_module_applies_to_the_buffer(tmp_path):
    """Finding 1: the refusal sits in an edited `files` buffer that uses an
    overlay-only sibling. The repair is spelled from that buffer and the
    repaired candidate carries the patched buffer as its overlay."""
    from revl.mcp import server as server_mod

    path = tmp_path / "cand_2855.rvl"
    path.write_text(T2_FILES_DISK)
    sibling = str(tmp_path / "t2_overlay_2855.rvl")
    server_mod.set_authoring_trust(host_code=False, roots=(str(tmp_path),))
    modules = {str(path): T2_USES_OVERLAY, sibling: OVERLAY_MODULE}
    refused = _call("revl_check", {"files": [str(path)], "modules": modules})
    assert refused["ok"] is False
    assert refused["diagnostics"][0]["code"] == "T2"
    assert refused["repairId"]
    applied = _call("revl_apply_repair", {"repairId": refused["repairId"]})
    assert applied["ok"] is True
    assert applied["repaired"]["files"] == [str(path)]
    buffer = applied["repaired"]["modules"][str(path)]
    assert "null" not in buffer and "return None" in buffer
    assert applied["repaired"]["modules"][sibling] == OVERLAY_MODULE
    assert applied["check"]["ok"] is True



#: A terse candidate: an `if` without parentheses, which `revl_check`
#: completes before it compiles, and a T2 `null` in an optional context.
T2_TERSE = (
    "fn first(x: Int) -> Opt[Int] {\n"
    "  if x > 1 { return null }\n"
    "  return Some(x)\n"
    "}\n"
)


def test_the_re_check_is_revl_checks_own_answer():
    """Review of #3248, finding 2: the re-check goes through `revl_check`'s
    handler, so it carries the same canonical report a direct check does."""
    refused = _call("revl_check", {"source": T2_OPT})
    applied = _call("revl_apply_repair", {"repairId": refused["repairId"]})
    direct = _call("revl_check", {"source": applied["repaired"]["source"]})
    assert applied["check"]["ok"] is True
    assert applied["check"]["canonicalSource"] == direct["canonicalSource"]


def test_applying_while_a_draft_is_held_is_the_draft_held_refusal():
    """Finding 2: a check of a candidate while a draft is held is refused as
    `DRAFT_HELD`, and so is applying a repair, whose re-check is that check."""
    from revl.mcp import server as server_mod

    refused = _call("revl_check", {"source": T2_OPT})
    scaffold = _call("revl_scaffold", {"service": "Greeter", "effect": False,
                                       "methods": ["greet() -> Str"]})
    try:
        held = _call("revl_load", {"source": scaffold["source"]})
        assert held["ok"] is True and held["draft"] is True, held
        applied = _call("revl_apply_repair",
                        {"repairId": refused["repairId"]})
        assert applied["ok"] is False
        assert applied["code"] == "DRAFT_HELD"
    finally:
        server_mod.SESSION.pending_draft = None
        server_mod.SESSION.draft = None


def test_the_stored_candidate_is_the_text_the_check_compiled():
    """Finding 3, documented in docs/mcp-reference.md: the repair's ranges are
    into the text the check compiled, so a terse candidate comes back with the
    punctuation the check completed as well as the repair."""
    refused = _call("revl_check", {"source": T2_TERSE})
    assert refused["ok"] is False
    assert refused["diagnostics"][0]["code"] == "T2"
    applied = _call("revl_apply_repair", {"repairId": refused["repairId"]})
    assert applied["repaired"]["source"] == T2_TERSE.replace(
        "if x > 1 { return null }", "if (x > 1) { return None }")
    assert applied["check"]["ok"] is True


#: Issue #2759's G1 and G2 candidates carry a `fix_code` on each repair entry.
#: They are not in the documented applyable set (T2, A9, G4, REVL), so a
#: refusal that lists them mints no `repairId`.
_SERVICES_2759 = ("service Clock { fn now() -> Int }\n"
                  "service UserSvc { fn go() -> Int }\n")
G1_WHOLE_STATEMENT = (
    _SERVICES_2759
    + "component User provides user: UserSvc {\n"
    "  provide user {\n"
    "    fn go() -> Int {\n"
    "      let t = clock.now()\n"
    "      return 1\n"
    "    }\n"
    "  }\n"
    "}\n"
)
G2_CONFLICT = _SERVICES_2759 + (
    "service Store { fn get(k: Str) -> Str }\n"
    "component A provides s: Store {\n"
    '  provide s { fn get(k) = "a" }\n'
    "}\n"
    "component B provides s: Store {\n"
    '  provide s { fn get(k) = "b" }\n'
    "}\n"
)


@pytest.mark.parametrize("source,code", [(G1_WHOLE_STATEMENT, "G1"),
                                         (G2_CONFLICT, "G2")])
def test_a_generated_capability_repair_mints_no_id(source, code):
    refused = _call("revl_check", {"source": source})
    assert refused["ok"] is False
    record = refused["diagnostics"][0]
    assert record["code"] == code
    # the engine did list verified candidates, so the guard is what holds
    assert any((r.get("fix_code") or {}).get("edits")
               for r in record.get("repairs") or [])
    assert "repairId" not in refused


def test_a_widening_repair_entry_is_never_taken():
    from revl.mcp import refusal_repair

    edits = [{"range": {"start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 0}},
              "newText": "x"}]
    widening = {"code": "G4", "repairs": [
        {"kind": "site-undo", "widens_authority": True,
         "fix_code": {"title": "t", "edits": edits}}]}
    assert refusal_repair._fix_of(widening) is None
    narrowing = {"code": "G4", "repairs": [
        {"kind": "site-undo", "fix_code": {"title": "t", "edits": edits}}]}
    assert refusal_repair._fix_of(narrowing)["edits"] == edits
    other_kind = {"code": "G2", "repairs": [
        {"kind": "rename-key", "widens_authority": False,
         "fix_code": {"title": "t", "edits": edits}}]}
    assert refusal_repair._fix_of(other_kind) is None


def test_an_applyable_refusal_without_a_line_is_still_reported():
    """A refusal raised with no line (policy and least-authority build some)
    must not crash the repair attachment: its range falls back to line 0."""
    from revl.compiler import compile_source
    from revl.diagnostics import report
    from revl.errors import RevlError

    with pytest.raises(RevlError) as raised:
        compile_source(T2_OPT, "<candidate>.rvl")
    error = (getattr(raised.value, "errors", None) or [raised.value])[0]
    error.line = None
    payload = report(error, sources={"<candidate>.rvl": T2_OPT},
                     verifier=lambda patched, one: [])
    assert payload["diagnostics"][0]["code"] == "T2"
