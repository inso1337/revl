"""The quarantine tier — a candidate proves itself in the sandbox (item 45,
docs/quarantine-tier.md).

Two layers, mirroring the wasm tier's existing policy (test_canonical_abi.py):

  * the FLOW-LOGIC tests run everywhere, with no toolchain — a rejected
    candidate never reaches the substrate, an aggregate candidate is deferred
    honestly, the admission gate refuses/bypasses per policy + operator, and the
    swap gate is inert without a requiring policy;
  * the SUBSTRATE tests build a real component and run it under wasmtime's
    component model — a clean Str-transform PASSES and a faulting one is TRAPPED
    in the sandbox (proven by an actual trap, not asserted). They skip cleanly
    when wasm-tools/wasmtime are absent, unless REVL_REQUIRE_WASMTIME is set.
"""

import os
import shutil

import pytest

from revl.mcp import quarantine as Q
from revl.mcp.operator import Grant, Operator
from revl.mcp.session import Session
from revl.policy import parse_policy


# --------------------------------------------------------------------------- #
# Fixtures — the three candidate shapes.
# --------------------------------------------------------------------------- #

CLEAN = 'fn tag(s: Str) -> Str { return `[${s}]` }'

# a faulting Str->Str candidate: it overflows Int (i64, overflow *traps* under
# wasm — docs/arithmetic.md) on a value derived from the input length, so the
# overflow is real at runtime (not constant-folded away) and the component
# *traps* when invoked. The trap is the physical confinement.
FAULTING = (
    "fn boom(s: Str) -> Str {\n"
    "  var n = 9223372036854775807\n"   # Int.MAX
    "  n = n + s.length + 1\n"          # overflow -> wasm trap
    "  return `${n}`\n"
    "}"
)

# no Str-surface function — the aggregate follow-on (records/lists/variants
# across the boundary), gated on the parallel canonical work.
AGGREGATE = 'fn add(a: Int, b: Int) -> Int { return a + b }'

# does not compile — admission refuses it before the substrate.
BROKEN = 'fn nope(s: Str) -> Str { return t }'


_REQUIRE = os.environ.get("REVL_REQUIRE_WASMTIME", "").strip().lower() not in (
    "", "0", "false", "no")


def _toolchain() -> bool:
    have = shutil.which("wasm-tools") and (
        shutil.which("wasmtime")
        or os.path.exists(os.path.expanduser("~/.wasmtime/bin/wasmtime")))
    return bool(have)


def _need_toolchain():
    if not _toolchain():
        if _REQUIRE:
            pytest.fail("wasm-tools/wasmtime absent but REVL_REQUIRE_WASMTIME "
                        "is set", pytrace=False)
        pytest.skip("wasm-tools/wasmtime not installed "
                    "(set REVL_REQUIRE_WASMTIME=1 to make this a failure)")


# --------------------------------------------------------------------------- #
# Flow logic — runs everywhere.
# --------------------------------------------------------------------------- #

def test_rejected_candidate_never_reaches_the_substrate():
    report = Q.run(Session(), {"source": BROKEN})
    assert report["verdict"] == "rejected"
    assert report["substrate"]["status"] == "not-run"
    assert report["substrate"]["counts"]["probes"] == 0
    # the gauntlet graded it — admission is refused before anything ran
    assert report["gauntlet"]["verdict"] == "rejected"


def test_aggregate_candidate_is_deferred_honestly():
    report = Q.run(Session(), {"source": AGGREGATE})
    assert report["verdict"] == "deferred"
    assert report["substrate"]["status"] == "deferred"
    # the reason names the aggregate follow-on, not a fake pass
    assert "follow-on" in report["substrate"]["note"]


def test_deferred_needs_no_toolchain():
    # emit_component raises before any binary is invoked, so this holds even on
    # a machine with no wasm toolchain at all.
    report = Q.run(Session(), {"source": AGGREGATE})
    assert report["verdict"] == "deferred"


def test_swap_gate_is_inert_without_a_requiring_policy():
    # no policy bound -> gate_swap returns None (the default path pays nothing).
    assert Q.gate_swap(Session(), {"source": FAULTING}) is None
    # a policy that does not require quarantine is also inert.
    session = Session()
    session.sandbox = parse_policy("component * may reach db")
    assert Q.gate_swap(session, {"source": FAULTING}) is None


def test_policy_parses_quarantine_required_dsl_and_json():
    dsl = parse_policy("quarantine required")
    assert dsl.quarantine_required is True
    assert not dsl.is_empty()
    js = parse_policy('{"quarantine": {"required": true}}')
    assert js.quarantine_required is True
    # absent by default — unchanged behaviour
    assert parse_policy("component * may reach db").quarantine_required is False


# -- the admission gate (item 33 policy + item 55 operator) ------------------

def _decision(verdict, *, policy=None, operator=None):
    session = Session()
    session.sandbox = policy
    session.operator = operator
    return Q.admission_decision(session, {"verdict": verdict, "candidate": {}})


def test_admission_advisory_without_a_requiring_policy():
    d = _decision("trapped")
    assert d["gated"] is False and d["admit"] is True


def test_admission_admits_a_passing_candidate_when_required():
    d = _decision("passed", policy=parse_policy("quarantine required"))
    assert d["gated"] and d["admit"] and not d["bypass"]


def test_admission_refuses_a_trapped_candidate_when_required():
    d = _decision("trapped", policy=parse_policy("quarantine required"))
    assert d["gated"] and d["admit"] is False
    assert "refused" in d["message"]


def test_operator_may_bypass_a_required_quarantine():
    op = Operator("root", (Grant(("quarantine-bypass",), ("*",), True),))
    d = _decision("trapped", policy=parse_policy("quarantine required"),
                  operator=op)
    assert d["admit"] is True and d["bypass"] is True
    assert d["operator"] == "root"


def test_operator_without_the_grant_may_not_bypass():
    op = Operator("reader", (Grant(("swap",), ("*",), True),))
    d = _decision("trapped", policy=parse_policy("quarantine required"),
                  operator=op)
    assert d["admit"] is False


# -- MCP wiring --------------------------------------------------------------

def test_mcp_registers_the_quarantine_verb():
    from revl.mcp import server
    names = {t["name"] for t in server.TOOLS}
    assert "revl_quarantine" in names
    tool = next(t for t in server.TOOLS if t["name"] == "revl_quarantine")
    assert tool["annotations"]["readOnlyHint"] is True


def test_mcp_quarantine_requires_a_candidate():
    from revl.mcp import server
    out = server._tool_quarantine({})
    assert out["ok"] is False


# --------------------------------------------------------------------------- #
# The substrate — a real component run under wasmtime's component model.
# --------------------------------------------------------------------------- #

def test_clean_candidate_passes_in_the_sandbox():
    _need_toolchain()
    report = Q.run(Session(), {"source": CLEAN, "service": "Tagger"})
    assert report["verdict"] == "passed", report["substrate"]
    sub = report["substrate"]
    assert sub["ran"] is True
    assert sub["counts"]["trapped"] == 0
    assert sub["counts"]["returned"] == sub["counts"]["probes"] > 0
    # a nominal probe round-trips the canonical string ABI both directions
    tagged = [p for p in sub["probes"] if p["input"] == "revl"][0]
    assert tagged["result"] == "[revl]"


def test_faulting_candidate_is_trapped_in_the_sandbox():
    """The exit test's teeth: a faulting candidate is TRAPPED — proven by an
    actual wasmtime trap, not asserted. The host is never touched."""
    _need_toolchain()
    report = Q.run(Session(), {"source": FAULTING, "service": "Boomer"})
    assert report["verdict"] == "trapped", report["substrate"]
    sub = report["substrate"]
    assert sub["ran"] is True
    assert sub["counts"]["trapped"] > 0
    assert sub["counts"]["returned"] == 0
    # every recorded trap carries the runtime's own failure detail (proof)
    trap = [p for p in sub["probes"] if p["outcome"] == "trapped"][0]
    assert "trap" in trap and trap["trap"]


def test_quarantine_then_admit_end_to_end():
    """The whole exit test in one flow: a clean candidate under a requiring
    policy is quarantined AND admitted; a faulting one is trapped and REFUSED;
    an operator with bypass authority admits the faulting one anyway."""
    _need_toolchain()
    policy = parse_policy("quarantine required")

    clean_session = Session()
    clean_session.sandbox = policy
    clean = Q.run(clean_session, {"source": CLEAN})
    assert clean["verdict"] == "passed"
    assert clean["admission"]["admit"] is True

    trap_session = Session()
    trap_session.sandbox = policy
    trapped = Q.run(trap_session, {"source": FAULTING})
    assert trapped["verdict"] == "trapped"
    assert trapped["admission"]["admit"] is False

    bypass_session = Session()
    bypass_session.sandbox = policy
    bypass_session.operator = Operator(
        "root", (Grant(("quarantine-bypass",), ("*",), True),))
    bypassed = Q.run(bypass_session, {"source": FAULTING})
    assert bypassed["verdict"] == "trapped"
    assert bypassed["admission"]["admit"] is True
    assert bypassed["admission"]["bypass"] is True


def test_swap_gate_refuses_a_faulting_candidate_under_required_policy():
    """The admission hook in the swap path: a faulting candidate under a
    requiring policy is refused before any hot-swap, the running system
    untouched."""
    _need_toolchain()
    session = Session()
    session.sandbox = parse_policy("quarantine required")
    refusal = Q.gate_swap(session, {"source": FAULTING})
    assert refusal is not None
    assert refusal["swapped"] is False and refusal["admitted"] is False
    assert refusal["quarantine"]["verdict"] == "trapped"


def test_swap_gate_lets_a_clean_candidate_through():
    _need_toolchain()
    session = Session()
    session.sandbox = parse_policy("quarantine required")
    assert Q.gate_swap(session, {"source": CLEAN}) is None
