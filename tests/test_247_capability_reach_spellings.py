"""Which spellings a capability policy rule selects — item 247.

A policy rule (`capability <glob> requires register <level>`, `component X may
reach [...]`, `component X may not reach [...]`) selects a component only when
the token it names is in that component's REACH (`policy.component_reach`, off
the G8 audit graph). Until item 247 the reach of a DIRECTLY EMITTED extern was
the extern's own NAME even when the extern declared a capability scope, while
every other authority surface over the same crossing used the DECLARED TOKEN:

  * `lower._extern_emission_caps`         -> `capabilities or (name,)`
  * `emission_analysis`'s witnessed seed  -> `capabilities or [name]`
  * `audit_diff._capability_registers`    -> `capabilities or [name]`
  * item 343's approval `ClassMap`        -> `capabilities or [name]`

So `capability db requires register keyed` read its floor input from a map keyed
`db` and its selector from a reach keyed `pg_write`, and the two halves of one
rule could never meet: an operator wrote a floor and silently got less, with no
diagnostic. That was found in PR #245, where the design note's two-extern
reproducer never reached the refusal at all and the end-to-end test had to add a
service method to make the rule fire.

The decision item 247 records: reach carries the DECLARED TOKEN, because item
343 already decided this for the same crossing on the approval axis, and G8's
boundary is meant to be enumerable in ONE namespace. `_boundary` was the single
surface that had not been given item 343's treatment.

This file is the pin. `SPELLINGS` is the whole table — every way a component can
cross a boundary, and the token a `capability <glob>` rule must name to select
it — so that which spelling puts a token under policy is READ, not discovered by
experiment. The register-floor regression at the bottom drives the fixed hole
end to end.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.audit_diff import audit_report  # noqa: E402
from revl.mcp.approval import ClassMap  # noqa: E402
from revl.policy import component_reach, evaluate, parse_policy  # noqa: E402

PRELUDE = """
type Stash = { path: Str, bak: Str }
extern pure fn unstash(w: Stash) -> Unit = @py { return None }
extern pure fn digest(s: Str) -> Str = @py { return s }
extern emission[db] fn pg_write(row: Str) -> Int = @py { return 0 }
extern emission fn send_mail(to: Str) -> Int = @py { return 0 }
extern witnessed[fs] fn stash_scoped(p: Str) -> Result[Stash, Str]
    undo unstash(result) = @py { return Ok({"path": p, "bak": p}) }
extern witnessed fn stash_bare(p: Str) -> Result[Stash, Str]
    undo unstash(result) = @py { return Ok({"path": p, "bak": p}) }
fn wrapped(row: Str) -> Int { return pg_write(row) }
fn dispatch(f: (Str) -> Int, x: Str) -> Int { return f(x) }
service Scoped { emission[cap_svc] fn put(row: Str) -> Int }
service Bare { emission fn put(row: Str) -> Int }
service Ops { emission fn go(x: Str) -> Int }
"""

# component -> (requires clause, provide-method steps, the tokens a capability
# rule must name to select it). One component per SPELLING, each reaching a
# distinct token so a rule can be aimed at exactly one.
SPELLINGS: dict[str, tuple[str, list[str], set[str]]] = {
    # --- host code, reached directly or through a chain of `fn`s -------------
    # a SCOPED emission extern: the declared token, NOT the extern name.
    "DirectScoped": ("", ["emit pg_write(x)"], {"db"}),
    # the same crossing routed through a plain `fn`: the scope is what
    # propagates, so refactoring a body into helpers cannot change the rule.
    "TransitiveScoped": ("", ["let a = wrapped(x)"], {"db"}),
    # an UNSCOPED emission extern: the extern IS the boundary, so it names
    # itself (docs/capabilities.md §2).
    "DirectBare": ("", ["emit send_mail(x)"], {"send_mail"}),
    # a SCOPED witnessed extern joins the same authority namespace (item 343).
    "WitnessedScoped": ("", ["effect stash_scoped(x)"], {"fs"}),
    "WitnessedBare": ("", ["effect stash_bare(x)"], {"stash_bare"}),
    # a `pure` extern cannot carry a scope, so it names itself; it is still
    # reached host code and a deny-list can name it.
    "PureHost": ("", ["let d = digest(x)", "emit send_mail(d)"],
                 {"digest", "send_mail"}),
    # --- service-method emissions -------------------------------------------
    # the REQUIREMENT KEY the scoped method is called through, not the service
    # name and not the callee's own downstream capabilities (§3, "what is not
    # transitive").
    "ScopedSvc": ("requires cap_svc: Scoped ", ["emit cap_svc.put(x)"],
                  {"cap_svc"}),
    # a bare `emission` promises nothing, so it reaches the unnameable `*`. No
    # named glob selects it; only a literal `*` in an allow-list accepts it.
    "BareSvc": ("requires bare: Bare ", ["emit bare.put(x)"], {"*"}),
}


def _source() -> str:
    src = PRELUDE
    for index, (name, (req, steps, _)) in enumerate(SPELLINGS.items()):
        key = f"ops{index}"
        src += (f"component {name} {req}provides {key}: Ops {{\n"
                f"  provide {key} {{\n    fn go(x) {{\n")
        src += "".join(f"      {step}\n" for step in steps)
        src += "      return 0\n    }\n  }\n}\n"
    return src


@pytest.fixture(scope="module")
def audit():
    return audit_report(compile_source(_source(), "spellings.rvl"))


@pytest.mark.parametrize("component", sorted(SPELLINGS))
def test_the_spelling_table_is_exact(audit, component):
    """The token set a `capability <glob>` rule selects on, per spelling.

    Exact equality, not membership: an extra token is a rule selecting a
    component the operator did not aim at, and a missing one is a floor that
    silently does not apply.
    """
    expected = SPELLINGS[component][2]
    assert {r.token for r in component_reach(audit, component)} == expected


def test_a_scoped_externs_name_is_not_a_policy_token(audit):
    """The decision, stated as a refusal to double-count.

    A scoped extern's reach is its DECLARED token and nothing else. Carrying the
    name as well would create a third namespace no other surface uses, and would
    break the allow-list direction: `may reach [db]` is exactly what an operator
    who read the declaration writes, and it must admit.
    """
    tokens = {r.token for r in component_reach(audit, "DirectScoped")}
    assert "db" in tokens
    assert "pg_write" not in tokens

    allow = parse_policy("component DirectScoped may reach db")
    assert evaluate(allow, audit) == []

    # aiming the deny-list at the token refuses; aiming it at the extern NAME
    # selects nothing, which is the visible half of the same decision.
    by_token = parse_policy("component DirectScoped may not reach db")
    assert [v.token for v in evaluate(by_token, audit)] == ["db"]
    by_name = parse_policy("component DirectScoped may not reach pg_write")
    assert evaluate(by_name, audit) == []


def test_the_extern_name_is_still_enumerated_on_the_audit_surface(audit):
    """Nothing goes dark: the audit's per-component `externs` table is the
    HOST-CODE enumeration (name, class, backends, ref provenance) and stays keyed
    by name. It gains the declared scope beside it, so a reader of `revl audit`
    can get from the token a rule names back to the source that declares it."""
    entries = {e["name"]: e for e in audit["boundary"]["DirectScoped"]["externs"]}
    assert entries["pg_write"]["capabilities"] == ["db"]
    # an unscoped extern carries no `capabilities` key at all, so every pre-247
    # audit entry is byte-identical.
    bare = {e["name"]: e for e in audit["boundary"]["DirectBare"]["externs"]}
    assert "capabilities" not in bare["send_mail"]


def test_the_trace_names_the_host_code_a_token_was_declared_on(audit):
    """A refusal on `db` has to be navigable back to `pg_write`, or the operator
    reads a token that appears nowhere in the source."""
    policy = parse_policy("component DirectScoped may not reach db")
    violation = evaluate(policy, audit)[0]
    rendered = violation.render()
    assert "`db`" in rendered
    assert "pg_write" in rendered


# the host-extern spellings — the axis item 247 changed. The service-method
# spellings are excluded deliberately: the approval fold reads a BARE emission's
# required KEY (`bare`) where `_boundary` records the unnameable `*`, which is a
# separate enumeration question about bare emissions and is not decided here.
EXTERN_SPELLINGS = ("DirectScoped", "TransitiveScoped", "DirectBare",
                    "WitnessedScoped", "WitnessedBare", "PureHost")


@pytest.mark.parametrize("component", EXTERN_SPELLINGS)
def test_component_reach_and_the_approval_fold_agree_on_extern_spellings(
        audit, component):
    """The cross-surface check that makes this more than a preference.

    The approval `ClassMap` (item 343/246) and `component_reach` grade the SAME
    crossing for two different operator sentences — `capability db requires
    approval` and `capability db requires register keyed`. Before item 247 the
    fold said `db` and the reach said `pg_write`, so the two sentences named
    different things while looking identical. They must not diverge again.
    """
    ir = compile_source(_source(), "spellings.rvl")
    index = list(SPELLINGS).index(component)
    fold = ClassMap(ir).classify_call(f"ops{index}", "go")
    # the approval fold walks only the boundary-crossing classes (emission,
    # witnessed); a `pure` extern is not a crossing, so it is dropped from the
    # comparison rather than expected on the fold's side.
    crossing = {r.token for r in component_reach(audit, component)} - {"digest"}
    assert set(fold["capabilities"]) == crossing


# --------------------------------------------------------------- the regression

# PR #245's design-note reproducer, restored to its intended shape: `db` is
# declared ONLY by a directly-emitted extern. Before item 247 the register rule
# selected nothing here and the composition was admitted; the end-to-end test
# had to add a bare-`idempotent` service method on `db` to make the rule fire at
# all. The floor now applies to the crossing the operator was looking at.
BARE_IDEMPOTENT = """
extern emission[db] idempotent fn pg_write(row: Str) -> Int = @py { return 0 }
service Ops { emission fn go(x: Str) -> Int }
component Writer provides ops: Ops {
  provide ops { fn go(x) { emit pg_write(x) return 0 } }
}
"""

KEYED_IDEMPOTENT = BARE_IDEMPOTENT.replace("idempotent fn", "idempotent(key: row) fn")


def test_a_register_floor_now_applies_to_a_directly_emitted_extern():
    """`capability db requires register keyed` refuses a `db` declared `keyed`
    nowhere — the refusal PR #245 could not reach through a direct emission."""
    audit = audit_report(compile_source(BARE_IDEMPOTENT, "bare.rvl"))
    # both halves of the rule are now keyed the same way.
    assert audit["capability_registers"] == {"db": "declared"}
    assert {r.token for r in component_reach(audit, "Writer")} == {"db"}

    policy = parse_policy("capability db requires register keyed")
    violations = evaluate(policy, audit)
    assert [v.token for v in violations] == ["db"]
    assert "keyed" in violations[0].message

    # the floor the declaration does meet still admits, so the rule refuses on
    # the register and not merely on being selected.
    assert evaluate(parse_policy("capability db requires register declared"),
                    audit) == []


def test_the_keyed_declaration_admits_under_the_keyed_floor():
    audit = audit_report(compile_source(KEYED_IDEMPOTENT, "keyed.rvl"))
    assert audit["capability_registers"] == {"db": "keyed"}
    assert evaluate(parse_policy("capability db requires register keyed"),
                    audit) == []


def test_the_rule_is_still_inert_against_a_token_nothing_reaches():
    """The negative half: selection is by reach, so a floor on a capability this
    composition never crosses refuses nothing. A fix that made every declared
    token selectable everywhere would pass the test above and break this one."""
    audit = audit_report(compile_source(BARE_IDEMPOTENT, "bare.rvl"))
    assert evaluate(parse_policy("capability bus requires register keyed"),
                    audit) == []
