"""`ensures`: a function's spec, checked as a generated property test (issue #2705).

A `fn` can state what it is supposed to do:

    fn min0(x: Int) -> Int ensures result <= 0 {
      if (x < 0) { return x }
      return 0
    }

The clause is SUGAR over the existing `prop test` (roadmap item 37): lowering
expands it to a property over the function's parameter types whose body binds
`result` to one call of the function and asserts the clause. The type-derived
generator, the coverage fold, the shrinker and the runner are unchanged, so a
counterexample is reported as the minimal input that violates the clause.

`revl audit` lists each spec and the result of the last RECORDED check
(`revl test --report json` piped to `--spec-results`); the audit is static, so
a spec no run has checked yet reads `unrun`, never a guessed pass.
"""

import json

import pytest

from revl import RevlError, compile_source
from revl import fault as fault_mod
from revl.audit_diff import audit_report, spec_results_from_report


PASSING = """
fn min0(x: Int) -> Int ensures result <= 0 {
  if (x < 0) {
    return x
  }
  return 0
}
"""

# the same body with an off-by-one: at x = 0 it returns 1, violating `<= 0`
OFF_BY_ONE = """
fn min1(x: Int) -> Int ensures result <= 0 {
  if (x < 0) {
    return x
  }
  return 1
}
"""


# ---------------------------------------------------------------------------
# frontend: the clause parses and lowers to a prop unit
# ---------------------------------------------------------------------------

def test_ensures_lowers_to_a_generated_property_over_the_parameter_types():
    ir = compile_source(PASSING, "t.rvl")
    units = [u for u in ir["prop_tests"] if u.get("spec_of")]
    assert len(units) == 1
    unit = units[0]
    assert unit["name"] == "min0 ensures"
    assert unit["spec_of"] == "min0"
    assert unit["spec"] == "result <= 0"
    assert unit["params"] == [{"name": "x", "type": "Int"}]
    # the generated body binds `result` to the call, then asserts the clause
    assert unit["body"][0]["step"] == "let"
    assert unit["body"][-1]["step"] == "assert"


def test_a_fn_without_a_spec_adds_no_prop_unit():
    # byte-compatibility: the common fn's IR is exactly what it was before
    ir = compile_source("fn f(x: Int) -> Int { return x }", "t.rvl")
    assert "prop_tests" not in ir


def test_ensures_is_contextual_and_stays_usable_as_an_identifier():
    src = """
    fn pick(ensures: Int) -> Int ensures result == ensures {
      return ensures
    }
    """
    ir = compile_source(src, "t.rvl")
    unit = next(u for u in ir["prop_tests"] if u.get("spec_of") == "pick")
    assert unit["params"] == [{"name": "ensures", "type": "Int"}]
    assert unit["spec"] == "result == ensures"


def test_the_spec_can_call_the_function_by_name_instead_of_using_result():
    src = """
    fn inc(x: Int) -> Int ensures inc(x) == x + 1 {
      return x + 1
    }
    """
    unit = next(u for u in compile_source(src, "t.rvl")["prop_tests"]
                if u.get("spec_of") == "inc")
    assert unit["spec"] == "inc(x) == x + 1"


@pytest.mark.parametrize("source, message", [
    ("fn f(result: Int) -> Int ensures result == result { return result }",
     "cannot be named `result`"),
    ("fn f() -> Int ensures 1 > 0 { return 1 }",
     "needs at least one parameter"),
    ("fn id[T](x: Int) -> Int ensures result == x { return x }",
     "needs concrete parameter types"),
    # `result` would name the function itself inside the generated property,
    # which then fails at run time instead of at compile time
    ("fn result(x: Int) -> Int ensures result == x { return x }",
     "a function with a spec cannot be named `result`"),
])
def test_a_spec_shape_the_expansion_cannot_express_is_refused(source, message):
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, "t.rvl")
    assert message in str(excinfo.value)


@pytest.mark.parametrize("source, where", [
    ("service S { fn f(x: Int) -> Int ensures result == x }", "a service operation"),
    ("service S { fn f(x: Int) -> Int }\n"
     "component C provides s: S {\n"
     "  provide s { fn f(x) ensures result == x { return x } }\n"
     "}", "a provided method"),
    ("extern pure fn f(x: Int) -> Int ensures result == x = @py { return x }",
     "an extern"),
])
def test_a_spec_outside_a_top_level_fn_is_refused_by_name(source, where):
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, "t.rvl")
    assert ("a spec (`ensures`) is only supported on a top-level `fn`, "
            f"not on {where}") in str(excinfo.value)


def test_a_user_prop_test_cannot_share_the_generated_property_name():
    src = """
    fn f(x: Int) -> Int ensures result == x { return x }
    prop test "f ensures" (x: Int) { assert x == x }
    """
    with pytest.raises(RevlError) as excinfo:
        compile_source(src, "t.rvl")
    assert "already declared" in str(excinfo.value)


# ---------------------------------------------------------------------------
# the check: a correct body holds; the off-by-one fails, shrunk to the minimum
# ---------------------------------------------------------------------------

def _run(source: str):
    ir = compile_source(source, "t.rvl")
    return fault_mod.run_prop_units(ir, fault_mod.prop_units(ir),
                                    out=lambda _line: None)


def test_a_correct_body_holds_at_every_generated_input():
    failures, dossier = _run(PASSING)
    assert failures == 0
    prop = dossier["properties"][0]
    assert prop["name"] == "min0 ensures"
    assert prop["status"] == "pass"
    assert prop["rounds"] > 9  # the nine i64 edges plus the random rounds


def test_an_off_by_one_fails_with_the_minimal_counterexample():
    failures, dossier = _run(OFF_BY_ONE)
    assert failures == 1
    prop = dossier["properties"][0]
    assert prop["status"] == "fail"
    assert prop["counterexample"]["args"] == "x=0"
    assert "result <= 0" in prop["counterexample"]["reason"]


def test_the_test_runner_prints_the_spec_and_the_shrunk_counterexample(
        tmp_path, capsys):
    from revl.__main__ import main

    src = tmp_path / "spec.rvl"
    src.write_text(OFF_BY_ONE + "\n" + PASSING, encoding="utf-8")
    assert main(["test", str(src)]) == 1
    out = capsys.readouterr().out
    assert "FAIL min1 ensures: property is false" in out
    assert "counterexample (shrunk): x=0" in out
    assert "PASS min0 ensures: property held" in out


# ---------------------------------------------------------------------------
# audit: the spec and the last RECORDED result
# ---------------------------------------------------------------------------

SPEC_FNS = PASSING + "\n" + OFF_BY_ONE


def test_the_audit_lists_the_spec_and_unrun_when_nothing_was_recorded():
    report = audit_report(compile_source(SPEC_FNS, "t.rvl"))
    rows = {row["function"]: row for row in report["function_specs"]}
    assert set(rows) == {"min0", "min1"}
    assert rows["min1"]["spec"] == "result <= 0"
    assert rows["min1"]["last_result"] == "unrun"


def test_the_audit_lists_the_last_recorded_result(tmp_path, capsys):
    from revl.__main__ import main

    src = tmp_path / "spec.rvl"
    src.write_text(SPEC_FNS, encoding="utf-8")
    report_path = tmp_path / "report.json"
    assert main(["test", "--report", "json", str(src)]) == 1
    report_path.write_text(capsys.readouterr().out, encoding="utf-8")

    assert main(["audit", "--spec-results", str(report_path), str(src)]) == 0
    out = capsys.readouterr().out
    assert "fn min0 ensures result <= 0  (last result: pass)" in out
    assert "fn min1 ensures result <= 0  (last result: fail)" in out

    assert main(["audit", "--json", "--spec-results", str(report_path),
                 str(src)]) == 0
    body = json.loads(capsys.readouterr().out)
    rows = {row["function"]: row for row in body["function_specs"]}
    assert rows["min1"]["last_result"] == "fail"
    assert rows["min1"]["counterexample"]["args"] == "x=0"


def test_a_test_report_without_a_spec_section_leaves_every_spec_unrun():
    report = audit_report(compile_source(SPEC_FNS, "t.rvl"),
                          spec_results_from_report({"tests": []}))
    assert {row["last_result"] for row in report["function_specs"]} == {"unrun"}


def test_a_document_without_specs_keeps_its_audit_body_byte_compatible():
    report = audit_report(compile_source("fn f(x: Int) -> Int { return x }",
                                         "t.rvl"))
    assert "function_specs" not in report


# ---------------------------------------------------------------------------
# the spec is evidence, never a substitute for the effect checks
# ---------------------------------------------------------------------------

def test_a_passing_spec_does_not_admit_an_undeclared_capability():
    source = """
    fn min0(x: Int) -> Int ensures result <= 0 {
      if (x < 0) { return x }
      return 0
    }

    service Database { emission fn execute(sql: Str) -> Int }
    service Bus { emission fn publish(topic: Str, payload: Str) }
    service Cache { emission[db] fn put(key: Str, value: Str) }

    component LeakyCache requires db: Database, bus: Bus provides cache: Cache {
      provide cache {
        fn put(key, value) {
          emit db.execute(key)
          emit bus.publish("cache.put", key)
        }
      }
    }
    """
    with pytest.raises(RevlError) as excinfo:
        compile_source(source, "t.rvl")
    assert excinfo.value.code == "G4"
    assert "emits through `bus`" in str(excinfo.value)


def test_a_recorded_result_from_a_different_spec_spelling_is_not_attributed():
    report = {"function_specs": [
        {"function": "min0", "spec": "result < 0", "status": "pass"}]}
    rows = audit_report(compile_source(PASSING, "t.rvl"),
                        spec_results_from_report(report))["function_specs"]
    assert next(row for row in rows if row["function"] == "min0")["last_result"] == "unrun"


CLAMP = """
fn clamp(x: Int) -> Int ensures result <= 0 {
  if (x < 0) {
    return x
  }
  return 0
}
"""


def _record(tmp_path, capsys, name: str, source: str):
    """Write ``source`` to ``name``, run `revl test --report json` on it and
    return (source path, report path)."""
    from revl.__main__ import main

    src = tmp_path / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(source, encoding="utf-8")
    code = main(["test", "--report", "json", str(src)])
    report = src.with_suffix(".report.json")
    report.write_text(capsys.readouterr().out, encoding="utf-8")
    return code, src, report


def _audit_line(capsys, src, report, fn: str = "clamp") -> str:
    from revl.__main__ import main

    assert main(["audit", "--spec-results", str(report), str(src)]) == 0
    return next(line.strip() for line in capsys.readouterr().out.splitlines()
                if line.strip().startswith(f"fn {fn} ensures"))


def test_a_recorded_pass_is_not_shown_after_the_body_changes(tmp_path, capsys):
    from revl.__main__ import main

    code, src, report = _record(tmp_path, capsys, "spec.rvl", CLAMP)
    assert code == 0
    assert _audit_line(capsys, src, report).endswith("(last result: pass)")
    # the same signature and clause over a body that now breaks the spec
    src.write_text(CLAMP.replace("return 0", "return 5"), encoding="utf-8")
    assert main(["test", str(src)]) == 1
    capsys.readouterr()
    assert _audit_line(capsys, src, report).endswith("(last result: unrun)")


def test_a_record_from_another_file_with_the_same_fn_does_not_match(tmp_path, capsys):
    code, _, report = _record(tmp_path, capsys, "a/spec.rvl", CLAMP)
    assert code == 0
    other = tmp_path / "b" / "spec.rvl"
    other.parent.mkdir()
    other.write_text(CLAMP, encoding="utf-8")
    assert _audit_line(capsys, other, report).endswith("(last result: unrun)")


def test_a_record_without_a_digest_does_not_match(tmp_path, capsys):
    code, src, report = _record(tmp_path, capsys, "spec.rvl", CLAMP)
    assert code == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    row = next(r for r in payload["function_specs"] if r["function"] == "clamp")
    assert row["status"] == "pass"
    row.pop("digest", None)
    report.write_text(json.dumps(payload), encoding="utf-8")
    assert _audit_line(capsys, src, report).endswith("(last result: unrun)")


HELPER = """
fn helper(x: Int) -> Int { return 0 }
fn clamp(x: Int) -> Int ensures result <= 0 { return helper(x) }
"""


def test_a_recorded_pass_is_not_shown_after_a_callee_changes(tmp_path, capsys):
    from revl.__main__ import main

    code, src, report = _record(tmp_path, capsys, "spec.rvl", HELPER)
    assert code == 0
    assert _audit_line(capsys, src, report).endswith("(last result: pass)")
    # the fn's own text is unchanged; only the helper it calls now breaks it
    src.write_text(HELPER.replace("{ return 0 }", "{ return 5 }"), encoding="utf-8")
    assert main(["test", str(src)]) == 1
    capsys.readouterr()
    assert _audit_line(capsys, src, report).endswith("(last result: unrun)")


VARIANTS = """
type Sh = A(Int) | B
fn g(s: Sh) -> Int ensures result <= 0 {
  return match s {
    A(n) => 0,
    B => 0,
    _ => 5,
  }
}
"""


def test_a_recorded_pass_is_not_shown_after_a_parameter_type_gains_a_variant(
        tmp_path, capsys):
    from revl.__main__ import main

    code, src, report = _record(tmp_path, capsys, "spec.rvl", VARIANTS)
    assert code == 0
    assert _audit_line(capsys, src, report, "g").endswith("(last result: pass)")
    # the fn's text is unchanged; the generator now reaches the `_` arm
    src.write_text(VARIANTS.replace("A(Int) | B", "A(Int) | B | C"),
                   encoding="utf-8")
    assert main(["test", str(src)]) == 1
    capsys.readouterr()
    assert _audit_line(capsys, src, report, "g").endswith("(last result: unrun)")


def test_a_record_still_matches_when_the_checkout_moves(tmp_path, capsys,
                                                        monkeypatch):
    """The digest names the file relative to the project root (the working
    directory), so a report recorded in one checkout, on CI say, matches an
    audit of the same tree in another."""
    first = tmp_path / "one"
    first.mkdir()
    monkeypatch.chdir(first)
    code, _, report = _record(first, capsys, "spec.rvl", CLAMP)
    assert code == 0
    second = tmp_path / "two"
    second.mkdir()
    (second / "spec.rvl").write_text(CLAMP, encoding="utf-8")
    monkeypatch.chdir(second)
    line = _audit_line(capsys, second / "spec.rvl", report)
    assert line.endswith("(last result: pass)")


def test_the_ts_tier_compiles_a_spec_fn_and_ignores_the_clause(tmp_path):
    """Only the py reference tier checks a spec (it runs `prop_tests`). The ts
    tier emits the fn and the file's tests exactly as it would without the
    clause: the module is byte-identical."""
    from revl.__main__ import main

    plain = CLAMP.replace(" ensures result <= 0", "")
    assert plain != CLAMP
    outputs = []
    for name, source in (("spec.rvl", CLAMP), ("plain.rvl", plain)):
        src = tmp_path / name
        src.write_text(source + 'test "t" { assert clamp(-1) == -1 }\n',
                       encoding="utf-8")
        out = tmp_path / (name + ".ts")
        assert main(["emit", "--backend", "ts", "-o", str(out), str(src)]) == 0
        outputs.append(out.read_text(encoding="utf-8"))
    assert "export function clamp(" in outputs[0]
    assert outputs[0] == outputs[1]


def test_spec_results_is_refused_with_the_policy_and_diff_gates(tmp_path, capsys):
    from revl.__main__ import main

    src = tmp_path / "spec.rvl"
    src.write_text(PASSING, encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text('{"function_specs": []}', encoding="utf-8")
    assert main(["audit", "--spec-results", str(report),
                 "--policy", "policy.rvl", str(src)]) == 2
    assert "--spec-results" in capsys.readouterr().err
    assert main(["audit", "--spec-results", str(report),
                 "--diff", "prev.json", str(src)]) == 2
    assert "--spec-results" in capsys.readouterr().err
