"""The admission kernel as a capability (roadmap item 544, issue #1223).

Four things are held here, in this order because each is a precondition of the
next:

1. THE ENUMERATION IS REAL. Every `KERNEL_PATHS` entry exists in the tree, and
   every `KernelCap.paths` entry is one of them. An enumeration naming a file
   that is not there protects nothing, and a member standing for a path outside
   the enumeration means there are two lists again.
2. NO NEW GUARANTEE CODE. Every guarantee a refusal cites is already in
   `diagnostics.GUARANTEES`, so `tools/tier_guarantees.py`'s generated matrix
   gains no row and needs no `ACKNOWLEDGED` entry.
3. NON-VACUITY, MEASURED. The same corpus is compiled twice: once with
   `lower._check_kernel_boundary` neutralised (which is the tree WITHOUT this
   change) and once with it live. Every kernel program is admitted in the first
   pass and refused in the second, and a control whose declared capability set
   is disjoint from the kernel's is admitted in BOTH.
4. THE FAILURE DIRECTION. An unnameable reach is not an empty one, and an
   omitted `reaches [...]` clause on an authority surrogate is not a proof of
   narrowness (item 519, PR #1253). Both resolve to `*`, which is disjoint from
   nothing.

The residual this slice does NOT refuse is pinned too (`test_the_key_namespaced
_residual_is_still_admitted`), because a gap that is written down stays visible
and a gap that is assumed away does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from revl import cap_order, kernel_boundary as kb, lower
from revl.admit_profile import AdmissionProfile
from revl.compiler import compile_source
from revl.diagnostics import GUARANTEES
from revl.errors import RevlError

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


#: One program per enumerated kernel member, plus one naming the namespace with
#: a member the enumeration does not carry. Each is admitted without the check
#: and refused with it; the assertions are in `test_non_vacuity_*` below.
KERNEL_PROGRAMS = {c.token: _reaching(c.token) for c in kb.KERNEL_CAPS}
KERNEL_PROGRAMS["kernel.not_enumerated"] = _reaching("kernel.not_enumerated")

#: The control: a declared set that is disjoint from every kernel member.
#: Admitted on both sides of the change, which is what makes the refusals above
#: evidence of a boundary rather than of a broken compiler.
CONTROL = _reaching("kv.write")


@pytest.fixture
def without_the_check(monkeypatch):
    """The tree as it was before item 544: the fold does not run."""
    monkeypatch.setattr(lower, "_check_kernel_boundary",
                        lambda *args, **kwargs: [])


# ------------------------------------------------------- 1. the enumeration

def test_every_kernel_path_exists_in_the_tree():
    root = ROOT
    missing = [p for p in kb.KERNEL_PATHS if not (root / p).exists()]
    assert not missing, (
        f"KERNEL_PATHS names {missing}, which is not in the tree. An "
        f"enumeration that protects a path nothing writes to protects nothing.")


def test_every_member_path_is_an_enumerated_kernel_path():
    stray = sorted({p for c in kb.KERNEL_CAPS for p in c.paths}
                   - set(kb.KERNEL_PATHS))
    assert not stray, (
        f"kernel capability members stand for {stray}, which KERNEL_PATHS does "
        f"not enumerate. One list, or there are two lists.")


def test_every_kernel_path_is_stood_for_by_some_member():
    covered = {p for c in kb.KERNEL_CAPS for p in c.paths}
    orphan = [p for p in kb.KERNEL_PATHS if p not in covered]
    assert not orphan, (
        f"KERNEL_PATHS enumerates {orphan}, which no capability member stands "
        f"for, so a component could reach it with nothing to refuse.")


def test_every_member_token_is_in_the_reserved_namespace():
    for member in kb.KERNEL_CAPS:
        assert kb.is_kernel_token(member.token), member.token


def test_retention_is_on_the_kernel_side():
    """The one the wave filed on the wrong side (issue #1223)."""
    assert "kernel.retention" in kb.BY_TOKEN
    assert kb.RETENTION.guarantee == "G-RETAIN"
    assert "src/revl/retention.py" in kb.RETENTION.paths


# ------------------------------------------------------ 2. no new guarantee

def test_every_cited_guarantee_is_already_registered():
    """No new code, so item 523's generated tier matrix gains no row.

    Registering a code with no reproducer under `examples/rejections/` and no
    `ACKNOWLEDGED` entry fails matrix generation, which reds `main`. This slice
    cites `G8`, `G9` and `G-RETAIN`, all of which the tree already carries."""
    for member in kb.KERNEL_CAPS:
        assert member.guarantee in GUARANTEES, member.guarantee


# ---------------------------------------------------------- 3. non-vacuity

@pytest.mark.parametrize("token", sorted(KERNEL_PROGRAMS))
def test_a_kernel_reaching_component_is_admitted_without_the_check(
        token, without_the_check):
    compile_source(KERNEL_PROGRAMS[token], "candidate.rvl")


@pytest.mark.parametrize("token", sorted(KERNEL_PROGRAMS))
def test_a_kernel_reaching_component_is_refused_with_it(token):
    with pytest.raises(RevlError) as excinfo:
        compile_source(KERNEL_PROGRAMS[token], "candidate.rvl")
    error = excinfo.value
    assert token in error.message or token in (error.hint or ""), error.message
    if token in kb.BY_TOKEN:
        assert error.code == kb.BY_TOKEN[token].guarantee
        assert kb.BY_TOKEN[token].why.split(";")[0] in (error.hint or "")
    else:
        # a claim on the namespace the enumeration has no member for: refused
        # under the namespace's own guarantee rather than read as ordinary.
        assert error.code == "G8"
        assert "enumeration has no member for" in error.message


def test_the_control_is_admitted_on_both_sides(without_the_check):
    compile_source(CONTROL, "control.rvl")


def test_the_control_is_admitted_with_the_check_live():
    compile_source(CONTROL, "control.rvl")


def test_the_refusal_names_the_tree_the_authority_is_over():
    with pytest.raises(RevlError) as excinfo:
        compile_source(KERNEL_PROGRAMS["kernel.gate"], "candidate.rvl")
    assert "crates/revl-gate" in (excinfo.value.hint or "")


def test_the_refusal_names_what_the_component_actually_holds():
    with pytest.raises(RevlError) as excinfo:
        compile_source(KERNEL_PROGRAMS["kernel.admission"], "candidate.rvl")
    assert "`kernel.admission`" in (excinfo.value.hint or "")


# ------------------------------------------ 3b. non-vacuity: the retention half

RETENTION_POLICY = '''
retention loop_cache {
  until: "2030-01-01T00:00:00Z"
  residence: "eu"
  deleters: dpo
}
''' + _reaching("kv.write")


def test_an_untrusted_author_may_not_declare_a_retention_policy():
    """A deadline change is an authority change, not behaviour tuning."""
    profile = AdmissionProfile.untrusted_author({"Boundary", "Task"})
    with pytest.raises(RevlError) as excinfo:
        compile_source(RETENTION_POLICY, "candidate.rvl", profile=profile)
    error = excinfo.value
    assert error.code == "G-RETAIN"
    assert "retention loop_cache" in error.message
    assert "until: 2030-01-01T00:00:00Z" in error.message
    assert "not a cache setting" in error.message


def test_the_same_policy_is_not_refused_by_this_rule_for_a_trusted_author():
    """The rule is a property of the AUTHOR, not of the declaration.

    A trusted composition declares the policies; the refusal above must not be
    reachable without a profile. (The program is still refused here, by
    `revl.retention`'s own validation of a policy that names no `hold` - a
    different rule with a different message, which is the point.)"""
    try:
        compile_source(RETENTION_POLICY, "composition.rvl")
    except RevlError as error:
        assert error.code != "G-RETAIN" or "cache setting" not in error.message


def test_a_retained_qualifier_naming_a_trusted_policy_is_untouched():
    """What an untrusted author may not do is MINT the policy, not name one."""
    module = '''
pub retention customer_pii {
  until: "2099-01-01T00:00:00Z"
  residence: "eu"
  deleters: dpo
}
'''
    root = 'use "policies.rvl" { customer_pii }\n' + _reaching("kv.write")
    profile = AdmissionProfile.untrusted_author({"Boundary", "Task"})
    try:
        compile_source(root, "candidate.rvl",
                       modules={"policies.rvl": module}, profile=profile)
    except RevlError as error:
        assert "forbids declaring a retention policy" not in error.message


# --------------------------------------------------- 4. the failure direction

def test_the_unnameable_star_is_disjoint_from_no_kernel_member():
    star = cap_order.Cap("*", ())
    for member in kb.kernel_held():
        assert not cap_order.disjoint(star, member), member.token


def test_an_unnameable_reach_meets_the_kernel_for_a_generated_component():
    star = {cap_order.Cap("*", ())}
    assert kb.offending(star, undeclared_reaches_kernel=True)
    # and the scope switch is a scope switch, not a second answer about `*`
    assert not kb.offending(star, undeclared_reaches_kernel=False)


def test_a_declared_narrow_reach_is_provably_outside_the_kernel():
    narrow = {cap_order.parse_cap('fs.write(path="/tmp")')}
    assert not kb.offending(narrow, undeclared_reaches_kernel=True)
    assert not kb.offending(narrow, undeclared_reaches_kernel=False)


def test_a_declared_kernel_reach_meets_the_kernel_on_both_sides():
    held = {cap_order.parse_cap("kernel.attest")}
    for switch in (True, False):
        hits = kb.offending(held, undeclared_reaches_kernel=switch)
        assert [m.token for m, _c in hits] == ["kernel.attest"]


# ------------------------- 4b. what item 519's product record contributes

#: One `manifest["model_reach"]` row exactly as `lower._check_model_attenuation`
#: writes it (item 519, PR #1253 `docs/design/539-model-in-attenuation.md` §3.3).
#: Built by hand rather than by compiling, because the clause that produces it
#: is on that branch and not on `main`; what is held here is the SHAPE, which is
#: the contract between the two items.
def _model_reach_row(**overrides) -> dict:
    row = {
        "component": "Candidate",
        "action": "classify",
        "origin": "public",
        "role": "planner",
        "residence": "off_device",
        "holds": ["kv.write"],
        "reaches": ["kv.write"],
        "effective": ["kv.write"],
        "attenuated": [],
        "reach_declared": True,
    }
    row.update(overrides)
    return row


def test_a_declared_model_reach_contributes_the_recorded_effective_ceiling():
    manifest = {"model_reach": [_model_reach_row()]}
    extra = kb.effective_from_model_reach("Candidate", manifest)
    assert [c.to_str() for c in extra] == ["kv.write"]
    assert not kb.offending(set(extra), undeclared_reaches_kernel=True)


def test_an_undeclared_model_reach_contributes_the_unnameable_star():
    """`reach_declared: false` is not a proof of narrowness.

    The record still renders an `effective` list, and reading THAT would make a
    role nobody declared look like the narrowest thing in the composition. It
    resolves to `*` instead, which no held set covers and which is disjoint
    from nothing - item 519's own resolution of the same question."""
    manifest = {"model_reach": [
        _model_reach_row(reach_declared=False, reaches=[], effective=[])]}
    extra = kb.effective_from_model_reach("Candidate", manifest)
    assert [c.to_str() for c in extra] == ["*"]
    assert kb.offending(set(extra), undeclared_reaches_kernel=True)


def test_a_model_reach_row_for_another_component_is_not_folded_in():
    manifest = {"model_reach": [
        _model_reach_row(component="Other", reach_declared=False)]}
    assert kb.effective_from_model_reach("Candidate", manifest) == []


def test_no_model_reach_key_contributes_nothing():
    assert kb.effective_from_model_reach("Candidate", {}) == []
    assert kb.effective_from_model_reach("Candidate", None) == []


def test_an_undeclared_surrogate_refuses_the_compile_end_to_end(monkeypatch):
    """The arm wired through `lower`, not only the helper in isolation.

    `main` has no `reaches [...]` clause, so no program on this tree produces a
    `model_reach` row; the product record is injected at the one seam item 519
    fills. What is proved is that a `*` arriving from the product REFUSES, so
    the two items compose instead of each half being correct alone."""
    monkeypatch.setattr(
        kb, "effective_from_model_reach",
        lambda component, manifest: [cap_order.Cap("*", ())])
    profile = AdmissionProfile.untrusted_author({"Boundary", "Task"})
    with pytest.raises(RevlError) as excinfo:
        compile_source(CONTROL, "candidate.rvl", profile=profile)
    error = excinfo.value
    assert error.code == "G8"
    assert "unnameable host boundary" in (error.hint or "")
    assert "not a proof of narrowness" in (error.hint or "")
    # and the same injection is inert for the first-party tree, which is the
    # subject of the loop rather than a candidate passing through admission
    compile_source(CONTROL, "composition.rvl")


# ----------------------------------------------------------- 5. the residual

def test_the_key_namespaced_residual_is_still_admitted():
    """PINNED, not assumed away (`kernel_boundary._undeclared`).

    A service method that declares `emission` with no capability list yields a
    reserved-namespace fold element: a declared WIRING with an undeclared
    REACH. Refusing it is the stronger answer and this slice does not, because
    on this tree it is the ordinary spelling. This test fails if that ever
    changes silently, which is the only thing standing between a stated
    residual and a forgotten one.

    Item 561 moved that element off the consumer's wiring key and onto the
    SERVICE the method is declared on (`lower._undeclared_cap`), which is what
    it is named BY and not whether it is undeclared. The element below is built
    by the reference rather than spelled as a literal, so this pin tracks the
    real one instead of a namespace that has moved out from under it."""
    src = _TASK + """
service Bare { emission fn cross(row: Str) -> Int }

component Candidate requires b: Bare provides task: Task {
  provide task {
    fn go() {
      emit b.cross("x")
      return 0
    }
  }
}
"""
    profile = AdmissionProfile.untrusted_author({"Bare", "Task"})
    compile_source(src, "candidate.rvl", profile=profile)
    element = lower._undeclared_cap("Bare")
    assert element.token == lower._UNDECLARED_NS + "Bare"
    assert not kb._undeclared(element)
    assert not kb.offending({element}, undeclared_reaches_kernel=True)


def test_the_host_extern_routes_to_star_are_closed_before_this_check():
    """Why the `*` arm is dormant for a candidate's OWN reach on `main`.

    Both routes that would put a `*` in an untrusted-authored component's reach
    are already refused, earlier and by name: declaring a host extern
    (`check_no_extern`) and reaching an imported one
    (`check_no_host_extern_reach`). That is defence in depth rather than a
    check that cannot fail - the arm is live through the product record above -
    and it is measured here so the claim is a fact about the tree."""
    profile = AdmissionProfile.untrusted_author({"Task"})
    declares = _TASK + """
extern emission fn host_write(m: Str) -> Int = @py { return 0 }

component Candidate provides task: Task {
  provide task {
    fn go() {
      emit host_write("x")
      return 0
    }
  }
}
"""
    with pytest.raises(RevlError) as excinfo:
        compile_source(declares, "candidate.rvl", profile=profile)
    assert "forbids new `extern`" in excinfo.value.message

    module = 'pub extern emission fn host_write(m: Str) -> Int = @py { return 0 }\n'
    imports = 'use "host.rvl" { host_write }\n' + declares.replace(
        'extern emission fn host_write(m: Str) -> Int = @py { return 0 }\n', "")
    with pytest.raises(RevlError) as excinfo:
        compile_source(imports, "candidate.rvl",
                       modules={"host.rvl": module}, profile=profile)
    assert "forbids reaching host code" in excinfo.value.message


# ------------------------------------------ 6. one enumeration, two consumers

def test_the_controller_reads_this_enumeration_rather_than_copying_it():
    """`tools/evolution_controller.py` (item 520) is the diff-side consumer.

    It is on an open branch, not on `main`, so this asserts the contract only
    when the file is there: the two lists must be the same list. When it lands
    it imports `KERNEL_PATHS` from here; until then this test skips rather than
    pretending to have checked something."""
    controller = ROOT / "tools" / "evolution_controller.py"
    if not controller.exists():
        pytest.skip("tools/evolution_controller.py is not on this tree yet "
                    "(item 520, PR #1241)")
    text = controller.read_text(encoding="utf-8")
    assert "from revl.kernel_boundary import" in text or \
           "kernel_boundary" in text, (
        "tools/evolution_controller.py keeps its own KERNEL_PATHS. The "
        "enumeration is meant to be in ONE file (issue #1223): import it from "
        "src/revl/kernel_boundary.py.")
