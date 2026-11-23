"""The evaluator as a capability (issue #2802, slice 3).

Slices 1 and 2 put the judge of the evolution loop on ONE enumeration
(`kernel_boundary.EVALUATOR_PATHS`) and gave every scorecard and verdict a
digest of it. Both are answered against a changed-file set. This slice is the
other half, the one item 544 gave the admission kernel: a component that is
composed for another consumer and DECLARES a reach into the evaluator is
refused by that declaration, not only by its diff.

Four things are held here, in this order because each is a precondition of the
next:

1. THE ENUMERATION IS REAL. Every `EvaluatorCap` path is an `EVALUATOR_PATHS`
   entry and exists in the tree, every evaluator path that is not a kernel path
   is stood for by exactly one member, and no path is both. Two lists that agree
   today are still two lists.
2. NO NEW GUARANTEE CODE. Every member cites a code `diagnostics.GUARANTEES`
   already carries, so the generated tier matrix gains no row.
3. NON-VACUITY, MEASURED. The same corpus is compiled twice: once with
   `lower._check_evaluator_boundary` neutralised (the tree WITHOUT this change)
   and once live. Every program is admitted in the first pass and refused in the
   second, and a control whose declared set is disjoint from the evaluator's is
   admitted in BOTH.
4. THE FAILURE DIRECTION. A claim on the namespace the enumeration has no member
   for is refused rather than read as an ordinary boundary, a declared claim is
   refused on both sides of the trust boundary, and a reach folded in from item
   519's product record is read the way the kernel's is.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from revl import cap_order, kernel_boundary as kb, lower
from revl.admit_profile import AdmissionProfile
from revl.compiler import compile_source
from revl.diagnostics import GUARANTEES
from revl.errors import RevlError
from revl.parser import Parser

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------- corpus

_TASK = "service Task { emission fn go() -> Int }\n"


def _reaching(token: str) -> str:
    """A component that reaches one declared capability token."""
    return _TASK + f"""
service Boundary {{
  emission[{token}] fn cross(row: Str) -> Int
}}

component Candidate requires b: Boundary provides task: Task {{
  provide task {{
    fn go() {{
      emit b.cross("x")
      return 0
    }}
  }}
}}
"""


#: One program per enumerated member, plus one naming a member the enumeration
#: does not carry and one naming the bare namespace. Each is admitted without the
#: check and refused with it.
EVALUATOR_PROGRAMS = {c.token: _reaching(c.token) for c in kb.EVALUATOR_CAPS}
EVALUATOR_PROGRAMS["evaluator.not_enumerated"] = _reaching(
    "evaluator.not_enumerated")
EVALUATOR_PROGRAMS["evaluator"] = _reaching("evaluator")

#: The control: a declared set disjoint from every evaluator member. Admitted on
#: both sides of the change, which is what makes the refusals above evidence of a
#: boundary and not of a broken compiler.
CONTROL = _reaching("kv.write")


@pytest.fixture
def without_the_check(monkeypatch):
    """The tree as it was before this slice: the evaluator fold does not run."""
    monkeypatch.setattr(lower, "_check_evaluator_boundary",
                        lambda *args, **kwargs: [])


# ------------------------------------------------------- 1. the enumeration

def test_every_evaluator_member_path_is_an_evaluator_path_that_exists():
    stray = sorted({p for c in kb.EVALUATOR_CAPS for p in c.paths}
                   - set(kb.EVALUATOR_PATHS))
    assert not stray, (
        f"evaluator capability members stand for {stray}, which "
        f"EVALUATOR_PATHS does not enumerate. One list, or there are two lists.")
    missing = [p for c in kb.EVALUATOR_CAPS for p in c.paths
               if not (ROOT / p).exists()]
    assert not missing, (
        f"evaluator members name {missing}, which is not in the tree. An "
        f"enumeration that protects a path nothing writes to protects nothing.")


def test_every_non_kernel_evaluator_path_is_stood_for_by_exactly_one_member():
    kernel = set(kb.KERNEL_PATHS)
    owners: dict[str, list[str]] = {}
    for member in kb.EVALUATOR_CAPS:
        for path in member.paths:
            owners.setdefault(path, []).append(member.token)
    unowned = [p for p in kb.EVALUATOR_PATHS
               if p not in kernel and p not in owners]
    assert not unowned, (
        f"EVALUATOR_PATHS enumerates {unowned}, which no evaluator capability "
        f"stands for, so a component could reach it with nothing to refuse.")
    twice = {p: t for p, t in owners.items() if len(t) > 1}
    assert not twice, f"a path with two capabilities is two refusals: {twice}"


def test_no_path_is_both_a_kernel_member_and_an_evaluator_member():
    """One path has one capability, so a reader never has to decide which of two
    refusals names it. The census tool and its baseline are `kernel.census`;
    this file is `kernel.admission`."""
    kernel = {p for c in kb.KERNEL_CAPS for p in c.paths}
    both = sorted({p for c in kb.EVALUATOR_CAPS for p in c.paths} & kernel)
    assert not both, both


def test_every_member_token_is_in_the_reserved_namespace_and_unique():
    tokens = [c.token for c in kb.EVALUATOR_CAPS]
    assert len(tokens) == len(set(tokens))
    for token in tokens:
        assert kb.is_evaluator_token(token), token
        assert not kb.is_kernel_token(token), token


def test_the_two_namespaces_do_not_read_each_other():
    assert not kb.is_evaluator_token("kernel.admission")
    assert not kb.is_kernel_token("evaluator.reward")
    # and a namespace is a head, not a substring
    assert not kb.is_evaluator_token("evaluators")
    assert not kb.is_evaluator_token("my.evaluator")


# ------------------------------------------------------ 2. no new guarantee

def test_every_cited_guarantee_is_already_registered():
    for member in kb.EVALUATOR_CAPS:
        assert member.guarantee in GUARANTEES, member.guarantee


# ---------------------------------------------------------- 3. non-vacuity

@pytest.mark.parametrize("token", sorted(EVALUATOR_PROGRAMS))
def test_an_evaluator_reaching_component_is_admitted_without_the_check(
        token, without_the_check):
    compile_source(EVALUATOR_PROGRAMS[token], "candidate.rvl")


@pytest.mark.parametrize("token", sorted(EVALUATOR_PROGRAMS))
def test_an_evaluator_reaching_component_is_refused_with_it(token):
    with pytest.raises(RevlError) as excinfo:
        compile_source(EVALUATOR_PROGRAMS[token], "candidate.rvl")
    error = excinfo.value
    assert token in error.message or token in (error.hint or ""), error.message
    assert error.code == "G8"
    if token in kb.EVALUATOR_BY_TOKEN:
        member = kb.EVALUATOR_BY_TOKEN[token]
        assert "inside the evaluator's authority" in error.message
        assert member.why.split(";")[0] in (error.hint or "")
    else:
        assert "enumeration has no member for" in error.message


def test_the_control_is_admitted_on_both_sides(without_the_check):
    compile_source(CONTROL, "control.rvl")


def test_the_control_is_admitted_with_the_check_live():
    compile_source(CONTROL, "control.rvl")


def test_the_refusal_names_the_tree_the_authority_is_over():
    with pytest.raises(RevlError) as excinfo:
        compile_source(EVALUATOR_PROGRAMS["evaluator.reward"], "candidate.rvl")
    hint = excinfo.value.hint or ""
    assert "tools/evolution_reward.py" in hint
    assert "tests/test_evolution_reward.py" in hint


def test_the_refusal_names_what_the_component_actually_holds():
    with pytest.raises(RevlError) as excinfo:
        compile_source(EVALUATOR_PROGRAMS["evaluator.heldout"], "candidate.rvl")
    assert "`evaluator.heldout`" in (excinfo.value.hint or "")


def test_the_refusal_names_the_enumeration_for_an_unknown_member():
    with pytest.raises(RevlError) as excinfo:
        compile_source(EVALUATOR_PROGRAMS["evaluator.not_enumerated"],
                       "candidate.rvl")
    hint = excinfo.value.hint or ""
    for member in kb.EVALUATOR_CAPS:
        assert f"`{member.token}`" in hint, member.token


def test_the_refusal_is_the_same_on_both_sides_of_the_trust_boundary():
    """A DECLARED evaluator token is refused for the first-party tree and for a
    candidate admitted under the untrusted-author profile alike. The evaluator is
    the judge, never the subject, so there is no first-party exemption to the
    declared reach (the kernel namespace reads it the same way)."""
    source = EVALUATOR_PROGRAMS["evaluator.controller"]
    with pytest.raises(RevlError) as first_party:
        compile_source(source, "composition.rvl")
    profile = AdmissionProfile.untrusted_author({"Boundary", "Task"})
    with pytest.raises(RevlError) as untrusted:
        compile_source(source, "candidate.rvl", profile=profile)
    for excinfo in (first_party, untrusted):
        assert excinfo.value.code == "G8"
        assert "inside the evaluator's authority" in excinfo.value.message


def test_a_component_holding_a_kernel_token_is_still_refused_as_the_kernel():
    """The two namespaces are two refusals. A kernel token is not re-read as an
    evaluator claim, and the evaluator fold does not run in the kernel's place."""
    with pytest.raises(RevlError) as excinfo:
        compile_source(_reaching("kernel.attest"), "candidate.rvl")
    assert "admission kernel's authority" in excinfo.value.message


def test_no_shipped_example_spells_the_reserved_namespace():
    """The namespace is reserved, so no program on the tree may already use it.
    Held on the shipped examples: not one of them spells it, which is what makes
    the refusal a no-op for them. The inertness of the check itself (no pass, no
    record) is `test_the_gate_is_closed_when_nothing_spells_the_namespace`."""
    for path in sorted((ROOT / "examples").glob("*.rvl")):
        assert "evaluator." not in path.read_text(encoding="utf-8"), path


# ------------------------------------------------- 4. the failure direction

def test_an_unenumerated_claim_has_no_member_to_fold_against():
    """`cap_order.disjoint` treats two distinct tokens as disjoint, so a bare
    `evaluator` or an `evaluator.something_new` would slip through the fold. The
    enumeration check is what catches them, and it is a separate arm."""
    for token in ("evaluator", "evaluator.something_new"):
        cap = {cap_order.parse_cap(token)}
        assert not kb.evaluator_offending(cap), token
        assert [c.token for c in kb.unenumerated_evaluator(cap)] == [token]


def test_a_declared_evaluator_reach_meets_the_evaluator():
    held = {cap_order.parse_cap("evaluator.progress")}
    hits = kb.evaluator_offending(held)
    assert [m.token for m, _c in hits] == ["evaluator.progress"]
    assert not kb.unenumerated_evaluator(held)


def test_a_declared_narrow_reach_is_provably_outside_the_evaluator():
    narrow = {cap_order.parse_cap('fs.write(path="/tmp")')}
    assert not kb.evaluator_offending(narrow)
    assert not kb.unenumerated_evaluator(narrow)


def test_the_unnameable_star_is_not_read_here_and_the_kernel_still_refuses_it():
    """The undeclared reaches are the kernel arm's, not this one's. Folding `*`
    here as well would produce a second refusal for the same reach, naming the
    evaluator where the kernel is already the stronger statement."""
    star = {cap_order.Cap("*", ())}
    assert not kb.evaluator_offending(star)
    assert not kb.unenumerated_evaluator(star)
    assert kb.offending(star, undeclared_reaches_kernel=True)


# ------------------------- 4b. what item 519's product record contributes

def _row(**overrides) -> dict:
    """One `manifest["model_reach"]` row, as `lower._check_model_attenuation`
    writes it (item 519)."""
    row = {
        "component": "Candidate", "action": "classify", "origin": "public",
        "role": "planner", "residence": "off_device",
        "holds": ["kv.write"], "reaches": ["kv.write"], "effective": ["kv.write"],
        "attenuated": [], "reach_declared": True,
    }
    row.update(overrides)
    return row


def _compiled(source: str):
    """`(components, services)` exactly as the check receives them."""
    ir = compile_source(source, "composition.rvl")
    services = {s.name: s for s in Parser(source, "composition.rvl").parse().services}
    return ir["components"], services


def test_a_model_reach_that_meets_the_evaluator_refuses_the_component():
    """Wired through `lower`, not only the helper in isolation. The row is
    injected at the one seam item 519 fills, as the kernel's own end-to-end test
    does, and the gate that decides whether the fold runs must read it."""
    components, services = _compiled(CONTROL)
    manifest = {"model_reach": [_row(effective=["evaluator.reward"])]}
    with pytest.raises(RevlError) as excinfo:
        lower._check_evaluator_boundary(components, services, "composition.rvl",
                                        manifest=manifest)
    assert excinfo.value.code == "G8"
    assert "`evaluator.reward`" in excinfo.value.message


def test_a_model_reach_that_stays_outside_the_evaluator_is_recorded():
    components, services = _compiled(CONTROL)
    manifest = {"model_reach": [_row(effective=["kv.write"])]}
    assert lower._check_evaluator_boundary(
        components, services, "composition.rvl", manifest=manifest) == []


def test_a_model_reach_row_for_another_component_is_not_folded_in():
    """The row names a different component, so this component's ceiling does not
    include it. The gate opens because a row spells the namespace; the per
    component fold is what decides who holds it."""
    components, services = _compiled(CONTROL)
    manifest = {"model_reach": [
        _row(component="Other", effective=["evaluator.reward"])]}
    record = lower._check_evaluator_boundary(
        components, services, "composition.rvl", manifest=manifest)
    assert [r["component"] for r in record] == ["Candidate"]
    assert all(r["disjoint"] for r in record)


def test_the_gate_is_closed_when_nothing_spells_the_namespace():
    components, services = _compiled(CONTROL)
    assert lower._check_evaluator_boundary(
        components, services, "composition.rvl", manifest=None) == []
    assert lower._check_evaluator_boundary(
        components, services, "composition.rvl",
        manifest={"model_reach": [_row()]}) == []


# --------------------------------------- 5. one enumeration, three consumers

def test_the_namespace_reads_the_same_enumeration_the_fences_are_views_of():
    """The kernel's members and the evaluator's members together cover exactly
    `EVALUATOR_PATHS`: there is no second list beside it. The per-path checks
    above say which path is owned by whom; this is the whole-set statement, so
    the capability cannot silently fall behind the diff check."""
    assert set(p for c in kb.EVALUATOR_CAPS for p in c.paths) \
        | set(kb.KERNEL_PATHS) == set(kb.EVALUATOR_PATHS)
