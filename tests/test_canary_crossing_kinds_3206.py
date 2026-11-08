"""The canary/promote authority barrier sees every crossing kind (issue #3206).

`canary.authority_diff` sorts the crossing tokens `audit_diff.crossings` adds
into drift buckets by prefix. Before this change only `host:` and `emit:` had a
bucket, so a candidate that newly routed web content into a send (`taint:`),
added an `endorse` (`declassify:`), bound a new secret (`secret:`) or added or
loosened an environment contract (`env:`) passed the barrier with
`widened=False`, while `revl audit --diff` on the same pair reported a
widening. Both consumers act on that verdict: `revl canary` went on to
recommend `promote`, and `revl promote` (cli/promote.py) calls the same
`judge_authority` on the two compositions.

Each kind is checked on both entry points: `run_canary` with the candidate as
a provider replacement (the `revl canary` path), and `judge_authority` over two
whole compositions (the call `revl promote` makes). A token kind no bucket
claims is counted on every axis, so a kind added to `crossings` later fails
closed. The no-crossing control keeps the refusals from being satisfiable by
refusing everything.
"""

import pytest

from revl import audit_diff
from revl import promotion_barrier as pb
from revl.audit_diff import audit_report, crossings
from revl.compiler import compile_source
from revl.mcp import canary

EXTERNS = (
    'extern emission[web] fn fetch(url: Str) -> Untrusted[Str] = @py { return "" }\n'
    'extern emission[net] fn log_it(s: Str) = @py { return }\n'
    'extern emission[model.complete] fn complete(p: Str) -> Str = @py { return p }\n'
)


def _agent(body: str, *, endorse: bool = False, secret: bool = False) -> str:
    service = ("service Ops { emission endorse[web] fn go(url: Str) }\n"
               if endorse else "service Ops { emission fn go(url: Str) }\n")
    return (EXTERNS
            + ("secret openai_key for model.complete\n" if secret else "")
            + service
            + "component Agent provides ops: Ops {\n"
            + '  isolate ops in realm("tenant_a")\n'
            + "  provide ops {\n"
            + "    fn go(url) {\n"
            + "      let page = emit fetch(url)\n"
            + body
            + "    }\n  }\n}\n")


AGENT_BASE = _agent('      emit log_it("constant")\n')
AGENT_TAINT = _agent("      emit log_it(page)\n")
AGENT_DECLASSIFY = _agent(
    '      let safe = endorse[web](page, reason = "ack")\n'
    '      emit log_it("constant")\n', endorse=True)
AGENT_SECRET = _agent('      emit log_it("constant")\n', secret=True)


def _boot(bound: str) -> str:
    return ("service Env { fn data_root() -> Str }\n"
            "boot component HarnessBoot provides env: Env {\n"
            '  isolate env in realm("tenant_a")\n'
            f"  config {{ data_dir: Str{bound} }}\n"
            "  provide env { fn data_root() = config.data_dir }\n"
            "}\n")


BOOT_BASE = _boot(' under "./.harness-data"')
BOOT_ENV = _boot("")

#: (kind, running source, candidate source, axis, the one added token)
CASES = [
    ("taint", AGENT_BASE, AGENT_TAINT, "taint", "taint:Agent:web"),
    ("declassify", AGENT_BASE, AGENT_DECLASSIFY, "taint",
     "declassify:Agent:web"),
    ("secret", AGENT_BASE, AGENT_SECRET, "capability",
     "secret:model.complete:openai_key"),
    ("env", BOOT_BASE, BOOT_ENV, "capability", "env:data_dir:*"),
]
IDS = [c[0] for c in CASES]


def _ir(src: str) -> dict:
    return compile_source(src, "app.rvl")


@pytest.mark.parametrize("kind,running,candidate,axis,token", CASES, ids=IDS)
def test_the_audit_gate_sees_exactly_this_one_widening(kind, running,
                                                       candidate, axis, token):
    """The non-vacuity of each pair: the drift gate reports one added token, of
    this kind, and nothing else. A pair that also added a `host:` or `emit:`
    crossing would have been refused on main for that, and would prove
    nothing about the kind under test."""
    prev, new = audit_report(_ir(running)), audit_report(_ir(candidate))
    assert sorted(crossings(new) - crossings(prev)) == [token]
    assert token.startswith(f"{kind}:")


@pytest.mark.parametrize("kind,running,candidate,axis,token", CASES, ids=IDS)
def test_judge_authority_refuses_the_added_crossing(kind, running, candidate,
                                                    axis, token):
    """The call `revl promote` makes on the two compositions."""
    verdict = canary.judge_authority(_ir(running), _ir(candidate))
    assert verdict["ok"] is False
    assert verdict["widened"] is True
    assert verdict["link"] == "authority-widened"
    assert verdict["moved"] == [axis]
    assert verdict["diff"][axis] == [token]
    assert verdict["unmeasured"] == []


@pytest.mark.parametrize("kind,running,candidate,axis,token", CASES, ids=IDS)
def test_revl_canary_refuses_and_never_reads_the_comparison(
        kind, running, candidate, axis, token):
    """The `revl canary` path: the candidate replaces the slice's provider and
    its recorded world is not even compared."""
    report = canary.run_canary(_ir(running), candidate_source=candidate,
                               realm="tenant_a", over_the_transport=False)
    assert report["ok"] and report["admitted"], report.get("reason")
    assert report["recommendation"] == "refuse"
    assert report["authority"]["moved"] == [axis]
    assert report["authority"]["diff"][axis] == [token]
    assert report["divergenceRead"] is False
    assert "divergence" not in report
    rendered = canary.render(report)
    assert "AUTHORITY WIDENED" in rendered
    assert f"+ {axis}: {token}" in rendered


@pytest.mark.parametrize("running", [AGENT_BASE, BOOT_BASE],
                         ids=["agent", "boot"])
def test_a_candidate_adding_no_crossing_still_promotes(running):
    """The control: the same composition as its own candidate adds nothing,
    so the barrier is still and the canary promotes."""
    assert canary.judge_authority(_ir(running), _ir(running))["ok"] is True
    report = canary.run_canary(_ir(running), candidate_source=running,
                               realm="tenant_a", over_the_transport=False)
    assert report["authority"]["ok"] is True
    assert report["authority"]["moved"] == []
    assert report["recommendation"] == "promote"


def test_an_unclaimed_token_kind_fails_closed_on_every_axis(monkeypatch):
    """A crossing kind `audit_diff.crossings` might emit tomorrow. Which axis
    it carries is what nobody knows, so it is counted on every one of them
    rather than dropped or guessed."""
    real = audit_diff.diff_crossings

    def with_future_kind(prev, new):
        delta = real(prev, new)
        delta["added"] = sorted(delta["added"] + ["zzz:foo"])
        return delta

    monkeypatch.setattr(audit_diff, "diff_crossings", with_future_kind)
    ir = _ir(AGENT_BASE)
    verdict = canary.judge_authority(ir, ir)
    assert verdict["ok"] is False
    assert verdict["widened"] is True
    assert verdict["moved"] == list(pb.AUTHORITY_AXES)
    for axis in pb.AUTHORITY_AXES:
        assert verdict["diff"][axis] == ["zzz:foo"]
    assert canary.authority_diff(ir, ir)["buckets"]["unclaimed_added"] == [
        "zzz:foo"]


def test_every_crossing_kind_the_cases_produce_has_a_bucket():
    """The table is complete for every kind this file can make `crossings`
    emit, plus the two it already had."""
    kinds = {"host", "emit"} | {c[0] for c in CASES}
    assert kinds <= set(canary._CROSSING_BUCKET)
