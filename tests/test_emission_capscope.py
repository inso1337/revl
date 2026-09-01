"""Capability-scope slot on EMISSION externs — roadmap item 343 (feature half).

`extern emission[gateway.send] fn ...` declares the emission's capability as a
realm-style TOKEN, mirroring the `witnessed[caps]` spelling (item 243). The
policy layer (246 class->approval, 344 standing grants) then keys the emission
on the declared token, not the extern NAME — so a `capability gateway.send
requires approval` rule and a standing `revl_approve` against `gateway.send`
both target the emission by token, and two emissions sharing one token are
governed together. An emission WITHOUT a scope keeps keying on its name, so
every existing program's IR and policy behaviour is byte-identical.
"""

from revl.compiler import compile_source
from revl.mcp.approval import ClassMap

# Two emissions share the realm-style token `gateway.send`; a third, unscoped,
# keeps its name-as-capability behaviour (the byte-compat fallback).
_SOURCE = (
    "extern emission[gateway.send] fn send_email(to: Str) = @py {\n"
    "    return\n"
    "}\n"
    "extern emission[gateway.send] fn send_sms(to: Str) = @py {\n"
    "    return\n"
    "}\n"
    "extern emission fn legacy_ping(to: Str) = @py {\n"
    "    return\n"
    "}\n"
    "service Ops {\n"
    "  emission fn email(to: Str)\n"
    "  emission fn sms(to: Str)\n"
    "  emission fn old(to: Str)\n"
    "}\n"
    "component Agent provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn email(to) { emit send_email(to) }\n"
    "    fn sms(to) { emit send_sms(to) }\n"
    "    fn old(to) { emit legacy_ping(to) }\n"
    "  }\n"
    "}\n"
)


def _ir():
    return compile_source(_SOURCE, "emission_capscope.rvl")


# ---------------------------------------------------------------------------
# 1. the surface: `emission[gateway.send]` parses and reaches the IR
# ---------------------------------------------------------------------------

def test_emission_capability_scope_parses_and_lowers():
    ir = _ir()
    externs = {e["name"]: e for e in ir.get("externs") or []}
    # the scoped emissions carry the declared token in their IR entry
    assert externs["send_email"]["capabilities"] == ["gateway.send"]
    assert externs["send_sms"]["capabilities"] == ["gateway.send"]
    # BYTE-IDENTITY: an unscoped emission carries NO capabilities key at all
    assert "capabilities" not in externs["legacy_ping"]


# ---------------------------------------------------------------------------
# 2. the policy keys on the TOKEN, not the extern name (246 class->approval)
# ---------------------------------------------------------------------------

def test_scoped_emission_is_keyed_by_token_not_name():
    cm = ClassMap(_ir())
    email = cm.classify_call("ops", "email")
    sms = cm.classify_call("ops", "sms")
    assert email["class"] == "c" and sms["class"] == "c"
    # keyed by the declared token...
    assert "gateway.send" in email["capabilities"]
    assert "gateway.send" in sms["capabilities"]
    # ...NOT by the extern name
    assert "send_email" not in email["capabilities"]
    assert "send_sms" not in sms["capabilities"]


def test_unscoped_emission_still_keyed_by_name_byte_compat():
    cm = ClassMap(_ir())
    old = cm.classify_call("ops", "old")
    assert old["class"] == "c"
    # no scope declared -> falls back to the extern name, exactly as today
    assert "legacy_ping" in old["capabilities"]


# ---------------------------------------------------------------------------
# 3. one token governs both emissions through the 344 standing-grant path
# ---------------------------------------------------------------------------

def test_one_token_governs_two_emissions_through_standing_grants():
    cm = ClassMap(_ir())
    # crossings_for_capability is the surface item 344's mint_standing_grant and
    # the operator proactive-mint gate read to target a capability.
    by_token = cm.crossings_for_capability("gateway.send")
    assert by_token, "the token must name a live class-(c) crossing"
    # every returned crossing carries the token in its capability set
    assert all("gateway.send" in c["capabilities"] for c in by_token)
    # the extern NAMES no longer name a crossing (token replaced name)
    assert cm.crossings_for_capability("send_email") == []
    assert cm.crossings_for_capability("send_sms") == []
    # the unscoped emission is still reachable by its name (byte-compat)
    assert cm.crossings_for_capability("legacy_ping")
