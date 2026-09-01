"""Taint, Slice D + the B3/B4 follow-ups (roadmap item 249) — the arc's finish.

Slice A shipped the qualifiers, Slice B the interprocedural propagation fixpoint,
Slice C the endorsement boundary. This suite covers the last pieces:

  * Slice D — DERIVED sinks and sources: a sink set that exists without the
    author's cooperation (a shell/exec/terminal-scoped crossing, the item-330
    admission crossing's `granted` list), a policy-gated tier over the landed
    taint tokens (`web-taint may not reach net without approval`), and derived
    sources under a profile-gated `taint_strict` mode so plain programs stay
    byte-identical;
  * B3 — component-STATE threading and body-local loop back edges;
  * B4 — first-class FUNCTION-VALUE taint with the over-approximate unnamed-callee
    rule.

See docs/design/249-taint-provenance.md, "Slice D" and the B3/B4 exit tests.
"""

import pytest

from revl import RevlError
from revl.admit_profile import AdmissionProfile
from revl.audit_diff import audit_report, crossings
from revl.compiler import compile_source
from revl.diagnostics import classify
from revl.policy import evaluate, parse_policy


def _strict() -> AdmissionProfile:
    """A profile that only turns derived sinks/sources on (no other gate)."""
    return AdmissionProfile(taint_strict=True)


# --- D1: derived sinks (shell / exec / terminal, the admission granted list) ---

def test_unannotated_shell_extern_refuses_untrusted_under_strict_mode():
    """The headline D1 exit: with NO `Trusted[T]` anywhere, a shell-scoped extern
    is a sink by scope derivation and a web emission mints its origin, so a
    fetch-to-shell program is refused at G9 under strict mode."""
    src = (
        "extern emission[web] fn fetch(url: Str) -> Str = @py { return \"\" }\n"
        "extern emission[shell] fn run(cmd: Str) = @py { return }\n"
        "service Ops { emission fn go(url: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops { fn go(url) { let page = emit fetch(url)  emit run(page) } }\n"
        "}\n"
    )
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "strict_shell.rvl", profile=_strict())
    err = excinfo.value
    assert classify(err)["code"] == "G9"
    assert "shell command" in err.message
    assert "web" in err.message


@pytest.mark.parametrize("scope", ["exec", "terminal"])
def test_exec_and_terminal_scopes_are_derived_sinks_too(scope):
    src = (
        "extern emission[web] fn fetch(url: Str) -> Str = @py { return \"\" }\n"
        f"extern emission[{scope}] fn run(cmd: Str) = @py {{ return }}\n"
        "service Ops { emission fn go(url: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops { fn go(url) { let page = emit fetch(url)  emit run(page) } }\n"
        "}\n"
    )
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, f"strict_{scope}.rvl", profile=_strict())
    assert classify(excinfo.value)["code"] == "G9"


def test_admission_granted_list_refuses_untrusted_while_source_accepts_it():
    """The item-330 asymmetry (D1): the admission crossing's `granted` list is a
    sink (`Trusted[List[Str]]`) — injected text must never choose what a turn is
    granted — while `source` deliberately accepts untrusted data, because the
    admission gate is its validator. No strict mode needed: the sink is an
    explicit stdlib annotation."""
    prelude = (
        "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
        "extern emission[web] fn fetch_list(url: Str) -> Untrusted[List[Str]] = @py { return }\n"
        "service Admission { emission fn admit(source: Str, granted: Trusted[List[Str]]) -> Str }\n"
        "service Ops { emission fn go(url: Str) }\n"
        "component Gate provides adm: Admission {\n"
        "  provide adm { fn admit(source, granted) { return source } }\n"
        "}\n"
    )

    # a tainted `granted` list is refused
    bad = (
        prelude
        + "component Agent requires adm: Admission provides ops: Ops {\n"
        + "  provide ops {\n"
        + "    fn go(url) {\n"
        + "      let g = emit fetch_list(url)\n"
        + "      let out = emit adm.admit(url, g)\n"
        + "    }\n"
        + "  }\n"
        + "}\n"
    )
    with pytest.raises(RevlError) as excinfo:
        compile_source(bad, "granted_sink.rvl")
    assert classify(excinfo.value)["code"] == "G9"

    # an untrusted `source` is accepted — admitting untrusted source is the point
    good = (
        prelude
        + "component Agent requires adm: Admission provides ops: Ops {\n"
        + "  provide ops {\n"
        + "    fn go(url) {\n"
        + "      let page = emit fetch(url)\n"
        + "      let out = emit adm.admit(page, [\"turn\"])\n"
        + "    }\n"
        + "  }\n"
        + "}\n"
    )
    compile_source(good, "source_ok.rvl")  # must not raise


def test_stdlib_admit_annotates_granted_as_a_sink():
    """The real `stdlib/admit.rvl` carries the `Trusted[List[Str]]` sink on both
    the service operation and the host extern."""
    import os
    from revl.parser import Parser
    from revl.taint import extract_and_normalize, top_qualifier

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "stdlib", "admit.rvl"), encoding="utf-8") as fh:
        program = Parser(fh.read(), "admit.rvl").parse()
    # capture the qualifier before extraction strips it
    ext = {e.name: e for e in program.externs}["host_admit"]
    assert top_qualifier(ext.params[1].type) == "Trusted"
    model = extract_and_normalize(program)
    assert 1 in model.sinks.get("host_admit", {})   # `granted` is a sink
    assert 0 not in model.sinks.get("host_admit", {})  # `source` is not
    assert 1 in model.sinks.get("admit", {})           # the service op too


# --- D2: the policy-gated tier over the landed taint tokens --------------------

_SEND = (
    "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
    "service Sink { emission[net] fn send(body: Str) }\n"
    "service Ops { emission fn go(url: Str) }\n"
    "component Backend provides s: Sink { provide s { fn send(body) { } } }\n"
)


def _sender(header: str = "", with_edge: str = "") -> str:
    return (
        _SEND
        + "component Agent requires s: Sink provides ops: Ops {\n"
        + header
        + "  provide ops {\n"
        + f"    fn go(url) {{ let page = emit fetch(url)  emit s.send(page){with_edge} }}\n"
        + "  }\n"
        + "}\n"
    )


def test_web_taint_to_net_refuses_without_approval():
    """The D2 exit: `web-taint may not reach net without approval` refuses a
    web-tainted send with no covering approval, naming the flow."""
    audit = audit_report(compile_source(_sender(), "d2_noapproval.rvl"))
    pol = parse_policy("web-taint may not reach net without approval", "p.rvl")
    vios = evaluate(pol, audit)
    assert vios and vios[0].kind == "taint-flow"
    assert "web`-taint may not reach" in vios[0].message


def test_web_taint_to_net_admits_with_a_covering_approval():
    """With an operator approval threaded on the send (`with a`), the same flow
    admits — the item-246 surface is the third declassifier."""
    src = _sender(header="  let a = await approval[net] { reason: \"ship it\" }\n",
                  with_edge=" with a")
    audit = audit_report(compile_source(src, "d2_approval.rvl"))
    pol = parse_policy("web-taint may not reach net without approval", "p.rvl")
    assert evaluate(pol, audit) == []


def test_web_taint_to_net_hard_rule_refuses_even_with_approval():
    """A rule with no `without approval` clause is absolute: an approval does not
    lift it."""
    src = _sender(header="  let a = await approval[net] { reason: \"ship it\" }\n",
                  with_edge=" with a")
    audit = audit_report(compile_source(src, "d2_hard.rvl"))
    pol = parse_policy("web-taint may not reach net", "p.rvl")
    vios = evaluate(pol, audit)
    assert vios and vios[0].kind == "taint-flow"


def test_taint_flow_rule_parses_from_dsl_and_json():
    dsl = parse_policy("model-taint may not reach fs", "p.rvl")
    assert len(dsl.taint_flow_rules) == 1
    rule = dsl.taint_flow_rules[0]
    assert rule.origin == "model" and rule.patterns == ("fs",)
    assert rule.without_approval is False

    js = parse_policy(
        '{"taintFlow": [{"origin": "web", "reach": ["net"], '
        '"withoutApproval": true}]}', "p.json")
    assert js.taint_flow_rules[0].origin == "web"
    assert js.taint_flow_rules[0].without_approval is True


def test_a_clean_program_is_untouched_by_a_taint_flow_rule():
    """A program that routes no taint to the capability is clean under the rule."""
    src = (
        "extern emission[net] fn send(body: Str) = @py { return }\n"
        "service Ops { emission fn go() }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops { fn go() { emit send(\"safe\") } }\n"
        "}\n"
    )
    audit = audit_report(compile_source(src, "clean_flow.rvl"))
    pol = parse_policy("web-taint may not reach net without approval", "p.rvl")
    assert evaluate(pol, audit) == []


# --- D3: derived sources under strict mode, and the permanent additivity line --

def test_taint_strict_off_is_byte_identical_to_pre_249():
    """The additivity line: the same unannotated fetch-to-shell program compiled
    with NO profile compiles clean and carries no taint surface — strict mode is
    opt-in, never ambient."""
    src = (
        "extern emission[web] fn fetch(url: Str) -> Str = @py { return \"\" }\n"
        "extern emission[shell] fn run(cmd: Str) = @py { return }\n"
        "service Ops { emission fn go(url: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops { fn go(url) { let page = emit fetch(url)  emit run(page) } }\n"
        "}\n"
    )
    ir = compile_source(src, "additive.rvl")  # no profile — must not raise
    assert "taint" not in ir["components"][0]
    assert not any(t.startswith(("taint:", "declassify:"))
                   for t in crossings(audit_report(ir)))


def test_strict_and_nonstrict_emit_the_same_ir_for_a_clean_program():
    """A program with no source-to-sink flow is byte-identical with strict ON and
    OFF: strict adds refusals and tokens only where taint actually flows."""
    src = (
        "extern emission[fs] fn read(p: Str) -> Str = @py { return \"\" }\n"
        "service Ops { emission fn go(p: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops { fn go(p) { let x = emit read(p) } }\n"
        "}\n"
    )
    off = compile_source(src, "q.rvl")
    on = compile_source(src, "q.rvl", profile=_strict())
    assert off["components"] == on["components"]
    assert off["externs"] == on["externs"]


def test_untrusted_author_profile_turns_strict_on_by_default():
    assert AdmissionProfile.untrusted_author([]).taint_strict is True
    assert AdmissionProfile().taint_strict is False


def test_derived_source_flows_through_the_propagation_fixpoint_under_strict():
    """Strict-derived sources compose with Slice B: a derived web source laundered
    through an unannotated cross-component relay into a derived shell sink refuses,
    with zero annotations anywhere."""
    src = (
        "extern emission[web] fn fetch(url: Str) -> Str = @py { return \"\" }\n"
        "extern emission[shell] fn run(cmd: Str) = @py { return }\n"
        "service Relay { emission fn pass_on(s: Str) }\n"
        "service Ops { emission fn go(url: Str) }\n"
        "component Middle provides relay: Relay {\n"
        "  provide relay { fn pass_on(s) { emit run(s) } }\n"
        "}\n"
        "component Agent requires relay: Relay provides ops: Ops {\n"
        "  provide ops {\n"
        "    fn go(url) { let page = emit fetch(url)  emit relay.pass_on(page) }\n"
        "  }\n"
        "}\n"
    )
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "strict_relay.rvl", profile=_strict())
    assert classify(excinfo.value)["code"] == "G9"


# --- B3: component-STATE threading -------------------------------------------

def test_untrusted_state_field_taints_its_reads_across_methods():
    """B3 state: a value stored into a component state world by one method is
    tainted when a DIFFERENT method reads it into a sink — the walk now threads a
    per-component state environment (join over all writers)."""
    src = (
        "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
        "extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }\n"
        "service Ops { emission fn stash(url: Str)  emission fn go(k: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  let store = effect Map.new() undo store.drop()\n"
        "  provide ops {\n"
        "    fn stash(url) { let page = emit fetch(url)  effect store.insert(\"k\", page)  undo store.remove(\"k\") }\n"
        "    fn go(k) { let v = store.get(k)  emit run(v) }\n"
        "  }\n"
        "}\n"
    )
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "state_taint.rvl")
    assert classify(excinfo.value)["code"] == "G9"


def test_a_state_world_only_clean_values_are_written_into_flows_clean():
    """B3 additivity: a state world only clean values are written into stays
    clean — a read of it into a sink is accepted."""
    src = (
        "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
        "extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }\n"
        "service Ops { emission fn stash()  emission fn go(k: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  let store = effect Map.new() undo store.drop()\n"
        "  provide ops {\n"
        "    fn stash() { effect store.insert(\"k\", \"safe\")  undo store.remove(\"k\") }\n"
        "    fn go(k) { let v = store.get(k)  emit run(v) }\n"
        "  }\n"
        "}\n"
    )
    compile_source(src, "clean_state.rvl")  # must not raise


# --- B3: body-local loop back edges ------------------------------------------

def test_a_back_edge_tainted_binding_is_refused_at_a_sink_in_the_loop():
    """B3 back edge: a `var` rebound to an untrusted value at the bottom of a loop
    is tainted on its read at the TOP on the next iteration; the loop fixpoint sees
    it, so the sink refuses (single-pass would have missed it)."""
    src = (
        "extern pure fn danger(cmd: Trusted[Str]) -> Str = @py { return cmd }\n"
        "fn loopy(page: Untrusted[Str]) -> Str {\n"
        "  var acc = \"safe\"\n"
        "  var i = 0\n"
        "  while (i < 1) {\n"
        "    let x = danger(acc)\n"
        "    acc = page\n"
        "    i = i + 1\n"
        "  }\n"
        "  return acc\n"
        "}\n"
    )
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "backedge.rvl")
    assert classify(excinfo.value)["code"] == "G9"


def test_a_loop_with_no_back_edge_taint_converges_and_compiles():
    """The loop fixpoint terminates and stays additive: a loop that never carries
    taint to the sink compiles cleanly (and the compile does not hang)."""
    src = (
        "extern pure fn danger(cmd: Trusted[Str]) -> Str = @py { return cmd }\n"
        "fn loopy(page: Untrusted[Str]) -> Str {\n"
        "  var acc = \"safe\"\n"
        "  var i = 0\n"
        "  while (i < 3) {\n"
        "    let x = danger(acc)\n"
        "    i = i + 1\n"
        "  }\n"
        "  return acc\n"
        "}\n"
    )
    compile_source(src, "loop_clean.rvl")  # must terminate and compile


# --- B4: first-class function values -----------------------------------------

def test_tainted_arg_through_an_unnameable_fn_value_is_refused():
    """B4: a call through a function VALUE the checker cannot name (an arrow-typed
    parameter) with a tainted argument is refused — the over-approximate rule,
    what cannot be named cannot be proven safe."""
    src = (
        "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
        "extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }\n"
        "extern pure fn upper(s: Str) -> Str = @py { return s }\n"
        "fn apply(f: (Str) -> Str, x: Str) -> Str { return f(x) }\n"
        "service Ops { emission fn go(url: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops {\n"
        "    fn go(url) {\n"
        "      let page = emit fetch(url)\n"
        "      let y = apply(upper, page)\n"
        "      emit run(y)\n"
        "    }\n"
        "  }\n"
        "}\n"
    )
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "fnvalue.rvl")
    err = excinfo.value
    assert classify(err)["code"] == "G9"
    assert "unnameable" in err.message or "unnamed" in err.message


def test_a_named_fn_value_carries_its_signature_precisely():
    """B4 precision: `let g = discard` binds a reference to a NAMED fn, so the
    indirect call `g(page)` carries `discard`'s signature — its parameter does not
    flow to the return, so the result is clean and the sink accepts it (no
    over-approximation for a nameable value)."""
    src = (
        "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
        "extern emission[shell] fn run(cmd: Trusted[Str]) = @py { return }\n"
        "fn discard(s: Str) -> Str { return \"safe\" }\n"
        "service Ops { emission fn go(url: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops {\n"
        "    fn go(url) {\n"
        "      let page = emit fetch(url)\n"
        "      let g = discard\n"
        "      let y = g(page)\n"
        "      emit run(y)\n"
        "    }\n"
        "  }\n"
        "}\n"
    )
    compile_source(src, "named_fnvalue.rvl")  # must not raise


def test_an_indirect_call_in_a_sink_free_program_is_not_over_approximated():
    """The over-approximate unnamed-callee rule fires only when the program HAS a
    sink: a program with no sink at all is unaffected by an indirect call, even a
    tainted one (there is nowhere to leak)."""
    src = (
        "extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return \"\" }\n"
        "extern pure fn upper(s: Str) -> Str = @py { return s }\n"
        "fn apply(f: (Str) -> Str, x: Str) -> Str { return f(x) }\n"
        "service Ops { emission fn go(url: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops {\n"
        "    fn go(url) {\n"
        "      let page = emit fetch(url)\n"
        "      let y = apply(upper, page)\n"
        "    }\n"
        "  }\n"
        "}\n"
    )
    compile_source(src, "sinkfree_indirect.rvl")  # must not raise
