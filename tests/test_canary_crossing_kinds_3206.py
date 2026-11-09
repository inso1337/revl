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

import inspect
import re

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


def _token_prefixes_in_crossings_source() -> set:
    """The token kinds `audit_diff.crossings` can emit, read from its own
    source: every `out.add(f"<kind>:...")` it makes. An `out.add` written in
    any other shape is a kind this reader cannot name, so it is counted and
    the caller fails rather than skipping it."""
    src = inspect.getsource(audit_diff.crossings)
    kinds = re.findall(r'out\.add\(f"(\w+):', src)
    assert len(kinds) == src.count("out.add("), (
        "audit_diff.crossings adds a token this test cannot read the kind of")
    return set(kinds)


def test_every_crossing_kind_audit_diff_emits_has_a_bucket():
    """The table is complete for every kind `audit_diff.crossings` can emit,
    read from `crossings` itself rather than from a list in this file, so a
    kind added there later reds here until it is mapped."""
    emitted = _token_prefixes_in_crossings_source()
    produced = {t.partition(":")[0]
                for src in {c[1] for c in CASES} | {c[2] for c in CASES}
                for t in crossings(audit_report(_ir(src)))}
    # the control: the source reader names every kind the real compositions
    # in this file make `crossings` produce, so it is reading the right code
    assert produced and produced <= emitted, (produced, emitted)
    assert emitted <= set(canary._CROSSING_BUCKET), (
        emitted - set(canary._CROSSING_BUCKET))


# --------------------------------------------------------------------------
# A tightened `env:` bound promotes; every other bound change still refuses.
# The bound is fingerprinted into the token, so a tightening arrives as one
# added and one removed token for the same field. Read alone the added token
# is a widening, and canary has no accept path, so before the pairing a
# hardened contract could never be promoted.


def _boot_field(name: str, bound: str, typ: str = "Str") -> str:
    body = f"config.{name}" if typ == "Str" else '"constant"'
    return ("service Env { fn data_root() -> Str }\n"
            "boot component HarnessBoot provides env: Env {\n"
            '  isolate env in realm("tenant_a")\n'
            f"  config {{ {name}: {typ}{bound} }}\n"
            f"  provide env {{ fn data_root() = {body} }}\n"
            "}\n")


#: (id, running bound, candidate bound): each candidate admits a subset
TIGHTENINGS = [
    ("unbounded-to-under", "", ' under "./d"'),
    ("under-to-descendant", ' under "./.harness-data"',
     ' under "./.harness-data/sub"'),
    ("in-to-subset", ' in ["a", "b"]', ' in ["a"]'),
]

#: (id, running source, candidate source, the added token that must refuse)
WIDENINGS = [
    ("loosened-under", _boot_field("data_dir", ' under "./.harness-data/sub"'),
     _boot_field("data_dir", ' under "./.harness-data"'),
     "env:data_dir:under=./.harness-data"),
    ("sibling-path", _boot_field("data_dir", ' under "/a/b"'),
     _boot_field("data_dir", ' under "/a/bc"'), "env:data_dir:under=/a/bc"),
    ("dotdot-escape", _boot_field("data_dir", ' under "./d"'),
     _boot_field("data_dir", ' under "./d/../e"'),
     "env:data_dir:under=./d/../e"),
    ("different-field", _boot_field("data_dir", ""),
     _boot_field("other_dir", ' under "./d"'), "env:other_dir:under=./d"),
    ("in-superset", _boot_field("data_dir", ' in ["a"]'),
     _boot_field("data_dir", ' in ["a", "b"]'), "env:data_dir:in=a|b"),
    ("under-to-in", _boot_field("data_dir", ' under "./d"'),
     _boot_field("data_dir", ' in ["./d/x"]'), "env:data_dir:in=./d/x"),
    ("under-to-unbounded", _boot_field("data_dir", ' under "./d"'),
     _boot_field("data_dir", ""), "env:data_dir:*"),
    # the bound narrows but another part of the row moves with it
    ("int-to-float", _boot_field("n", " in [1, 2]", "Int"),
     _boot_field("n", " in [1]", "Float"), "env:n:in=1"),
    ("int-to-bool", _boot_field("n", " in [1, 0]", "Int"),
     _boot_field("n", " in [1]", "Bool"), "env:n:in=1"),
    ("secret-to-plain", _boot_field("data_dir", ' under "./d"', "Secret[Str]"),
     _boot_field("data_dir", ' under "./d/x"'), "env:data_dir:under=./d/x"),
]


@pytest.mark.parametrize("running,candidate", [t[1:] for t in TIGHTENINGS],
                         ids=[t[0] for t in TIGHTENINGS])
def test_a_tightened_env_bound_promotes(running, candidate):
    running = _boot_field("data_dir", running)
    candidate = _boot_field("data_dir", candidate)
    prev, new = audit_report(_ir(running)), audit_report(_ir(candidate))
    # non-vacuity: the pair does change the field's token
    added = sorted(crossings(new) - crossings(prev))
    removed = sorted(crossings(prev) - crossings(new))
    assert len(added) == 1 and added[0].startswith("env:data_dir:")
    assert len(removed) == 1 and removed[0].startswith("env:data_dir:")
    verdict = canary.judge_authority(_ir(running), _ir(candidate))
    assert verdict["ok"] is True, verdict
    assert verdict["moved"] == []
    report = canary.run_canary(_ir(running), candidate_source=candidate,
                               realm="tenant_a", over_the_transport=False)
    assert report["ok"] and report["admitted"], report.get("reason")
    assert report["authority"]["ok"] is True
    assert report["recommendation"] == "promote"


@pytest.mark.parametrize("running,candidate,token", [w[1:] for w in WIDENINGS],
                         ids=[w[0] for w in WIDENINGS])
def test_an_env_bound_change_not_provably_narrower_refuses(running, candidate,
                                                           token):
    prev, new = audit_report(_ir(running)), audit_report(_ir(candidate))
    assert token in crossings(new) - crossings(prev)
    verdict = canary.judge_authority(_ir(running), _ir(candidate))
    assert verdict["ok"] is False
    assert verdict["widened"] is True
    assert verdict["moved"] == ["capability"]
    assert verdict["diff"]["capability"] == [token]
