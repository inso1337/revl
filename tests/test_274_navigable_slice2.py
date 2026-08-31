"""Navigable refusals, slice 2 (item 274): the REMAINING refusal families wired
onto the same `navigate` mechanism slice 1 built.

Families wired here: approval (246), ceilings (294/260), ownership (308 O1/B1/
R0), evidence (290), adapter (296), cache (310), admit profile (329/330). For
each the tests assert:

  * the trusted-view record carries the right `enacts` party and `proof` marker;
  * the untrusted-author collapse holds (a policy-family refusal degrades to the
    single non-discriminating verdict, byte-identical across the matrix), and the
    admit-profile family is the ONE §4 exception (it enumerates the author's own
    granted set);
  * no `clears-this-gate` marker sits on a lease/ledger/ceiling operand (the
    HIGH soundness sweep, extended to the new families);
  * the first refusal line is byte-identical - `navigate` is additive.

The tests are the spec made executable (design §7).
"""

import pytest

from revl import RevlError
from revl import navigate as nav
from revl.admit_profile import AdmissionProfile
from revl.adapt import Refusal, navigate_for_refusals
from revl.compiler import compile_source
from revl.lower import check_and_lower
from revl.parser import Parser
from revl.policy import (approval_admission, evaluate, parse_policy)


# --------------------------------------------------------------- helpers

def _refuse(src: str, filename: str = "t.rvl", **kw) -> RevlError:
    with pytest.raises(RevlError) as ei:
        compile_source(src, filename, **kw)
    return ei.value


def _refuse_lower(src: str) -> RevlError:
    with pytest.raises(RevlError) as ei:
        check_and_lower(Parser(src, "t.rvl").parse())
    return ei.value


def _untrusted():
    return AdmissionProfile.untrusted_author(["kv"])


_SOCK = (
    "type Sock = { fd: Int }\n"
    "extern pure fn close_sock(h: Sock) = @py { return None }\n"
    "extern acquire fn open_sock() -> Sock undo close_sock(result)"
    ' = @py { return {"fd": 1} }\n'
)


# ============================================================ approval (246)

def test_approval_recipe_is_runtime_approval_never_author():
    """The acquire-and-thread recipe is enacted by the runtime approval surface
    (revl has no principal directory), so it is `runtime-approval`, `candidate` -
    never `author`, never a promise."""
    rec = nav.approval_navigate(token="payment", ttl_ms=600000)
    assert rec["family"] == "approval"
    assert rec["refused"]["ttl_ms"] == 600000
    recipe = rec["alternatives"][0]
    assert recipe["enacts"] == nav.ENACTS_RUNTIME_APPROVAL
    assert recipe["proof"] == nav.PROOF_CANDIDATE


def test_approval_standing_grant_is_candidate_live_never_clears():
    """The covering standing grant is on the RUNTIME-mutable ledger (revoked or
    expired before the retry, TOCTOU): always `candidate`/`live`, never
    `clears-this-gate` (the HIGH fix)."""
    rec = nav.approval_navigate(token="payment", standing_grant="grant#7")
    grant = [a for a in rec["alternatives"] if a.get("ref") == "grant#7"][0]
    assert grant["proof"] == nav.PROOF_CANDIDATE
    assert grant["live"] is True


def test_approval_admission_attaches_navigate_and_keeps_first_line():
    src = (
        "service Pay { emission[payment] fn charge(x: Str) -> Int }\n"
        "service Ops { emission fn go() -> Int }\n"
        "component Agent requires pay: Pay provides ops: Ops {\n"
        '  provide ops { fn go() -> Int { return emit pay.charge("x") } }\n'
        "}\n")
    ir = compile_source(src, "p.rvl")
    pol = parse_policy("capability payment requires approval ttl 10m\n")
    violations = approval_admission(pol, ir)
    assert violations and violations[0].navigate["family"] == "approval"
    # the ttl the rule holds rides the refused record, not a guess.
    assert violations[0].navigate["refused"]["ttl_ms"] == 600000


def test_approval_collapses_under_untrusted():
    rec = nav.approval_navigate(token="payment", profile=_untrusted())
    assert rec == nav.collapsed()


# ============================================================ ceilings (294/260)

_CEILING_RESOURCE = (
    "service StoreA { emission[kv_a] fn write_a(row: Str) -> Int }\n"
    "service StoreB { emission[kv_b] fn write_b(row: Str) -> Int }\n"
    "service Task { emission fn go() -> Int }\n"
    "component Leaker requires kv_b: StoreB provides task: Task {\n"
    '  provide task { fn go() { emit kv_b.write_b("x") return 0 } }\n'
    "}\n"
    "component Supervisor requires kv_a: StoreA {\n"
    "  let l = effect spawn Leaker with { } undo l.dispose()\n"
    "}\n")

_CEILING_BUDGET = (
    "service NetTight { emission[net(requests=100)] fn call(u: Str) -> Int }\n"
    "service NetWide  { emission[net(requests=1000)] fn call(u: Str) -> Int }\n"
    "service Worker { emission[net] fn go() -> Int }\n"
    "component Child requires net: NetWide provides worker: Worker {\n"
    '  provide worker { fn go() -> Int { emit net.call("x"); return 0 } }\n'
    "}\n"
    "component Parent requires net: NetTight {\n"
    "  let c = effect spawn Child with { } undo c.dispose()\n"
    "}\n")


def test_ceiling_resource_narrow_clears_raise_is_operator_candidate():
    """A resource (path/host) bound is a STATIC fact at the refusal site, so
    narrowing the child to it clears the gate; raising it is operator-enacted."""
    err = _refuse(_CEILING_RESOURCE, "w.rvl")
    rec = err.navigate
    assert rec["family"] == "ceiling"
    narrow = [a for a in rec["alternatives"] if a["enacts"] == nav.ENACTS_AUTHOR][0]
    assert narrow["proof"] == nav.PROOF_CLEARS
    assert not narrow.get("live")
    raise_bound = [a for a in rec["alternatives"]
                   if a["enacts"] == nav.ENACTS_OPERATOR][0]
    assert raise_bound["proof"] == nav.PROOF_CANDIDATE


def test_ceiling_budget_narrow_is_candidate_live_never_clears():
    """A budget ceiling is a LIVE `remainingUses`/lease counter (TOCTOU): the
    in-bounds number may already be spent at the retry, so the narrowing is
    `candidate`/`live`, never `clears-this-gate` (the HIGH fix)."""
    err = _refuse(_CEILING_BUDGET)
    rec = err.navigate
    narrow = [a for a in rec["alternatives"] if a["enacts"] == nav.ENACTS_AUTHOR][0]
    assert narrow["proof"] == nav.PROOF_CANDIDATE
    assert narrow["live"] is True


def test_ceiling_first_line_unchanged():
    err = _refuse(_CEILING_BUDGET)
    assert "wider resource budget" in str(err).splitlines()[0]
    assert err.navigate is not None


def test_ceiling_collapses_under_untrusted():
    rec = nav.ceiling_navigate(param="calls", child_value="100",
                               parent_bound="10", bound_site="P",
                               is_budget=True, profile=_untrusted())
    assert rec == nav.collapsed()


# ============================================================ ownership (308)

def test_ownership_b1_return_is_author_candidate():
    src = (_SOCK + "service S { fn take(c: Sock) -> Sock }\n"
           "component P provides s: S { provide s { fn take(c) { return c } } }\n")
    err = _refuse(src)
    rec = err.navigate
    assert rec["family"] == "ownership"
    alt = rec["alternatives"][0]
    assert alt["enacts"] == nav.ENACTS_AUTHOR
    assert alt["proof"] == nav.PROOF_CANDIDATE  # a restructure, not gate-re-run
    assert str(err).splitlines()[0].endswith("(item 308, B1)")


def test_ownership_o1_names_the_only_legal_undo_site():
    src = (_SOCK + "service S { fn shut(c: Sock) -> Int }\n"
           "component P provides s: S {\n"
           "  provide s { fn shut(c) { let x = close_sock(c)  return 1 } }\n"
           "}\n")
    err = _refuse(src)
    rec = err.navigate
    assert rec["family"] == "ownership" and rec["refused"]["kind"] == "o1"
    assert "acquiring binding's own `undo`" in rec["alternatives"][0]["action"]


def test_ownership_r0_names_the_handle_type_to_declare():
    src = (_SOCK + "extern acquire fn grab() -> Int undo close_sock(result) "
           "= @py { return 1 }\n")
    err = _refuse(src)
    rec = err.navigate
    assert rec["family"] == "ownership" and rec["refused"]["kind"] == "r0"
    assert "nominal opaque handle type" in rec["alternatives"][0]["action"]


def test_ownership_compensate_clause_is_value_not_handle():
    rec = nav.ownership_navigate(kind="b1", resource="Sock", mode="borrowed",
                                 clause="compensate")
    assert "carry the data out as a value" in rec["alternatives"][0]["action"]


# ============================================================ evidence (290)

def test_evidence_missing_facet_names_producer_author_plus_operator_ack():
    rec = nav.evidence_navigate(facet="fault-sweep", threshold="full",
                                fact="unavailable",
                                producer="the fault gauntlet run")
    assert rec["family"] == "evidence" and rec["blocked"] is False
    by_enact = {a["enacts"] for a in rec["alternatives"]}
    assert by_enact == {nav.ENACTS_AUTHOR, nav.ENACTS_OPERATOR}
    author = [a for a in rec["alternatives"] if a["enacts"] == nav.ENACTS_AUTHOR][0]
    assert "the fault gauntlet run" in author["action"]


def test_evidence_recorded_below_threshold_is_blocked():
    """No command manufactures confidence: a recorded-but-below-threshold facet
    is a first-class `blocked`, naming both the operator and author paths in the
    reason but offering no gate-weakening alternative."""
    rec = nav.evidence_navigate(facet="fault-sweep", threshold="full",
                                fact="8/12 partial",
                                producer="the fault gauntlet run",
                                rule_line="component * requires evidence")
    assert rec["blocked"] is True
    assert rec["alternatives"] == []
    assert "no command manufactures confidence" in rec["reason"]


def test_evidence_real_admission_carries_navigate():
    from tests.test_evidence_policy import (  # noqa: PLC0415
        KEY, ROOTED, SOLO, _audit, _bundle, _solo_ir)
    audit = _audit(SOLO)
    bundle = _bundle(sweep=(8, 12))
    violations = evaluate(
        parse_policy(ROOTED), audit,
        evidence={"CsvReader": bundle}, origins={"CsvReader": "registry"},
        key=KEY, evidence_ir={"CsvReader": _solo_ir()})
    ev = [v for v in violations if v.kind == "evidence" and v.token == "fault-sweep"]
    assert ev and ev[0].navigate["family"] == "evidence"
    assert ev[0].navigate["blocked"] is True  # recorded 8/12, below threshold


def test_evidence_collapses_under_untrusted():
    rec = nav.evidence_navigate(facet="fault-sweep", threshold="full",
                                fact="unavailable", producer="x",
                                profile=_untrusted())
    assert rec == nav.collapsed()


# ============================================================ adapter (296)

def test_adapter_repairable_clause_is_author_candidate():
    refusals = [Refusal("m", "opt", None, "no-canonical-default",
                        "a scalar has no default", "add an explicit default")]
    rec = navigate_for_refusals(refusals)
    assert rec["family"] == "adapter" and rec["blocked"] is False
    alt = rec["alternatives"][0]
    assert alt["enacts"] == nav.ENACTS_AUTHOR
    assert alt["proof"] == nav.PROOF_CANDIDATE


def test_adapter_capability_expanding_clause_is_blocked_never_widens():
    """A capability-expanding clause (an uncovered reach) is terminal `blocked`:
    NO alternative that would widen authority is offered (the never-unsafe
    test)."""
    refusals = [Refusal("m", "return", None, "effect-exceeds-bound",
                        "candidate reaches outside the required bound",
                        "widen the required declaration")]
    rec = navigate_for_refusals(refusals)
    assert rec["blocked"] is True
    assert rec["alternatives"] == []
    # the unsafe "widen" wording never reaches a machine-actionable alternative.
    assert all("widen" not in (a.get("action") or "")
               for a in rec["alternatives"])


def test_adapter_collapses_under_untrusted():
    refusals = [Refusal("m", "opt", None, "no-canonical-default", "x", "y")]
    rec = navigate_for_refusals(refusals, profile=_untrusted())
    assert rec == nav.collapsed()


# ============================================================ cache (310)

def test_cache_external_missing_bound_add_clause_clears():
    err = _refuse_lower(
        "service S { emission fn get(k: Str) -> Str cache external }")
    rec = err.navigate
    assert rec["family"] == "cache" and rec["blocked"] is False
    assert rec["alternatives"][0]["proof"] == nav.PROOF_CLEARS
    assert "requires a freshness bound" in str(err).splitlines()[0]


def test_cache_pure_with_bound_drop_clause_clears():
    err = _refuse_lower("service S { fn g(k: Str) -> Str cache pure ttl 5m }")
    rec = err.navigate
    assert rec["family"] == "cache"
    assert rec["alternatives"][0]["proof"] == nav.PROOF_CLEARS


def test_cache_uncacheable_category_is_blocked():
    src = ("type W = { ok: Int }\ntype E = { err: Int }\n"
           "extern pure fn restore(w: W) -> Unit = @py { pass }\n"
           "extern witnessed[fs] fn rm(path: Str) -> Result[W, E] "
           "cache external ttl 5m undo restore(result) = @py { pass }\n")
    err = _refuse_lower(src)
    rec = err.navigate
    assert rec["family"] == "cache" and rec["blocked"] is True
    assert rec["alternatives"] == []
    assert "reclassify" in rec["reason"]  # names the unsafe move it will NOT do


def test_cache_collapses_under_untrusted():
    rec = nav.cache_navigate(kind="add", what="`S.g`", profile=_untrusted())
    assert rec == nav.collapsed()


# ============================================================ admit profile (329/330)

def test_admit_allowlist_enumerates_the_granted_set_under_untrusted():
    """The §4 EXCEPTION: the admit-profile family does NOT collapse. The granted
    set is the author's own contract, already observable from the program's
    successes, so it is enumerated even for the untrusted author."""
    prof = _untrusted()
    src = ("service Net { emission fn call(u: Str) -> Int }\n"
           "service Ops { fn go() }\n"
           "component A requires net: Net provides ops: Ops {\n"
           "  provide ops { fn go() { } }\n"
           "}\n")
    err = _refuse(src, "a.rvl", profile=prof)
    rec = err.navigate
    assert rec["family"] == "admit-profile"      # NOT the collapsed verdict
    assert rec["blocked"] is False
    assert rec["refused"]["granted"] == ["kv"]   # the author's own contract
    assert any(a["ref"] == "kv" for a in rec["alternatives"])


def test_admit_no_declassify_is_blocked():
    prof = _untrusted()
    src = ('service Ops { fn go() -> Int }\n'
           "component A provides ops: Ops {\n"
           '  provide ops { fn go() -> Int { return endorse[web](1, reason = "x") } }\n'
           "}\n")
    err = _refuse(src, "a.rvl", profile=prof)
    assert err.navigate["family"] == "admit-profile"
    assert err.navigate["blocked"] is True
    assert "cannot declassify" in err.navigate["reason"]


# ================================ CRITICAL: untrusted-author indistinguishability

def test_untrusted_matrix_across_new_policy_families_is_byte_identical():
    """A matrix of the new POLICY families (approval, ceiling, evidence) tripped
    under the untrusted profile yields BYTE-IDENTICAL navigate records - joining
    slice 1's taint/boundary - so no family, reason, or proof discriminates which
    gate fired."""
    prof = _untrusted()
    records = [
        nav.approval_navigate(token="payment", ttl_ms=1, profile=prof),
        nav.ceiling_navigate(param="calls", child_value="1", parent_bound="0",
                             bound_site="P", is_budget=True, profile=prof),
        nav.ceiling_navigate(param="fs.write", child_value="a", parent_bound="b",
                             bound_site="P", is_budget=False, profile=prof),
        nav.evidence_navigate(facet="fault-sweep", threshold="full",
                              fact="unavailable", producer="x", profile=prof),
        nav.evidence_navigate(facet="attestation", threshold="valid",
                              fact="8/12 partial", producer="y", profile=prof),
        nav.ownership_navigate(kind="o1", resource="Sock", profile=prof),
        nav.cache_navigate(kind="add", what="`S.g`", profile=prof),
        navigate_for_refusals(
            [Refusal("m", "opt", None, "no-canonical-default", "x", "y")],
            profile=prof),
    ]
    first = records[0]
    for r in records[1:]:
        assert r == first, "families must be indistinguishable under untrusted"
    assert first == nav.collapsed()
    assert "proof" not in repr(first["alternatives"])


def test_redacted_ceiling_is_byte_identical_to_a_genuine_block():
    """A refusal whose only real alternatives were redacted and a genuinely-
    blocked refusal are byte-identical under the untrusted profile."""
    prof = _untrusted()
    redacted = nav.ceiling_navigate(param="calls", child_value="1",
                                    parent_bound="0", bound_site="P",
                                    is_budget=True, profile=prof)
    genuine = nav.blocked_record(family="cache", reason="anything", profile=prof)
    assert redacted == genuine


# ================================ HIGH: clears-this-gate soundness sweep

def _all_builder_records():
    """Every new family's trusted record, for the soundness sweep."""
    return [
        nav.approval_navigate(token="payment", ttl_ms=1, standing_grant="g#1"),
        nav.ceiling_navigate(param="calls", child_value="100", parent_bound="10",
                             bound_site="P", is_budget=True),
        nav.ceiling_navigate(param="fs.write", child_value="a", parent_bound="b",
                             bound_site="P", is_budget=False),
        nav.ownership_navigate(kind="o1", resource="Sock"),
        nav.ownership_navigate(kind="b1", resource="Sock", mode="borrowed",
                               clause="return"),
        nav.ownership_navigate(kind="r0", returns="Int", handle_name="Grab"),
        nav.evidence_navigate(facet="fault-sweep", threshold="full",
                              fact="unavailable", producer="x"),
        nav.cache_navigate(kind="add", what="`S.g`"),
        nav.cache_navigate(kind="drop", what="`S.g`", clause="freshness"),
        navigate_for_refusals(
            [Refusal("m", "opt", None, "no-canonical-default", "x", "y")]),
    ]


def test_no_clears_marker_sits_on_a_live_operand():
    """The soundness sweep, extended to the new families: no `clears-this-gate`
    marker sits on a lease/ledger/ceiling operand (a value flagged `live`)."""
    for rec in _all_builder_records():
        for alt in rec.get("alternatives", []):
            if alt.get("proof") == nav.PROOF_CLEARS:
                assert not alt.get("live"), \
                    "clears-this-gate on a runtime-mutable (live) operand"


def test_operator_and_runtime_approval_alternatives_are_never_clears():
    """The self-mint invariant across every new family: an operator/runtime-
    approval alternative is never author-enactable and never `clears-this-gate`."""
    for rec in _all_builder_records():
        for alt in rec.get("alternatives", []):
            if alt["enacts"] in (nav.ENACTS_OPERATOR, nav.ENACTS_RUNTIME_APPROVAL):
                assert alt["proof"] == nav.PROOF_CANDIDATE


# ================================ real-alternative tests (apply the fix)

def test_ceiling_narrowing_the_child_removes_the_refusal():
    """Apply the author alternative (narrow the child to the parent's bound): the
    refusal is gone."""
    _refuse(_CEILING_BUDGET)  # NetWide (1000) under NetTight (100) refuses
    fixed = _CEILING_BUDGET.replace("requires net: NetWide", "requires net: NetTight")
    compile_source(fixed, "ok.rvl")  # narrowed to the bound -> admits


def test_cache_adding_the_freshness_bound_removes_the_refusal():
    _refuse_lower("service S { emission fn get(k: Str) -> Str cache external }")
    check_and_lower(Parser(
        "service S { emission[e] fn get(k: Str) -> Str cache external ttl 5m }",
        "ok.rvl").parse())
