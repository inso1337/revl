"""Taint declassification, the cross-call laundering hole (roadmap item 249,
adversarial-review Findings 1-3).

Slice C made a scoped `endorse[<origin>]` the audited, policy-forbiddable
downgrade point, and pinned that it is origin-PRECISE *intra-body*
(`test_endorse_is_origin_precise_a_second_origin_still_refuses`). This suite pins
the same guarantee ACROSS A CALL BOUNDARY, which a bug silently broke: during
interprocedural signature inference every `endorse[<origin>]` returned fully
clean, so a one-hop helper `wash(x) { return endorse[web](x, ...) }` looked to
every call site like a TOTAL sanitizer for all origins. `fs`/`secret`/... data
laundered through it into an absolute-refusal shell sink and compiled clean.

  * Finding 1 (CRITICAL): the signature is now origin-aware (`_Signature.clears`).
    A `endorse[web]` helper clears only `web`; a cross-origin argument still
    refuses at the sink, across a plain call AND a service-operation boundary.
  * Finding 2 (HIGH): a top-level `fn` declassifier folds its downgrade onto the
    audit surface of every component that reaches it, so `may not declassify` and
    `audit --diff` can see a top-level washer.
  * Finding 3 (MEDIUM): a taint policy rule over a program with no taint surface
    (derivation off) warns loudly rather than passing as a silent no-op.
"""

import warnings

import pytest

from revl import RevlError
from revl.audit_diff import audit_report, crossings
from revl.compiler import compile_source
from revl.diagnostics import classify
from revl.policy import InertTaintPolicyWarning, evaluate, parse_policy

_WASH = ("endorse[web] fn wash(s: Untrusted[Str]) -> Trusted[Str] {\n"
         "  return endorse[web](s, reason = \"parsed\")\n}\n")


# --- Finding 1: cross-call origin precision -----------------------------------

def test_cross_call_endorse_does_not_launder_a_foreign_origin():
    """The headline hole: `fs`-origin data passed through a `endorse[web]` helper
    into a shell sink must still be REFUSED at G9. `endorse[web]` clears only
    `web`; it is not a blanket sanitizer for `fs`."""
    src = (
        "extern emission[fs] fn read_file(p: Str) -> Untrusted[Str] = @py { return \"\" }\n"
        "extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }\n"
        + _WASH
        + "service Ops { emission fn go(p: Str) }\n"
        + "component Agent provides ops: Ops {\n"
        + "  provide ops { fn go(p) { let data = emit read_file(p)  emit run(wash(data)) } }\n"
        + "}\n")
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "cross_call_launder.rvl")
    assert classify(excinfo.value)["code"] == "G9"
    assert "fs" in excinfo.value.message


def test_service_operation_boundary_does_not_launder_a_foreign_origin():
    """The 329+249 granted-closure variant: the same `fs`-to-shell flow laundered
    through an `emission endorse[web]` SERVICE OPERATION (a provide method behind a
    required key) is refused too. The endorse slot on the operation clears `web`,
    not `fs`."""
    src = (
        "extern emission[fs] fn read_file(p: Str) -> Untrusted[Str] = @py { return \"\" }\n"
        "extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }\n"
        "service Wash { emission endorse[web] fn clean(s: Untrusted[Str]) -> Trusted[Str] }\n"
        "service Ops { emission fn go(p: Str) }\n"
        "component Washer provides w: Wash {\n"
        "  provide w { fn clean(s) { return endorse[web](s, reason = \"r\") } }\n"
        "}\n"
        "component Agent requires w: Wash provides ops: Ops {\n"
        "  provide ops { fn go(p) {\n"
        "    let data = emit read_file(p)\n"
        "    let c = emit w.clean(data)\n"
        "    emit run(c)\n"
        "  } }\n"
        "}\n")
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "svc_boundary_launder.rvl")
    assert classify(excinfo.value)["code"] == "G9"
    assert "fs" in excinfo.value.message


def test_matching_origin_endorse_across_a_call_still_passes():
    """The legitimate case must stay green: `web`-origin data through the same
    `endorse[web]` helper into the shell sink compiles clean (the helper clears
    exactly the origin it declares)."""
    src = (
        "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
        "extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }\n"
        + _WASH
        + "service Ops { emission fn go(url: Str) }\n"
        + "component Agent provides ops: Ops {\n"
        + "  provide ops { fn go(url) { let page = emit fetch(url)  emit run(wash(page)) } }\n"
        + "}\n")
    compile_source(src, "matching_ok.rvl")  # must not raise


def test_cross_call_second_origin_still_refuses():
    """Cross-call analog of the intra-body origin-precision pin: a value carrying
    both `web` and `fs` through a `endorse[web]` helper still refuses on the `fs`
    component at the sink."""
    src = (
        "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
        "extern emission[fs] fn read_file(p: Str) -> Untrusted[Str] = @py { return \"\" }\n"
        "extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }\n"
        + _WASH
        + "service Ops { emission fn go(url: Str) }\n"
        + "component Agent provides ops: Ops {\n"
        + "  provide ops { fn go(url) {\n"
        + "    let a = emit fetch(url)\n"
        + "    let b = emit read_file(url)\n"
        + "    let both = a + b\n"
        + "    emit run(wash(both))\n"
        + "  } }\n"
        + "}\n")
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "cross_second_origin.rvl")
    assert classify(excinfo.value)["code"] == "G9"
    assert "fs" in excinfo.value.message


# --- Finding 2: a top-level washer is on the audit surface ---------------------

def test_top_level_fn_declassifier_is_audited_and_policy_forbiddable():
    """A top-level `fn` washer's downgrade now folds onto the audit surface of
    every component that reaches it: the `declassify:` token appears and
    `may not declassify` catches it (a different origin stays clean)."""
    src = (
        "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
        "extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }\n"
        + _WASH
        + "service Ops { emission fn go(url: Str) }\n"
        + "component Agent provides ops: Ops {\n"
        + "  provide ops { fn go(url) { let page = emit fetch(url)  emit run(wash(page)) } }\n"
        + "}\n")
    audit = audit_report(compile_source(src, "fn_washer_audit.rvl"))

    assert "declassify:Agent:web" in crossings(audit)
    record = audit["boundary"]["Agent"]["taint"]["declassify_records"][0]
    assert record["origin"] == "web" and record["method"] == "wash"

    vios = evaluate(parse_policy("component * may not declassify web", "p.rvl"), audit)
    assert vios and vios[0].kind == "declassify" and vios[0].component == "Agent"

    assert evaluate(parse_policy("component * may not declassify net", "p2.rvl"),
                    audit) == []


# --- Finding 3: an inert taint rule warns loudly -------------------------------

def test_inert_taint_policy_rule_warns():
    """A `web-taint may not reach net` rule over an UNANNOTATED, no-profile program
    (derived sources off) mints nothing and matches nothing. It must warn loudly,
    not pass as a silent no-op."""
    inert = (
        "extern emission[web] fn fetch(url: Str) -> Str = @py { return \"\" }\n"
        "service Sink { emission[net] fn send(body: Str) }\n"
        "service Ops { emission fn go(url: Str) }\n"
        "component Backend provides s: Sink { provide s { fn send(body) { } } }\n"
        "component Agent requires s: Sink provides ops: Ops {\n"
        "  provide ops { fn go(url) { let page = emit fetch(url)  emit s.send(page) } }\n"
        "}\n")
    audit = audit_report(compile_source(inert, "inert_rule.rvl"))
    pol = parse_policy("web-taint may not reach net", "p.rvl")
    with pytest.warns(InertTaintPolicyWarning):
        assert evaluate(pol, audit) == []


def test_an_active_taint_surface_does_not_warn():
    """With the same program ANNOTATED (`Untrusted[Str]`), the origin is minted, the
    rule bites, and no inert-rule warning fires."""
    active = (
        "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
        "service Sink { emission[net] fn send(body: Str) }\n"
        "service Ops { emission fn go(url: Str) }\n"
        "component Backend provides s: Sink { provide s { fn send(body) { } } }\n"
        "component Agent requires s: Sink provides ops: Ops {\n"
        "  provide ops { fn go(url) { let page = emit fetch(url)  emit s.send(page) } }\n"
        "}\n")
    audit = audit_report(compile_source(active, "active_rule.rvl"))
    pol = parse_policy("web-taint may not reach net", "p.rvl")
    with warnings.catch_warnings():
        warnings.simplefilter("error", InertTaintPolicyWarning)
        vios = evaluate(pol, audit)  # must not warn
    assert vios and vios[0].kind == "taint-flow"
