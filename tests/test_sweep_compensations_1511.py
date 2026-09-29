"""The cross-tier sweep checks compensations, not only residue (issue #1511).

WHAT WAS WRONG. `revl test --sweep --backend all` compared the tiers on one
thing: the runtime's own no-residue proof. A compensation offsets an emission
at the far side of a boundary, so the runtime is exactly as clean whether it
ran or not. go, rust and java never ran an extern-declared compensation, and
the sweep still printed

    AGREEMENT — 4 tiers (py, rust, java, go) agree on 4 shared fault point(s):
    residue-free on every tier

It read an absence as agreement.

WHAT IT DOES NOW. At every fault point the sweep works out, from the IR, which
compensations the fault owes (site-spelled `compensate` and extern-declared
`compensate` on the faulted component's steps so far), and observes which ones
ran: each compensation extern's host body prints a marker first, on every
tier. A tier that ran anything other than the owed list, newest first, is
DIVERGED. A tier that ran none is the case this exists for, and it is never
counted as agreeing. The ts `--once` runner also stops treating a faulting
activation as fatal, so the ts tier is measured at all (it was skipped).

The pure tests need no runtime. The executed ones are gated on each tier's
toolchain, and a tier that still drops the declared compensation is an
`xfail(strict=True)`: when its lane fixes it, the test XPASSes, fails, and the
marker has to come off.
"""

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import fault as fault_mod  # noqa: E402


def _bodies(py: str, ts: str, go: str, java: str, rs: str) -> str:
    return (f"  = @py {{ {py} }}\n  = @ts {{ {ts} }}\n  = @go {{ {go} }}\n"
            f"  = @java {{ {java} }}\n  = @rs {{ {rs} }}\n")


_UNIT = _bodies("return None", "return", "return", "return;", "()")
_ONE = _bodies("return 1", "return 1n", "return 1", "return 1L;", "1")

#: An activation body that crosses an extern DECLARING its compensation, then
#: a site-spelled one. `Pinger` is there because the go runner needs a service
#: to bridge.
DECLARED = (
    "extern pure fn undo_act()\n" + _UNIT
    + "extern emission fn put_act(k: Str) -> Int compensate undo_act()\n" + _ONE
    + "extern emission fn plain(k: Str) -> Int\n" + _ONE
    + "extern pure fn undo_site()\n" + _UNIT
    + "service Ping { fn ping() -> Int }\n"
    "component Pinger provides p: Ping {\n  provide p { fn ping() = 1 }\n}\n"
    "component Agent {\n"
    '  emit put_act("k")\n'
    '  emit plain("k") compensate undo_site()\n'
    "}\n"
)


def _declared() -> dict:
    return compile_source(DECLARED, "declared_1511.rvl")


def _unit(component: str, step: int) -> dict:
    return {"component": component, "step": step,
            "where": f"step {step}", "name": f"sweep {component} @ step {step}"}


# ------------------------------------------------------------------ owed


def test_owed_reads_declared_and_site_compensations_in_registration_order():
    ir = _declared()
    assert fault_mod._owed_compensations(ir, "Agent", 1) == [
        {"name": "undo_act", "declared": True}]
    assert fault_mod._owed_compensations(ir, "Agent", 2) == [
        {"name": "undo_act", "declared": True},
        {"name": "undo_site", "declared": False}]
    # a component with no compensating emission owes nothing
    assert fault_mod._owed_compensations(ir, "Pinger", 1) == []


def test_instrumenting_marks_only_the_compensation_externs_on_one_tier():
    ir = _declared()
    before = {e["name"]: dict(e["bodies"]) for e in ir["externs"]}
    marked = fault_mod._instrument_compensations(ir, "ts")
    bodies = {e["name"]: e["bodies"] for e in marked["externs"]}
    mark = fault_mod._COMPENSATION_MARK
    for name in ("undo_act", "undo_site"):
        assert bodies[name]["ts"].startswith(f'console.log("{mark} {name}")\n')
        assert bodies[name]["py"] == before[name]["py"]   # other tiers untouched
    for name in ("put_act", "plain"):
        assert bodies[name] == before[name]
    # the input IR is not mutated
    assert {e["name"]: e["bodies"] for e in ir["externs"]} == before


def test_every_instrumentable_tier_gets_a_marker():
    ir = _declared()
    for tier in ("py", "ts", "go", "java", "rust"):
        key = fault_mod._BODY_KEY[tier]
        marked = fault_mod._instrument_compensations(ir, tier)
        body = next(e for e in marked["externs"] if e["name"] == "undo_act")["bodies"][key]
        assert f"{fault_mod._COMPENSATION_MARK} undo_act" in body, tier


# ------------------------------------------------------------------ the check


def _out(*names: str) -> str:
    return "\n".join(["[run] UP"] + [f"{fault_mod._COMPENSATION_MARK} {n}" for n in names]
                     + ["[run] NO-RESIDUE", "[run] DOWN"])


def test_a_tier_that_ran_no_compensation_diverges():
    check = fault_mod._compensation_check(_declared(), "go", _unit("Agent", 2), _out())
    assert check["expected"] == ["undo_site", "undo_act"]
    assert check["ran"] == []
    assert check["diverged"] is True


def test_the_declared_compensation_missing_alone_diverges():
    """The shape measured on go, rust and java: the site-spelled compensation
    runs and the declared one does not."""
    check = fault_mod._compensation_check(
        _declared(), "go", _unit("Agent", 2), _out("undo_site"))
    assert check["diverged"] is True
    assert check["declared"] == ["undo_act"]


def test_the_owed_compensations_newest_first_agree():
    check = fault_mod._compensation_check(
        _declared(), "go", _unit("Agent", 2), _out("undo_site", "undo_act"))
    assert check["diverged"] is False


def test_the_wrong_order_diverges():
    check = fault_mod._compensation_check(
        _declared(), "go", _unit("Agent", 2), _out("undo_act", "undo_site"))
    assert check["diverged"] is True


def test_an_offset_nobody_owed_diverges():
    """Faulting `Pinger` owes nothing; a compensation that ran anyway (an
    offset fired on a clean teardown) is a divergence too."""
    check = fault_mod._compensation_check(
        _declared(), "go", _unit("Pinger", 1), _out("undo_site"))
    assert check["diverged"] is True


def test_a_compensation_that_is_not_an_extern_is_named_unobserved():
    ir = _declared()
    for ext in ir["externs"]:
        if ext["name"] == "undo_site":
            del ext["bodies"]["go"]
    check = fault_mod._compensation_check(ir, "go", _unit("Agent", 2), _out("undo_act"))
    assert check["unobserved"] == ["undo_site"]
    assert check["expected"] == ["undo_act"]
    assert check["diverged"] is False


# ------------------------------------------------------------------ aggregation


def _pt(where: str, status: str) -> dict:
    return {"where": where, "component": "Agent", "status": status}


def _tier(tier: str, status: str, points: list, reason: str = "") -> dict:
    return {"tier": tier, "status": status, "points": points, "reason": reason}


def _claims_agreement(out: str) -> bool:
    return any(line.startswith("AGREEMENT") for line in out.splitlines())


def test_every_tier_diverging_is_a_failure_never_agreement(capsys):
    """Every tier ran none: all the tiers "agree" with one another, and every
    one of them is wrong. That is the exact shape the old sweep printed as
    AGREEMENT."""
    records = [_tier(t, "diverged", [_pt("step 1", "diverged")], "ran none")
               for t in ("py", "go", "rust")]
    dossier = fault_mod._cross_tier_dossier(_declared(), records, cap=None)
    assert dossier["status"] == "failed"
    assert dossier["agree"] is False
    assert dossier["counts"]["tiersDiverging"] == 3
    assert dossier["counts"]["executed"] == 0
    fault_mod._format_cross_tier(dossier, print)
    out = capsys.readouterr().out
    assert not _claims_agreement(out)
    assert "DIVERGENCE: py, go, rust" in out


def test_a_clean_tier_and_a_diverged_tier_disagree_at_the_point(capsys):
    records = [_tier("py", "executed", [_pt("step 1", "clean")]),
               _tier("go", "diverged", [_pt("step 1", "diverged")], "ran none")]
    dossier = fault_mod._cross_tier_dossier(_declared(), records, cap=None)
    assert dossier["status"] == "failed"
    assert dossier["agreement"]["disagreements"] == [
        {"component": "Agent", "where": "step 1",
         "verdicts": {"py": "clean", "go": "diverged"}}]
    fault_mod._format_cross_tier(dossier, print)
    out = capsys.readouterr().out
    assert "go    DIVERGED - ran none" in out
    assert not _claims_agreement(out)


def test_a_disagreement_is_never_also_printed_as_agreement(capsys):
    """The old formatter printed DISAGREEMENT and then AGREEMENT for the same
    run whenever two tiers executed."""
    records = [_tier("py", "executed", [_pt("step 1", "clean")]),
               _tier("go", "executed", [_pt("step 1", "residue")])]
    dossier = fault_mod._cross_tier_dossier(_declared(), records, cap=None)
    fault_mod._format_cross_tier(dossier, print)
    out = capsys.readouterr().out
    assert "DISAGREEMENT" in out
    assert not _claims_agreement(out)


def test_the_same_label_in_two_components_is_two_points():
    records = [
        _tier("py", "executed", [{"where": "step 1 (emit)", "component": "A", "status": "clean"},
                                 {"where": "step 1 (emit)", "component": "B", "status": "clean"}]),
        _tier("go", "executed", [{"where": "step 1 (emit)", "component": "A", "status": "clean"},
                                 {"where": "step 1 (emit)", "component": "B", "status": "clean"}]),
    ]
    dossier = fault_mod._cross_tier_dossier(_declared(), records, cap=None)
    assert dossier["agreement"]["points"] == 2


# ------------------------------------------------------------------ executed


needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="cordis-py runtime not installed (sh backends/python/setup.sh)")


def _tier_reason(tier: str):
    _runner, reason = fault_mod._once_runner(tier)
    return reason()


def _needs(tier: str):
    if tier == "go" and shutil.which("go") is None:
        return pytest.mark.skipif(True, reason="go not installed")
    reason = _tier_reason(tier)
    return pytest.mark.skipif(reason is not None, reason=f"{tier}: {reason}")


def _still_drops(tier: str):
    return pytest.mark.xfail(
        strict=True,
        reason=f"issue #1511: the {tier} tier does not register an "
               "extern-declared compensation; remove this marker with the fix")


@needs_cordis
def test_the_py_tier_runs_every_declared_compensation_newest_first():
    record = fault_mod._py_tier_sweep(_declared())
    assert record["status"] == "executed", record["reason"]
    agent = {p["where"]: p["compensations"] for p in record["points"]
             if p["component"] == "Agent"}
    assert agent["step 1 (emit)"]["ran"] == ["undo_act"]
    assert agent["step 2 (emit)"]["ran"] == ["undo_site", "undo_act"]


@pytest.mark.parametrize("tier", [
    pytest.param("ts", marks=[_needs("ts")]),
    pytest.param("go", marks=[_needs("go")]),
    pytest.param("java", marks=[_needs("java")]),
    pytest.param("rust", marks=[_needs("rust"), _still_drops("rust")]),
])
def test_a_compiled_tier_runs_every_declared_compensation(tier):
    record = fault_mod._compiled_tier_sweep(tier, _declared(), {}, [], None)
    assert record["status"] == "executed", record["reason"]


@pytest.mark.parametrize("tier", ["ts", "go", "java", "rust"])
def test_a_tier_that_drops_it_is_reported_as_diverged_not_clean(tier):
    """The instrument itself, on the real runners: whatever a tier does, its
    record carries the observed list, and a tier whose list differs from the
    owed one is never `executed`."""
    if tier == "go" and shutil.which("go") is None:
        pytest.skip("go not installed")
    reason = _tier_reason(tier)
    if reason is not None:
        pytest.skip(f"{tier}: {reason}")
    record = fault_mod._compiled_tier_sweep(tier, _declared(), {}, [], None)
    assert record["status"] in ("executed", "diverged"), record["reason"]
    agent = [p for p in record["points"] if p["component"] == "Agent"]
    assert len(agent) == 2
    for point in agent:
        check = point["compensations"]
        assert point["status"] == ("diverged" if check["ran"] != check["expected"] else "clean")
    step2 = next(p for p in agent if p["where"] == "step 2 (emit)")
    # the site-spelled compensation runs on every tier; it is the control
    assert "undo_site" in step2["compensations"]["ran"]
