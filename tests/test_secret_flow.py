"""`Secret[T]` information-flow — the confidentiality qualifier (roadmap item
256, Slice 3, docs/design/256-capability-bound-secrets.md §7).

`Secret[T]` is the complement of the bound key: a value the language DOES read
and compute with (a payment token the agent threads between two of its own
emissions), but that the checker keeps out of specific DISCLOSURE sinks — a log,
an ordinary JSON serialization, an LLM prompt, an MCP tool return, an un-approved
realm crossing, or any capability crossing whose receiver does not declare it
accepts a secret. It crosses ONLY at a declared `Secret[T]` receiver and
downgrades ONLY at a declared, audited `endorse[confidential]`.

The load-bearing invariant of this slice is DISJOINTNESS (the CRITICAL 1 fix): a
`Secret[T]` value carries the `confidential` origin, the bound key carries
`secret`, and no sink or declassifier admits both. The A8 regression below is the
direct test of it: a bound-key reflection is admitted at NEITHER a `Secret[T]`
receiver NOR `endorse[secret]`, and a `confidential` value is never admitted by
the bound-key same-capability rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from revl import RevlError
from revl.compiler import compile_source
from revl.diagnostics import classify, explain

ROOT = Path(__file__).resolve().parents[1]

# a `Secret[T]` source: an emission that hands back a confidential payment token.
# Its return is minted `confidential` (§7a), so `let t = charge(u)` puts a
# `confidential`-origin value into the value world — the thing the disclosure
# fence keeps out of every sink below.
_PRELUDE = (
    "extern emission[payment.charge] fn charge(a: Str) -> Secret[Str] "
    "= @py { return a }\n"
    "extern emission[log] fn logit(m: Str) -> Int = @py { return 0 }\n"
    "extern emission[fs.write] fn to_json(m: Str) -> Int = @py { return 0 }\n"
    "extern emission[model.complete] fn prompt(p: Str) -> Str = @py { return p }\n"
)


def _agent(body: str, extra: str = "", sig: str = "fn go(u: Str) -> Int",
           params: str = "u", reqs: str = "") -> str:
    return (
        _PRELUDE + extra
        + "service Ops { emission " + sig + " }\n"
        + "component Agent " + reqs + "provides ops: Ops {\n"
        + "  provide ops {\n"
        + "    fn go(" + params + ") {\n" + body + "\n      return 0\n    }\n"
        + "  }\n}\n"
    )


def _refuses(src: str, filename: str = "flow.rvl") -> RevlError:
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, filename)
    err = excinfo.value
    assert getattr(err, "code", None) == "G-SECRET-FLOW", (
        f"expected G-SECRET-FLOW, got {getattr(err, 'code', None)}: {err.message}")
    return err


def _code_of(src: str) -> str | None:
    try:
        compile_source(src, "flow.rvl")
        return None
    except RevlError as e:
        return getattr(e, "code", None)


# ===========================================================================
# 1. Refused at EACH disclosure sink (§7b).
# ===========================================================================

def test_refused_at_a_log():
    err = _refuses(_agent("      let t = charge(u)\n      let x = logit(t)"))
    assert "disclosure sink" in err.message or "log" in err.message


def test_refused_at_ordinary_json_serialization():
    _refuses(_agent("      let t = charge(u)\n      let x = to_json(t)"))


def test_refused_at_an_llm_prompt():
    """An argument to a `model.*` emission is a disclosure sink — a confidential
    value may not enter the model context."""
    _refuses(_agent("      let t = charge(u)\n      let r = prompt(t)"))


def test_refused_at_an_mcp_tool_return():
    """The value a `provide` method returns crosses the service / MCP bridge to a
    client that never declared a `Secret[T]` receiver — refused at the return."""
    src = (
        _PRELUDE
        + "service Ops { emission fn go(u: Str) -> Str }\n"
        + "component Agent provides ops: Ops {\n  provide ops {\n"
        + "    fn go(u) {\n      return charge(u)\n    }\n  }\n}\n")
    err = _refuses(src)
    assert "MCP tool return" in err.message


def test_refused_at_an_undeclared_receiver_across_a_capability_crossing():
    """A required service operation that does NOT declare a `Secret[T]` parameter
    is an undeclared receiver — the general rule the named sinks instance (§7b)."""
    err = _refuses(_agent(
        "      let t = charge(u)\n      emit snk.out(t)",
        extra="service Sink { emission fn out(s: Str) -> Int }\n",
        reqs="requires snk: Sink "))
    assert "emission crossing" in err.message


def test_refused_at_an_unnameable_first_class_callable():
    """What cannot be named cannot be shown to declare a `Secret[T]` receiver, so
    a confidential argument to it is an undeclared disclosure crossing."""
    err = _refuses(_agent(
        "      let t = charge(u)\n      let y = cb(t)",
        sig="fn go(cb: (Str) -> Int, u: Str) -> Int", params="cb, u"))
    assert "first-class" in err.message


# ===========================================================================
# 2. A declared `Secret[T]` receiver ADMITS the crossing (the one exception).
# ===========================================================================

def test_declared_secret_service_operation_parameter_admits_the_crossing():
    """A `Secret[T]` service-operation parameter is the dual of a `Trusted[T]`
    sink: the ONE crossing that admits a confidential value."""
    src = _agent(
        "      let t = charge(u)\n      emit vault.store(t)",
        extra="service Vault { emission fn store(x: Secret[Str]) -> Int }\n",
        reqs="requires vault: Vault ")
    compile_source(src, "admit.rvl")  # compiles — the receiver declared it


def test_declared_secret_extern_parameter_admits_the_crossing():
    """The same admission for an extern host call that declares a `Secret[T]`
    parameter — the confidential value crosses only where it is declared."""
    src = _agent(
        "      let t = charge(u)\n      let x = vaultput(t)",
        extra="extern emission fn vaultput(x: Secret[Str]) -> Int "
              "= @py { return 0 }\n")
    compile_source(src, "admit2.rvl")


def test_a_non_secret_position_on_a_secret_receiver_still_refuses():
    """Admission is per-position: a `Secret[T]` at param 0 does not admit a
    confidential value handed to param 1 (an ordinary `Str` receiver)."""
    _refuses(_agent(
        "      let t = charge(u)\n      let x = twoarg(\"tag\", t)",
        extra="extern emission fn twoarg(tag: Secret[Str], m: Str) -> Int "
              "= @py { return 0 }\n"))


# ===========================================================================
# 3. A2: a confidential value threaded through a generic stays confidential.
# ===========================================================================

def test_generic_round_trip_does_not_launder_confidential():
    """`id(secret_token)` does not erase the `confidential` qualifier: taint rides
    the VALUE (the inferred `flows_to_return`), not the erased generic type, so the
    downstream log is still refused (the A2 no-launder-through-generic case)."""
    _refuses(_agent(
        "      let t = charge(u)\n      let g = idf(t)\n      let x = logit(g)",
        extra="fn idf(x: Str) -> Str { return x }\n"))


def test_confidential_nested_in_a_record_is_not_laundered():
    """A confidential value nested in a record rides the value-graph joins and is
    caught at whichever crossing the container reaches (§7a / kind-5 analog)."""
    _refuses(_agent(
        "      let t = charge(u)\n      let r = { tok: t, tag: \"x\" }\n"
        "      let z = boxlog(r)",
        extra="type Box = { tok: Str, tag: Str }\n"
              "extern emission[log] fn boxlog(b: Box) -> Int = @py { return 0 }\n"))


# ===========================================================================
# 4. The audited `endorse[confidential]` downgrade (§7c).
# ===========================================================================

_END_DECL = (
    _PRELUDE
    + "service Ops { emission endorse[confidential] fn go(u: Str) -> Int }\n"
    + "component Agent provides ops: Ops {\n  provide ops {\n"
    + "    fn go(u) {\n      let t = charge(u)\n"
    + "      let c = endorse[confidential](t, reason = \"charge settled\")\n"
    + "      let x = logit(c)\n      return 0\n    }\n  }\n}\n")


def test_endorse_confidential_downgrades_and_compiles_with_a_declared_slot():
    """A declared, audited `endorse[confidential]` downgrades the value so a later
    disclosure is admitted — a payment token legitimately becomes a receipt id."""
    compile_source(_END_DECL, "endorse.rvl")  # compiles: downgraded before the log


def test_endorse_confidential_is_recorded_on_the_audit_surface():
    """The downgrade lands on the component's taint surface as a
    `declassify:confidential` token plus an enriched record, so `audit --diff`
    sees it (the drift gate)."""
    ir = compile_source(_END_DECL, "endorse.rvl")
    comp = {c["name"]: c for c in ir["components"]}["Agent"]
    taint = comp.get("taint") or {}
    assert "confidential" in (taint.get("declassify") or [])
    records = taint.get("declassify_records") or []
    assert any(r.get("origin") == "confidential"
               and r.get("reason") == "charge settled" for r in records)


def test_endorse_confidential_is_refused_without_the_declared_slot():
    """Without the declared `endorse[confidential]` slot the downgrade is refused
    at admission — a declassification is never ambient (the Slice C rule)."""
    src = (
        _PRELUDE
        + "service Ops { emission fn go(u: Str) -> Int }\n"
        + "component Agent provides ops: Ops {\n  provide ops {\n"
        + "    fn go(u) {\n      let t = charge(u)\n"
        + "      let c = endorse[confidential](t, reason = \"x\")\n"
        + "      let z = logit(c)\n      return 0\n    }\n  }\n}\n")
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "undeclared.rvl")
    # an undeclared declassification is the general G9 admission refusal
    assert excinfo.value.code == "G9"
    assert "endorse[confidential]" in excinfo.value.message


# ===========================================================================
# 5. A8 (CRITICAL 1): the two origins are DISJOINT. A bound-key reflection is
#    admitted at NEITHER a `Secret[T]` receiver NOR `endorse[secret]`, and a
#    `confidential` value is never admitted by the bound-key rule.
# ===========================================================================

_BOUND_PRELUDE = (
    "secret openai_key for model.complete\n"
    "extern emission[model.complete] fn complete(p: Str) -> Str = @py { return p }\n")


def test_a8_bound_key_is_refused_at_a_secret_receiver():
    """A bound key reflected out of its extern (origin `secret`) passed to a
    `Secret[Str]` receiver is REFUSED with G-SECRET, not admitted — the receiver
    rule admits `confidential` only. This is the direct CRITICAL 1 regression."""
    src = (
        _BOUND_PRELUDE
        + "extern emission fn vaultput(x: Secret[Str]) -> Int = @py { return 0 }\n"
        + "service Ops { emission fn go(u: Str) -> Int }\n"
        + "component Agent provides ops: Ops {\n  provide ops {\n"
        + "    fn go(u) {\n      let s = complete(u)\n"
        + "      let x = vaultput(s)\n      return 0\n    }\n  }\n}\n")
    assert _code_of(src) == "G-SECRET"  # the bound-key refusal, NOT G-SECRET-FLOW


def test_a8_bound_key_is_refused_at_a_secret_service_receiver_via_emit():
    """The same disjointness across an `emit` to a `Secret[Str]` service receiver:
    the bound key is refused (G-SECRET) even though the receiver declares it — the
    admission is for `confidential` alone."""
    src = (
        _BOUND_PRELUDE
        + "service Vault { emission fn store(x: Secret[Str]) -> Int }\n"
        + "service Ops { emission fn go(u: Str) -> Int }\n"
        + "component Agent requires vault: Vault provides ops: Ops {\n"
        + "  provide ops {\n"
        + "    fn go(u) {\n      let s = complete(u)\n"
        + "      emit vault.store(s)\n      return 0\n    }\n  }\n}\n")
    assert _code_of(src) == "G-SECRET"


def test_a8_endorse_secret_stays_refused_unconditionally():
    """`endorse[secret]` is refused with G-SECRET even where the declaration
    declares the slot — the bound key has no declassifier, unlike
    `endorse[confidential]` which the same slot mechanism DOES grant."""
    src = (
        _BOUND_PRELUDE
        + "extern emission fn logit(m: Str) -> Int = @py { return 0 }\n"
        + "service Ops { emission endorse[secret] fn go(u: Str) -> Int }\n"
        + "component Agent provides ops: Ops {\n  provide ops {\n"
        + "    fn go(u) {\n      let s = complete(u)\n"
        + "      let c = endorse[secret](s, reason = \"trust me\")\n"
        + "      let x = logit(c)\n      return 0\n    }\n  }\n}\n")
    assert _code_of(src) == "G-SECRET"


def test_confidential_is_never_admitted_by_the_bound_key_same_capability_rule():
    """The bound-key §4b same-capability re-emission admits a `secret` value back
    into its OWN bound emission; a `confidential` value gets no such pass. Emitting
    a confidential value at the bound emission `complete` (a `model.*` sink, an LLM
    prompt) is refused with G-SECRET-FLOW — the two rules never cross."""
    src = (
        "secret openai_key for model.complete\n"
        + "extern emission[model.complete] fn complete(p: Str) -> Str "
        + "= @py { return p }\n"
        + "extern emission[payment.charge] fn charge(a: Str) -> Secret[Str] "
        + "= @py { return a }\n"
        + "service Ops { emission fn go(u: Str) -> Int }\n"
        + "component Agent provides ops: Ops {\n  provide ops {\n"
        + "    fn go(u) {\n      let t = charge(u)\n"
        + "      let r = complete(t)\n      return 0\n    }\n  }\n}\n")
    assert _code_of(src) == "G-SECRET-FLOW"


# ===========================================================================
# 6. Slice 1 (`secret`) behavior is fully preserved, and the diagnostic is
#    registered.
# ===========================================================================

def test_g_secret_flow_is_a_registered_diagnostic():
    record = explain("G-SECRET-FLOW")
    assert record["ok"] and record["guarantee"] and record["fix"]
    err = _refuses(_agent("      let t = charge(u)\n      let x = logit(t)"))
    assert classify(err)["code"] == "G-SECRET-FLOW"


def test_a_secret_free_confidential_free_program_is_byte_identical():
    """A program using neither `secret` nor `Secret[T]` engages nothing new: no
    confidential source, no receiver, so the flow walk stays inert."""
    src = (
        "extern emission[net.send] fn send(m: Str) -> Int = @py { return 0 }\n"
        "service Ops { emission fn go(u: Str) -> Int }\n"
        "component A provides ops: Ops {\n  provide ops {\n"
        "    fn go(u) {\n      let n = send(u)\n      return 0\n    }\n  }\n}\n")
    ir = compile_source(src, "free.rvl")
    comp = {c["name"]: c for c in ir["components"]}["A"]
    assert "taint" not in comp  # no taint surface touched
