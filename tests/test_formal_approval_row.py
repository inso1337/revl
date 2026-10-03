"""The approval floor in the formal model (issue #1455).

The checker refuses a marked crossing of an approval-required capability
that carries no covering `with` edge (item 246, `lower.
_require_declared_approval`, code G4, category `approval`). The formal model
had no fact about approvals, so the 34 corpus documents refused that way sat
in a ratcheted `out-of-fragment-approval` bucket, which records an absence
and proves nothing about the rule.

The model states the rule now (`formal/RevL/Theorems/G4_ApprovalFloor.lean`,
`RevL.G4Approval.CrossingOK`), and `formal/harness/diff_corpus.py` exports the
three facts it is stated over:

  * `AR` - an approval-required capability TOKEN (a scoped extern's scope,
    an unscoped one's name);
  * `AX` - one token a marked crossing reaches;
  * `AE` - that crossing's `with` edge, absent for the value form.

The Lean oracle prints one `AP` verdict per crossing (`approvalRowB`, bridged
by `approvalRowB_iff`), the Python reference recomputes it with the shipped
`lower._approval_covers`, and the alignment arm files every approval refusal
under `agree-G4`. The bucket and its ledger entries are gone.

The same issue's comment: a SCOPED host emission carries its token into the
model. The F row's bound column used to read `extern emission[net] fn
zz_write` as `zz_write`, so a provider declared `emission[net]` that called
it was a model refusal the checker does not make.

A crossing through a provision receiver resolves off the alias table the
marker rule's `G` row reads: a field or element read off a local, a receiver
written in place, a provide method's service-typed parameter, and an arrow's
service-typed parameter at an application that binds a provision into it.

This module runs in the plain `pytest tests/` job. The Lean half is checked
here when `lake` is on PATH, and by `make formal` always.
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import shutil
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

LEAN = ROOT / "formal" / "RevL" / "Theorems" / "G4_ApprovalFloor.lean"
ORACLE = ROOT / "formal" / "harness" / "Oracle.lean"
GATE = ROOT / "formal" / "scripts" / "run_gate.sh"
CHECK = ROOT / "formal" / "CheckAxioms.lean"
REGISTRY = ROOT / "formal" / "scripts" / "nonvacuity.tsv"
LEDGER = ROOT / "formal" / "out_of_fragment_ledger.json"

SCOPED = "examples/rejections/g4_approval_scoped_extern.rvl"
HELPER = "examples/rejections/g4_approval_helper_reach.rvl"
VALUE_FORM = "examples/rejections/g4_approval_value_form_method.rvl"
COMPENSATE = "examples/rejections/g4_approval_compensate.rvl"
OTHER_EDGE = "examples/rejections/g4_approval_compensate_other_edge.rvl"
HANDLE = "examples/rejections/g4_approval_spawn_handle.rvl"
HANDLE_ALIAS = "examples/rejections/g4_approval_spawn_handle_alias.rvl"

#: Every corpus document the checker refuses under the approval floor: the
#: names the deleted `out-of-fragment-approval` ledger list held.
APPROVAL_FIXTURES = (
    "examples/rejections/g4_approval_compensate.rvl",
    "examples/rejections/g4_approval_compensate_method.rvl",
    "examples/rejections/g4_approval_compensate_other_edge.rvl",
    "examples/rejections/g4_approval_helper_reach.rvl",
    "examples/rejections/g4_approval_scoped_extern.rvl",
    "examples/rejections/g4_approval_spawn_handle.rvl",
    "examples/rejections/g4_approval_spawn_handle_alias.rvl",
    "examples/rejections/g4_approval_spawn_handle_method.rvl",
    "examples/rejections/g4_approval_value_form_method.rvl",
    "tests/fixtures/gate_block_nesting/g4_approval_after_else.rvl",
    "tests/fixtures/gate_block_nesting/g4_approval_after_for.rvl",
    "tests/fixtures/gate_block_nesting/g4_approval_after_guard_provide.rvl",
    "tests/fixtures/gate_block_nesting/g4_approval_after_guard_setup.rvl",
    "tests/fixtures/gate_block_nesting/g4_approval_after_if.rvl",
    "tests/fixtures/gate_block_nesting/g4_approval_after_while.rvl",
    "tests/fixtures/gate_block_nesting/g4_approval_in_braceless_if.rvl",
    "tests/fixtures/gate_block_nesting/g4_approval_in_multiline_if.rvl",
    "tests/fixtures/gate_block_nesting/g4_approval_in_oneline_else.rvl",
    "tests/fixtures/gate_block_nesting/g4_approval_in_oneline_for.rvl",
    "tests/fixtures/gate_block_nesting/g4_approval_in_oneline_if.rvl",
    "tests/fixtures/gate_block_nesting/g4_approval_in_oneline_while.rvl",
    "tests/fixtures/approval_service_locals/g4_alias_step.rvl",
    "tests/fixtures/approval_service_locals/g4_arrow_param_applied.rvl",
    "tests/fixtures/approval_service_locals/g4_handle_direct_value.rvl",
    "tests/fixtures/approval_service_locals/g4_if_local_step.rvl",
    "tests/fixtures/approval_service_locals/g4_if_local_value.rvl",
    "tests/fixtures/approval_service_locals/g4_list_element_step.rvl",
    "tests/fixtures/approval_service_locals/g4_record_alias_step.rvl",
    "tests/fixtures/approval_service_locals/g4_record_field_step.rvl",
    "tests/fixtures/service_receiver_expressions/g4_if_marked.rvl",
    "tests/fixtures/service_receiver_expressions/g4_list_marked.rvl",
    "tests/fixtures/service_receiver_expressions/g4_match_marked.rvl",
    "tests/fixtures/service_receiver_expressions/g4_record_marked.rvl",
    "tests/fixtures/service_typed_params/g4_param_marked_no_edge.rvl",
)

#: Receivers the marker rule resolves and the approval floor has to resolve
#: the same way: an element read off a list local, each receiver written in
#: place, and an arrow parameter bound to a provision at its application.
RECEIVER_FIXTURES = (
    "tests/fixtures/approval_service_locals/g4_list_element_step.rvl",
    "tests/fixtures/approval_service_locals/g4_arrow_param_applied.rvl",
    "tests/fixtures/service_receiver_expressions/g4_if_marked.rvl",
    "tests/fixtures/service_receiver_expressions/g4_list_marked.rvl",
    "tests/fixtures/service_receiver_expressions/g4_match_marked.rvl",
    "tests/fixtures/service_receiver_expressions/g4_record_marked.rvl",
)

#: Their covered twins, each crossing under an edge for the token.
RECEIVER_TWINS = (
    "tests/fixtures/approval_service_locals/ok_list_element_step.rvl",
    "tests/fixtures/service_receiver_expressions/ok_if_approved.rvl",
    "tests/fixtures/service_receiver_expressions/ok_list_approved.rvl",
    "tests/fixtures/service_receiver_expressions/ok_match_approved.rvl",
    "tests/fixtures/service_receiver_expressions/ok_record_approved.rvl",
)

#: An arrow with a service-typed parameter that is never applied to a
#: provision: the checker admits it, so its body reaches no token.
UNAPPLIED_ARROWS = (
    "tests/fixtures/approval_service_locals/ok_arrow_param_unapplied.rvl",
    "tests/fixtures/service_receiver_expressions/ok_arrow_param_expression_unapplied.rvl",
)

#: Admitted programs the floor has to ADMIT: the exact edge, a glob edge, an
#: `Approval[C]`-typed parameter, a `let` alias of the approval, and a
#: compensation covered by its step's edge.
ADMITTED_SOURCES = {
    "exact_edge.rvl": """\
extern emission[production.payment] fn charge(cents: Int) requires approval = @py { return }
service Till { fn total() -> Int }
component Register provides till: Till {
  let a = await approval[production.payment] { reason: "pay" }
  emit charge(199) with a
  provide till { fn total() = 199 }
}
""",
    "glob_edge.rvl": """\
extern emission[production.payment] fn charge(cents: Int) requires approval = @py { return }
service Till { fn total() -> Int }
component Register provides till: Till {
  let a = await approval["production.*"] { reason: "pay" }
  emit charge(199) with a
  provide till { fn total() = 199 }
}
""",
    "alias_edge.rvl": """\
extern emission[pay] fn charge(cents: Int) -> Int requires approval = @py { return 1 }
service Till { emission fn ring(cents: Int) -> Int }
component Register provides till: Till {
  let a = await approval[pay] { reason: "pay" }
  provide till { fn ring(cents) { let b = a  emit charge(cents) with b  return 1 } }
}
""",
    "compensation_covered.rvl": """\
extern emission fn charge(amount: Int) -> Int requires approval = @py { return 1 }
extern emission fn notify(n: Int) -> Int = @py { return 1 }
service Ops { fn ping() -> Int }
component Biller provides ops: Ops {
  let a = await approval[charge] { reason: "refund" }
  emit notify(1) compensate charge(2) with a
  provide ops { fn ping() = 1 }
}
""",
}

#: The same crossing under an edge whose glob does not reach it: refused.
WRONG_GLOB = """\
extern emission[production.payment] fn charge(cents: Int) requires approval = @py { return }
service Till { fn total() -> Int }
component Register provides till: Till {
  let a = await approval["staging.*"] { reason: "pay" }
  emit charge(199) with a
  provide till { fn total() = 199 }
}
"""

#: The marked twin of `tests/fixtures/gate_block_nesting/g4_host_marker_after_if.rvl`:
#: an `emission[net]` provider calling an `emission[net]` extern. Admitted by
#: the checker. The F row used to spell the crossing `zz_write` on the bound
#: side, so the model refused it (`formal-strict`, FATAL).
SCOPED_PROVIDER = """\
extern emission[net] fn zz_write(t: Str) -> Int = @py { return 1 }
service K { emission[net] fn f(k: Str) -> Int }
component C provides k: K {
  provide k {
    fn f(k) {
      let q = emit zz_write(k)
      return 0
    }
  }
}
"""

#: The same crossing reached through a module `fn`, which the F row used to
#: widen to `*` on the bound side.
SCOPED_THROUGH_FN = """\
extern emission[net] fn zz_write(t: Str) -> Int = @py { return 1 }
fn relay(t: Str) -> Int { return zz_write(t) }
service K { emission[net] fn f(k: Str) -> Int }
component C provides k: K {
  provide k {
    fn f(k) {
      let q = emit relay(k)
      return 0
    }
  }
}
"""

#: A parent that HOLDS `pay` spawning a child whose op emits through a
#: `pay`-scoped extern. The checker refuses it: its attenuation fold reads a
#: host emission as `*` (`_emit_step_caps_pairs`), and `pay` does not cover
#: `*`. So the fold column keeps `*`; carrying `pay` there would make the
#: model admit what the checker refuses (`missed-G4`).
SCOPED_CHILD_UNDER_HOLDER = """\
extern emission[pay] fn charge(amount: Int) -> Int = @py { return 1 }
service Pay { emission[pay] fn run(n: Int) -> Int }
service Sup { fn go(n: Int) -> Int }
component Worker provides task: Pay {
  provide task { fn run(n) { emit charge(n) return 1 } }
}
component Supervisor requires bank: Pay provides sup: Sup {
  let w = effect spawn Worker with { } undo w.dispose()
  provide sup { fn go(n) = 0 }
}
component Bank provides bank: Pay {
  provide bank { fn run(n) { emit charge(n) return 1 } }
}
"""


@pytest.fixture(scope="module")
def harness():
    spec = importlib.util.spec_from_file_location(
        "formal_diff_corpus_approval",
        ROOT / "formal" / "harness" / "diff_corpus.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tsv(harness):
    rows, _facts, _census = harness.export()
    return rows


@pytest.fixture(scope="module")
def verdicts(harness, tsv):
    return harness.reference_from_tsv(tsv)


def _rows(tsv, kind, rel=None):
    out = []
    for row in tsv:
        r = row.split("\t")
        if r[0] == kind and (rel is None or r[1] == rel):
            out.append(r)
    return out


def _crossings(tsv, rel):
    """`{(comp, ord): (sorted tokens, edge or None)}` for one file."""
    tokens: dict = {}
    edges: dict = {}
    for r in _rows(tsv, "AX", rel):
        tokens.setdefault((r[2], r[3]), set()).add(r[4])
    for r in _rows(tsv, "AE", rel):
        edges[(r[2], r[3])] = r[4]
    return {k: (sorted(v), edges.get(k)) for k, v in tokens.items()}


def _checker(source: str, name: str) -> tuple[str, str]:
    from revl.compiler import compile_source
    from revl.diagnostics import classify
    from revl.errors import RevlError

    try:
        compile_source(source, name)
        return ("accept", "")
    except RevlError as e:
        info = classify(e)
        return (info.get("code") or "UNCODED", info.get("category") or "")


def _synthetic(harness, root: Path, sources: dict[str, str]):
    """Export `sources` as the whole corpus and decide it on both sides: the
    reference always, the Lean oracle when `lake` is on PATH. Returns
    `(tsv rows, file facts, reference, formal-or-None)`."""
    corpus = root / "corpus"
    corpus.mkdir(exist_ok=True)
    for name, text in sources.items():
        (corpus / name).write_text(text, encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(harness, "REPO", root)
        mp.setattr(harness, "CORPUS_DIRS", ("corpus",))
        rows, facts, _census = harness.export()
    ref = harness.reference_from_tsv(rows)
    formal = None
    if shutil.which("lake") is not None:
        tsv_path = root / "corpus.tsv"
        tsv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        formal = harness.parse_verdicts(
            harness.run_oracle(tsv_path, root / "formal_verdicts.tsv"))
    return rows, facts, ref, formal


def _align(harness, root: Path, scratch: Path, rels, verdicts):
    """`checker_alignment` over `rels`, compiled from under `root`, with the
    no-manifest writer pointed at `scratch` so the tree is left alone.
    Returns `(bucket -> files, fatal findings)`."""
    (scratch / "harness" / "out").mkdir(parents=True, exist_ok=True)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(harness, "REPO", root)
        mp.setattr(harness, "FORMAL", scratch)
        buf = io.StringIO()
        with redirect_stdout(buf):
            fatal = harness.checker_alignment({rel: {} for rel in rels}, [],
                                              verdicts)
        samples = dict(harness._ALIGN_SAMPLES)
    return {rel: k for k, v in samples.items() for rel in v}, fatal


# ------------------------------------------------------------- the premise

def test_the_checker_refuses_every_fixture_under_the_approval_floor(harness):
    """If one of these stops being an approval refusal, the rest of the
    module measures the wrong thing."""
    for rel in APPROVAL_FIXTURES:
        assert harness.checker_code(rel) == ("G4", "approval"), rel


def test_the_checker_admits_the_covered_twins():
    for name, source in ADMITTED_SOURCES.items():
        assert _checker(source, name) == ("accept", ""), name
    assert _checker(WRONG_GLOB, "wrong_glob.rvl") == ("G4", "approval")


# ------------------------------------------------------------- the facts

def test_the_required_token_is_the_scope_not_the_name(tsv):
    """`extern emission[production.payment] fn charge ... requires
    approval` requires `production.payment`: the requirement belongs to the
    capability, as `lower._approval_index` keys it."""
    assert [r[2] for r in _rows(tsv, "AR", SCOPED)] == ["production.payment"]
    assert _crossings(tsv, SCOPED) == {
        ("Register", "0"): (["production.payment"], None)}


def test_each_carrier_resolves_to_its_token(tsv):
    """The checker's order of resolution (`_approval_crossed_caps`): a module
    `fn` crosses what it reaches, a spawn handle and a `let` alias of its
    provision cross the op's declared scope, and the value form has no
    edge."""
    assert _crossings(tsv, HELPER) == {("Register", "0"): (["charge"], None)}
    assert _crossings(tsv, VALUE_FORM) == {("Register", "0"): (["charge"], None)}
    assert _crossings(tsv, HANDLE) == {("Supervisor", "0"): (["pay"], None)}
    assert _crossings(tsv, HANDLE_ALIAS) == {("Supervisor", "0"): (["pay"], None)}


def test_a_provision_receiver_resolves_as_the_marker_rule_resolves_it(tsv):
    """Each receiver shape crosses the op's declared scope with no edge, and
    its covered twin crosses it under the edge. Before issue #1455 closed
    these, the floor read only a spawn handle and a plain `let` alias, so
    these crossings carried no token and were silently admitted."""
    for rel in RECEIVER_FIXTURES:
        assert _crossings(tsv, rel) == {
            ("Register", "0"): (["production.payment"], None)}, rel
    for rel in RECEIVER_TWINS:
        assert _crossings(tsv, rel) == {
            ("Register", "0"): (["production.payment"],
                                "production.payment")}, rel


def test_an_unapplied_arrow_parameter_reaches_no_token(harness, tsv):
    """No application binds a provision into the parameter, so the checker
    admits the arrow and the floor has nothing to judge."""
    for rel in UNAPPLIED_ARROWS:
        assert harness.checker_code(rel) == ("accept", ""), rel
        assert _crossings(tsv, rel) == {}, rel


def test_a_compensation_is_its_own_crossing_under_the_step_s_edge(tsv):
    """The head and the compensation are two crossings. In the other-edge
    fixture both carry the step's edge `mail`, which covers the head's token
    and not the compensation's."""
    assert _crossings(tsv, COMPENSATE) == {
        ("Biller", "0"): (["notify"], None),
        ("Biller", "1"): (["charge"], None)}
    assert _crossings(tsv, OTHER_EDGE) == {
        ("Biller", "0"): (["mail"], "mail"),
        ("Biller", "1"): (["pay"], "mail")}


def test_a_file_with_no_required_token_carries_no_crossing_rows(tsv):
    """Every crossing in such a file is admitted by construction, so a row
    would agree about nothing."""
    required = {r[1] for r in _rows(tsv, "AR")}
    crossed = {r[1] for r in _rows(tsv, "AX")} | {r[1] for r in _rows(tsv, "AE")}
    assert crossed <= required
    assert set(APPROVAL_FIXTURES) <= required


# ------------------------------------------------------- the reference

def test_every_approval_fixture_has_a_refused_crossing(verdicts):
    refused = {k[0] for k, x in verdicts.approvals.items() if x == "fail"}
    assert refused == set(APPROVAL_FIXTURES)


def test_the_covered_head_and_the_unrequired_head_are_admitted(verdicts):
    assert verdicts.approvals[(OTHER_EDGE, "Biller", "0")] == "ok"
    assert verdicts.approvals[(OTHER_EDGE, "Biller", "1")] == "fail"
    assert verdicts.approvals[(COMPENSATE, "Biller", "0")] == "ok"
    assert verdicts.approvals[(COMPENSATE, "Biller", "1")] == "fail"


def test_the_row_moves_with_each_fact_alone(harness):
    """Drop the edge and the crossing is refused; drop the requirement and it
    is admitted; a glob edge covers through the shipped `_approval_covers`."""
    ar = "\t".join(["AR", "x.rvl", "production.payment"])
    ax = "\t".join(["AX", "x.rvl", "C", "0", "production.payment"])
    ae = "\t".join(["AE", "x.rvl", "C", "0", "production.*"])
    key = ("x.rvl", "C", "0")
    assert harness.reference_from_tsv([ar, ax, ae]).approvals[key] == "ok"
    assert harness.reference_from_tsv([ar, ax]).approvals[key] == "fail"
    assert harness.reference_from_tsv([ax]).approvals[key] == "ok"
    wrong = "\t".join(["AE", "x.rvl", "C", "0", "staging.*"])
    assert harness.reference_from_tsv([ar, ax, wrong]).approvals[key] == "fail"


def test_the_ap_row_is_compared_and_counted(harness, verdicts):
    assert "approvals" in harness.Verdicts._fields
    text = "\t".join(["AP", SCOPED, "Register", "0", "approval=fail"]) + "\n"
    assert harness.parse_verdicts(text).approvals[(SCOPED, "Register", "0")] == "fail"
    assert verdicts.total() == sum(
        len(getattr(verdicts, f)) for f in harness.Verdicts._fields)


# ------------------------------------------------- admitted programs, both sides

@pytest.fixture(scope="module")
def admitted(harness, tmp_path_factory):
    root = tmp_path_factory.mktemp("approval_admitted")
    return root, _synthetic(harness, root, {**ADMITTED_SOURCES,
                                            "wrong_glob.rvl": WRONG_GLOB})


def test_the_floor_admits_every_covered_crossing(admitted):
    _root, (rows, _facts, ref, formal) = admitted
    for name in ADMITTED_SOURCES:
        rel = f"corpus/{name}"
        mine = {k: x for k, x in ref.approvals.items() if k[0] == rel}
        assert mine and set(mine.values()) == {"ok"}, (name, mine)
    assert ref.approvals[("corpus/wrong_glob.rvl", "Register", "0")] == "fail"
    if formal is not None:
        assert formal.approvals == ref.approvals


def test_the_admitted_programs_file_under_agree_accept(harness, admitted):
    root, (_rows_, facts, ref, _formal) = admitted
    buckets, fatal = _align(harness, root, root, list(facts), ref)
    assert fatal == []
    for name in ADMITTED_SOURCES:
        assert buckets[f"corpus/{name}"] == "agree-accept", name
    assert buckets["corpus/wrong_glob.rvl"] == "agree-G4"


# -------------------------------------------------- the alignment arm

def test_every_approval_fixture_files_under_agree_g4(harness, verdicts,
                                                     tmp_path):
    buckets, fatal = _align(harness, ROOT, tmp_path, APPROVAL_FIXTURES,
                            verdicts)
    assert fatal == []
    assert {buckets[rel] for rel in APPROVAL_FIXTURES} == {"agree-G4"}


def test_a_blind_row_is_filed_under_missed_g4(harness, verdicts, tmp_path):
    """With the AP rows reading `ok`, an approval refusal has nothing to
    agree with: the fatal bucket, not an out-of-fragment one."""
    blind = verdicts._replace(
        approvals={k: "ok" for k in verdicts.approvals})
    buckets, fatal = _align(harness, ROOT, tmp_path, (SCOPED,), blind)
    assert buckets[SCOPED] == "missed-G4"
    assert fatal == [f"missed-G4: {SCOPED}"]


def test_the_out_of_fragment_approval_bucket_is_gone(harness):
    assert "out-of-fragment-approval" not in harness.OOF_RATCHET_BUCKETS
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
    assert "out-of-fragment-approval" not in ledger
    assert "out-of-fragment-approval" not in (
        ROOT / "formal" / "STATUS.md").read_text(encoding="utf-8").split(
            "<!-- BEGIN GENERATED alignment")[1]


# ------------------------------------------------ the coverage ratchet

def test_the_coverage_ratchet_is_satisfied(harness, tsv):
    harness.reference_from_tsv(tsv)
    assert harness.approval_coverage() == []


def test_the_ratchet_bites_without_an_edge_in_the_corpus(harness, tsv):
    """Every approval refusal but one is refused with no edge. With the `AE`
    rows gone, the row never saw an edge, covering or not."""
    without = [row for row in tsv if not row.startswith("AE\t")]
    harness.reference_from_tsv(without)
    findings = harness.approval_coverage()
    harness.reference_from_tsv(tsv)
    assert any("edge covers the token" in f for f in findings)
    assert any("does not cover it" in f for f in findings)


# ------------------------------- issue #1455's comment: a scoped host emission

@pytest.fixture(scope="module")
def scoped(harness, tmp_path_factory):
    root = tmp_path_factory.mktemp("scoped_host")
    return root, _synthetic(harness, root, {
        "provider.rvl": SCOPED_PROVIDER,
        "through_fn.rvl": SCOPED_THROUGH_FN,
        "under_holder.rvl": SCOPED_CHILD_UNDER_HOLDER,
    })


def test_the_scoped_host_programs_are_what_the_checker_says():
    assert _checker(SCOPED_PROVIDER, "provider.rvl") == ("accept", "")
    assert _checker(SCOPED_THROUGH_FN, "through_fn.rvl") == ("accept", "")
    assert _checker(SCOPED_CHILD_UNDER_HOLDER, "under_holder.rvl") == (
        "G4", "capability-attenuation")


def test_a_scoped_host_emission_carries_its_token_on_the_bound(scoped):
    """The bound column names `net`, the token `_method_emissions` measures,
    directly and through a `fn`. The fold column stays `*`."""
    _root, (rows, _facts, ref, formal) = scoped
    for rel in ("corpus/provider.rvl", "corpus/through_fn.rvl"):
        pairs = {(r[6], r[7]) for r in (x.split("\t") for x in rows)
                 if r[0] == "F" and r[1] == rel}
        assert pairs == {("*", "net")}, rel
        assert ref.providers[(rel, "C", "k", "K", "f")] == "ok"
        if formal is not None:
            assert formal.providers[(rel, "C", "k", "K", "f")] == "ok"


def test_the_scoped_providers_file_under_agree_accept(harness, scoped):
    root, (_rows_, facts, ref, _formal) = scoped
    buckets, fatal = _align(harness, root, root, list(facts), ref)
    assert buckets["corpus/provider.rvl"] == "agree-accept"
    assert buckets["corpus/through_fn.rvl"] == "agree-accept"
    assert buckets["corpus/under_holder.rvl"] == "agree-G4"
    assert fatal == []


def test_the_attenuation_fold_keeps_the_checker_s_star(scoped):
    """The parent holds `pay` and the checker still refuses the spawn, so the
    child's reach on the fold is `*`, not `pay`."""
    _root, (rows, _facts, ref, _formal) = scoped
    rel = "corpus/under_holder.rvl"
    reach = {r[3] for r in (x.split("\t") for x in rows)
             if r[0] == "A" and r[1] == rel and r[2] == "Worker"}
    held = {r[4] for r in (x.split("\t") for x in rows)
            if r[0] == "K" and r[1] == rel and r[2] == "Supervisor"}
    assert reach == {"*"} and held == {"pay"}
    assert ref.spawns[(rel, "Supervisor", "Worker")] == "fail"


def test_the_token_table_follows_the_checker_s_fixed_point(harness):
    """`_emitting_tokens` against `emission_analysis._emitting_capabilities`
    on one program: a scoped extern is its scope, an unscoped one its name,
    a `fn` the union of its callees, and a first-class reference adds `*`."""
    from revl.compiler import compile_source
    from revl.emission_analysis import _emitting_capabilities
    from revl.parser import Parser

    source = """\
extern emission[net] fn zz_write(t: Str) -> Int = @py { return 1 }
extern emission fn wire(t: Str) -> Int = @py { return 1 }
fn relay(t: Str) -> Int { return zz_write(t) + wire(t) }
fn apply(f: (Str) -> Int, t: Str) -> Int { return f(t) }
fn hand(t: Str) -> Int { return apply(zz_write, t) }
service K { fn f() -> Int }
component C provides k: K { provide k { fn f() = 1 } }
"""
    tokens = harness._emitting_tokens(Parser(source, "t.rvl").parse())
    ir = compile_source(source, "t.rvl")
    reference = _emitting_capabilities(ir["functions"], ir["externs"])
    assert tokens["zz_write"] == {"net"}
    assert tokens["wire"] == {"wire"}
    assert tokens["relay"] == {"net", "wire"}
    assert tokens["hand"] == {"*", "net"}
    assert tokens == reference


# ------------------------------------------------ the Lean side, as text

def test_the_lean_side_is_registered_and_gated():
    """Every `RevL.G4Approval` theorem `CheckAxioms.lean` prints is in
    `run_gate.sh`'s argv, in the registry, and stated in the L2 file; the
    oracle decides the row with `approvalRowB` and its bridge is in the
    second argv list."""
    lean = LEAN.read_text(encoding="utf-8")
    gate = GATE.read_text(encoding="utf-8")
    check = re.findall(
        r"^#print axioms (RevL\.G4Approval\.[A-Za-z0-9_']+)\s*$",
        CHECK.read_text(encoding="utf-8"), re.MULTILINE)
    assert check, "CheckAxioms.lean prints no RevL.G4Approval theorem"
    registry = {line.split("\t")[0]
                for line in REGISTRY.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#")}
    for name in check:
        short = name.rsplit(".", 1)[1]
        assert re.search(rf"^theorem {short}\b", lean, re.MULTILINE), name
        assert f"  {name}" in gate, name
        assert name in registry, name
    for name in ("RevL.G4Approval.crossingB_iff",
                 "RevL.G4Approval.no_edge_iff_nothing_required",
                 "RevL.G4Approval.uncovered_required_refused",
                 "RevL.G4Approval.approval_not_vacuous"):
        assert name in check
    oracle = ORACLE.read_text(encoding="utf-8")
    assert "RevL.G4Approval.crossingB required ⟨tokens, edge⟩" in oracle
    assert 'if approvalRowB required toks edge then "ok" else "fail"' in oracle
    assert "#print axioms RevLOracle.approvalRowB_iff" in oracle
    assert "  RevLOracle.approvalRowB_iff" in gate
