"""Taint declassification — the endorsement boundary (roadmap item 249, Slice C).

Slice A shipped a per-body checker with an ambient `endorse(v)`; Slice B made
taint propagate across every call boundary. Slice C makes the ONLY sanctioned way
an `Untrusted[T]` becomes `Trusted[T]` a declared, auditable, policy-forbiddable
endorsement point, so declassification is explicit and on the audit surface, not
silent. The three declassifiers are (1) a checked parser from the trusted closure
(landed), (2) a scoped, reasoned, policy-forbiddable `endorse[<origin>]` the
enclosing declaration admits, and (3) a typed human approval on the item-246
surface. None is available to an untrusted author's root module.

See docs/design/249-taint-provenance.md, "Slice C: the endorsement boundary as a
granted surface" and its exit tests.
"""

import pytest

from revl import RevlError
from revl.admit_profile import AdmissionProfile
from revl.audit_diff import audit_report, crossings, diff_crossings
from revl.compiler import compile_source
from revl.diagnostics import classify
from revl.policy import evaluate, parse_policy

_PRELUDE = (
    "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
    "extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }\n"
)


def _agent(op: str, body: str, header: str = "") -> str:
    """A component whose provide method implements service operation `op`."""
    return (
        _PRELUDE
        + f"service Ops {{ {op} }}\n"
        + "component Agent provides ops: Ops {\n"
        + header
        + "  provide ops {\n"
        + f"    fn go(url) {{\n{body}\n    }}\n"
        + "  }\n"
        + "}\n"
    )


# --- C1: the scoped endorse -- declared, reasoned -----------------------------

def test_declared_endorse_lets_an_untrusted_value_pass_a_trusted_sink():
    """The headline flow: a fetched (web-origin) value, endorsed at a declared
    point, passes a shell sink. The service operation declares the slot."""
    src = _agent(
        "emission endorse[web] fn go(url: Str)",
        "      let page = emit fetch(url)\n"
        "      let safe = endorse[web](page, reason = \"operator-reviewed template\")\n"
        "      emit run(safe)")
    compile_source(src, "endorsed.rvl")  # must not raise


def test_undeclared_endorse_is_refused():
    """An `endorse[web]` whose enclosing declaration does not grant the slot is
    refused: a downgrade must appear in the declaration, never ambient."""
    src = _agent(
        "emission fn go(url: Str)",  # no `endorse[web]` slot
        "      let page = emit fetch(url)\n"
        "      let safe = endorse[web](page, reason = \"x\")\n"
        "      emit run(safe)")
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "undeclared.rvl")
    assert classify(excinfo.value)["code"] == "G9"
    assert "undeclared declassification" in excinfo.value.message


def test_top_level_fn_endorse_slot():
    """The declaration slot is available on a top-level `fn` too, and the origin
    it does not declare is still refused."""
    wash = ("endorse[web] fn wash(s: Untrusted[Str]) -> Trusted[Str] {\n"
            "  return endorse[web](s, reason = \"parsed\")\n"
            "}\n")
    ok = (
        _PRELUDE
        + wash
        + "service Ops { emission fn go(url: Str) }\n"
        + "component Agent provides ops: Ops {\n"
        + "  provide ops {\n"
        + "    fn go(url) { let page = emit fetch(url)  emit run(wash(page)) }\n"
        + "  }\n"
        + "}\n"
    )
    compile_source(ok, "fnslot_ok.rvl")  # must not raise

    bad = (
        _PRELUDE
        + "endorse[net] fn wash(s: Untrusted[Str]) -> Trusted[Str] {\n"
        + "  return endorse[web](s, reason = \"parsed\")\n"   # web not declared
        + "}\n"
    )
    with pytest.raises(RevlError) as excinfo:
        compile_source(bad, "fnslot_bad.rvl")
    assert classify(excinfo.value)["code"] == "G9"


def test_ambient_endorse_is_refused_with_a_migration_hint():
    """Slice A's ambient `endorse(v)` is superseded — refused at parse with a
    migration hint to the scoped form."""
    with pytest.raises(RevlError) as excinfo:
        compile_source("fn f(x: Str) -> Str { return endorse(x) }", "ambient.rvl")
    assert "superseded" in excinfo.value.message
    assert "endorse[<origin>]" in (excinfo.value.hint or "")


def test_endorse_requires_a_reason():
    src = _agent(
        "emission endorse[web] fn go(url: Str)",
        "      let page = emit fetch(url)\n"
        "      let safe = endorse[web](page)\n"   # no reason
        "      emit run(safe)")
    with pytest.raises(RevlError):
        compile_source(src, "noreason.rvl")


def test_endorse_is_origin_precise_a_second_origin_still_refuses():
    """`endorse[web]` clears only the web origin — a value that also carries a
    net origin is still refused at the sink (a scoped downgrade, not a blanket
    clean)."""
    src = (
        "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
        "extern emission[net] fn peek(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
        "extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }\n"
        "service Ops { emission endorse[web] fn go(url: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops {\n"
        "    fn go(url) {\n"
        "      let a = emit fetch(url)\n"
        "      let b = emit peek(url)\n"
        "      let both = a + b\n"                       # web ∪ net
        "      let safe = endorse[web](both, reason = \"only web reviewed\")\n"
        "      emit run(safe)\n"                          # net still refuses
        "    }\n"
        "  }\n"
        "}\n"
    )
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "precise.rvl")
    assert classify(excinfo.value)["code"] == "G9"
    assert "net" in excinfo.value.message


# --- C: policy-forbiddable -----------------------------------------------------

def test_policy_may_forbid_declassifying_an_origin():
    """`component * may not declassify web` refuses admission of a component that
    endorses a web-origin value; a rule naming a different origin is clean."""
    src = _agent(
        "emission endorse[web] fn go(url: Str)",
        "      let page = emit fetch(url)\n"
        "      let safe = endorse[web](page, reason = \"ack\")\n"
        "      emit run(safe)")
    audit = audit_report(compile_source(src, "forbid.rvl"))

    forbid_web = parse_policy("component * may not declassify web", "p.rvl")
    vios = evaluate(forbid_web, audit)
    assert vios and vios[0].kind == "declassify"
    assert "may not declassify" in vios[0].message

    forbid_net = parse_policy("component * may not declassify net", "p2.rvl")
    assert evaluate(forbid_net, audit) == []


def test_policy_declassify_rule_is_realm_scoped_too():
    pol = parse_policy("realm billing may not declassify model", "p.rvl")
    assert len(pol.declassify_rules) == 1
    rule = pol.declassify_rules[0]
    assert rule.scope == "realm" and rule.selector == "billing"
    assert rule.patterns == ("model",)


# --- C2: approval-gated endorse, on the item-246 surface ----------------------

def _approval_agent(with_edge: bool) -> str:
    header = "  let a = await approval[declassify.web] { reason: \"ship it\" }\n"
    endorse = ("endorse[web](page, reason = \"ack\")"
               + (" with a" if with_edge else ""))
    return _agent(
        "emission endorse[web] fn go(url: Str)",
        "      let page = emit fetch(url)\n"
        f"      let safe = {endorse}\n"
        "      emit run(safe)",
        header=header)


def test_endorse_under_a_requires_approval_rule_needs_a_covering_edge():
    pol = parse_policy("capability declassify.web requires approval", "p.rvl")

    no_edge = audit_report(compile_source(_approval_agent(False), "noedge.rvl"))
    vios = evaluate(pol, no_edge)
    assert vios and vios[0].kind == "declassify-approval"

    with_edge = audit_report(compile_source(_approval_agent(True), "edge.rvl"))
    assert evaluate(pol, with_edge) == []


def test_endorse_records_its_approval_edge_on_the_audit_surface():
    ir = compile_source(_approval_agent(True), "edge2.rvl")
    records = ir["components"][0]["taint"]["declassify_records"]
    assert records[0]["approved"] == "declassify.web"


# --- C3: the untrusted-author profile forbids self-minted declassifiers -------

def test_untrusted_author_profile_refuses_a_root_module_endorse():
    """`no_declassify` (on by default in the untrusted-author profile) refuses an
    `endorse` in the admitted root source, structurally, before lowering."""
    prof = AdmissionProfile.untrusted_author(["ops"])
    root = ("endorse[web] fn launder(x: Untrusted[Str]) -> Trusted[Str] {\n"
            "  return endorse[web](x, reason = \"r\")\n}\n")
    with pytest.raises(RevlError) as excinfo:
        compile_source(root, "root_endorse.rvl", profile=prof)
    assert "admission refused" in excinfo.value.message
    assert "declassif" in excinfo.value.message


def test_untrusted_author_profile_refuses_a_root_trusted_returning_verified_fn():
    """The second door: a root-declared `verified fn` returning `Trusted[...]` is
    a laundering parser the turn mints for itself — refused structurally."""
    prof = AdmissionProfile.untrusted_author(["ops"])
    root = "verified fn v(s: Untrusted[Str]) -> Trusted[Str] { return s }\n"
    with pytest.raises(RevlError) as excinfo:
        compile_source(root, "root_verified.rvl", profile=prof)
    assert "admission refused" in excinfo.value.message


def test_no_declassify_is_on_by_default_in_untrusted_author():
    assert AdmissionProfile.untrusted_author([]).no_declassify is True
    assert AdmissionProfile().no_declassify is False


def test_the_same_endorse_admits_without_the_profile():
    """The discipline is the profile's, not the language's: the same root source
    that the untrusted-author profile refuses compiles fine for a trusted
    author (no profile)."""
    root = ("endorse[web] fn wash(s: Untrusted[Str]) -> Trusted[Str] {\n"
            "  return endorse[web](s, reason = \"parsed\")\n}\n")
    compile_source(root, "trusted.rvl")  # no profile — must not raise


# --- Slice B regression: an un-endorsed launder still refuses at G9 -----------

def test_unannotated_cross_component_relay_still_refuses_at_G9():
    """The Slice B relay: a fetch laundered through an unannotated cross-component
    relay into a shell sink is still refused at G9 (Slice C is additive; without
    a declared endorse the laundering path is unchanged)."""
    src = (
        _PRELUDE
        + "service Relay { emission fn pass_on(s: Str) }\n"
        + "service Ops { emission fn go(url: Str) }\n"
        + "component Middle provides relay: Relay {\n"
        + "  provide relay { fn pass_on(s) { emit run(s) } }\n"
        + "}\n"
        + "component Agent requires relay: Relay provides ops: Ops {\n"
        + "  provide ops {\n"
        + "    fn go(url) {\n"
        + "      let page = emit fetch(url)\n"
        + "      emit relay.pass_on(page)\n"
        + "    }\n"
        + "  }\n"
        + "}\n"
    )
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "relay.rvl")
    assert classify(excinfo.value)["code"] == "G9"


def test_direct_launder_still_refuses_at_G9():
    src = _agent(
        "emission fn go(url: Str)",
        "      let page = emit fetch(url)\n"
        "      emit run(page)")
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "direct.rvl")
    assert classify(excinfo.value)["code"] == "G9"


# --- additivity: an endorse-free program is byte-identical --------------------

def test_endorse_free_program_is_untouched():
    """A program using neither qualifier, no endorse slot, no taint policy rule
    engages nothing — no `taint`/`declassify` on the IR or the audit surface."""
    src = (
        "extern emission[web] fn fetch(url: Str) -> Str = @py { return \"\" }\n"
        "extern emission[shell] fn run(cmd: Str) = @py { return }\n"
        "service Ops { emission fn go(url: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops { fn go(url) { let page = emit fetch(url)  emit run(page) } }\n"
        "}\n"
    )
    ir = compile_source(src, "plain.rvl")
    assert "taint" not in ir["components"][0]
    tokens = crossings(audit_report(ir))
    assert not any(t.startswith(("taint:", "declassify:")) for t in tokens)


# --- the audit surface shows the declassification -----------------------------

def test_declassification_lands_on_the_audit_surface_with_its_record():
    """The declassify record grows to `{origin, method, reason, line}`, while the
    diff token stays the coarse `declassify:<component>:<origin>` so `audit
    --diff` still fails on a newly-added endorse as a widening."""
    src = _agent(
        "emission endorse[web] fn go(url: Str)",
        "      let page = emit fetch(url)\n"
        "      let safe = endorse[web](page, reason = \"operator-reviewed\")\n"
        "      emit run(safe)")
    ir = compile_source(src, "audit.rvl")

    # the enriched record
    record = ir["components"][0]["taint"]["declassify_records"][0]
    assert record["origin"] == "web"
    assert record["method"] == "Agent.go"
    assert record["reason"] == "operator-reviewed"
    assert isinstance(record["line"], int)

    # the stable coarse token
    audit = audit_report(ir)
    assert "declassify:Agent:web" in crossings(audit)


def test_a_newly_added_endorse_is_a_widening_that_fails_audit_diff():
    """`audit --diff` treats a newly-added declassification as a widening (a
    `declassify:` crossing appears), the same way one more emission fails."""
    without = audit_report(compile_source(
        # a version that does not declassify (an untrusted-tolerant net sink)
        _PRELUDE
        + "extern emission[net] fn log_it(s: Str) = @py { return }\n"
        + "service Ops { emission fn go(url: Str) }\n"
        + "component Agent provides ops: Ops {\n"
        + "  provide ops { fn go(url) { let page = emit fetch(url)  emit log_it(page) } }\n"
        + "}\n",
        "before.rvl"))

    with_endorse = audit_report(compile_source(
        _PRELUDE
        + "extern emission[net] fn log_it(s: Str) = @py { return }\n"
        + "service Ops { emission endorse[web] fn go(url: Str) }\n"
        + "component Agent provides ops: Ops {\n"
        + "  provide ops {\n"
        + "    fn go(url) {\n"
        + "      let page = emit fetch(url)\n"
        + "      let safe = endorse[web](page, reason = \"ok\")\n"
        + "      emit log_it(safe)\n"
        + "    }\n"
        + "  }\n"
        + "}\n",
        "after.rvl"))

    delta = diff_crossings(without, with_endorse)
    assert "declassify:Agent:web" in delta["added"]
