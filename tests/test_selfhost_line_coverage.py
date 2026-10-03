"""The LINE-coverage gate for the self-host oracles (roadmap item 429).

`tests/test_selfhost_coverage.py` gates DISPATCH ARMS. This module gates
STATEMENTS, and it is the one the mirrored surface rests on, because an arm the
corpus reaches can have nearly all of its body unexercised. Measured: 2868 of
the 8233 unexecuted reference statements sit inside functions the corpus DOES
call — the mass a construct table structurally cannot see. The construct survey
reported 20% blind where statements report 53%.

Both sides are measured. The reference (`backends/<tier>/emit.py`) directly, it
being python; the port (`selfhost/emit_<tier>.rvl`) through the python module it
compiles to, which is how its own oracle runs it.

The mirrored pairs, spelled as paths so `tools/affected_tests.py::_tier_tests`
selects this module whenever one of them changes — new logic in a reference
emitter is precisely the event that opens a fresh uncovered region:
`backends/python/emit.py`, `backends/typescript/emit.py`, `backends/go/emit.py`,
`backends/java/emit.py`, `backends/rust/emit.py`, `backends/wasm/emit.py`.

Both halves are also driven over `tests/fixtures/emit_<tier>_refusals/`, the
documents a tier's reference REFUSES BY NAME (issue #1419). A refusal is logic
both halves carry and no `CORPUS` document can reach, because a corpus document
is one the reference emits. Five tiers have one: `emit_ts_refusals/`,
`emit_go_refusals/`, `emit_java_refusals/`, `emit_rust_refusals/` and
`emit_wasm_refusals/`.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

TIERS = ("py", "ts", "go", "java", "rust", "wasm")


@pytest.fixture(scope="module")
def lines():
    spec = importlib.util.spec_from_file_location(
        "selfhost_line_coverage_tool", ROOT / "tools" / "selfhost_line_coverage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def line_data(lines):
    return {"reference": lines.measure(), "selfhost": lines.measure_selfhost()}


def test_reference_and_selfhost_line_coverage_match_the_ledger(lines, line_data):
    """The line gate, both halves.

    Failing here means an uncovered count moved. UP: logic arrived in a mirrored
    emitter that no corpus document reaches, which is how the item-429(d)
    `Secret[T]` gap opened and stayed green. DOWN: coverage improved and the
    ratchet has to be told, or it is not a ratchet. Either way the message names
    the function; go read it before touching the ledger.
    """
    problems = lines.check(line_data)
    assert problems == [], "\n".join(problems)


@pytest.mark.parametrize("tier", TIERS)
def test_the_line_measurement_is_not_empty(lines, line_data, tier):
    """Non-vacuity again, and it matters more here than for the construct table:
    a coverage session that traced nothing reports every statement missing (a
    loud failure), but one that traced a file with no statements reports perfect
    coverage (a silent pass). Demand real statement counts on both sides."""
    reference = line_data["reference"][tier]
    port = line_data["selfhost"][tier]
    assert reference["statements"] > 500, (
        f"backends/*/emit.py for {tier} reported {reference['statements']} "
        f"statements: the coverage session is not tracing the reference")
    assert port["statements"] > 500, (
        f"the emitted selfhost/emit_{tier}.rvl reported {port['statements']} "
        f"statements: the coverage session is not tracing the port")
    assert 0 < reference["uncovered"] < reference["statements"]


def test_the_selfhost_side_has_real_declared_functions(lines, line_data):
    """Zero never-entered functions is valid when statement measurement is real."""
    for tier in TIERS:
        found = line_data["selfhost"][tier]
        assert found["declared"] > 30, (
            f"selfhost/emit_{tier}.rvl declared {found['declared']} functions: "
            f"the `.rvl` -> emitted `def` name mapping has broken")


def _entry(functions: dict, reasons: dict | None = None, budgets=None) -> dict:
    """One tier in the ledger's in-memory shape: `functions` maps a function
    to `(reason id, uncovered)`; budgets default to the counts."""
    reasons = reasons if reasons is not None else {
        rid: f"a specific decision about {rid}"
        for rid, _ in functions.values()}
    budgets = budgets or {}
    return {"reasons": reasons,
            "functions": {name: {"reason": rid, "uncovered": count,
                                 "budget": budgets.get(name, count)}
                          for name, (rid, count) in functions.items()}}


def _ledger(lines, monkeypatch, tmp_path, ledger: dict, raw: dict | None = None):
    """Write `ledger` in the on-disk layout under `tmp_path` and point the
    tool at it. `raw` maps `half/tier` to literal file text, for records the
    in-memory shape cannot express."""
    base = tmp_path / "ledger"
    lines.dump_ledger(ledger, base)
    for key, text in (raw or {}).items():
        path = base / f"{key}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    monkeypatch.setattr(lines, "LEDGER", base)
    return base


def test_generic_baseline_cannot_masquerade_as_closure(lines, monkeypatch, tmp_path):
    ledger = {half: {tier: _entry({"f": ("never-entered", 1)},
                                  {"never-entered": "NEVER ENTERED"})
                     for tier in TIERS}
              for half in ("reference", "selfhost")}
    _ledger(lines, monkeypatch, tmp_path, ledger)
    data = {half: {tier: {"functions": {}, "sizes": {}}
                   for tier in TIERS}
            for half in ("reference", "selfhost")}
    problems = lines.check(data)
    assert any("generic reason" in problem for problem in problems)


def test_line_closure_rejects_duplicates_and_missing_sides(lines, monkeypatch, tmp_path):
    raw = {"reference/py": (
        '["reason", "a", "specific decision"]\n\n'
        '["reason", "b", "another decision"]\n\n'
        '["function", "a", "f", 1, 1]\n\n'
        '["function", "b", "f", 1, 1]\n')}
    _ledger(lines, monkeypatch, tmp_path, {}, raw)
    problems = lines.check({"reference": {}, "selfhost": {}})
    assert any("appears in multiple reasons" in problem for problem in problems)
    assert any("missing line-coverage side" in problem for problem in problems)


def test_line_check_fails_closed_on_malformed_maps_and_counts(lines, monkeypatch, tmp_path):
    ledger = {half: {tier: _entry({}) for tier in TIERS}
              for half in ("reference", "selfhost")}
    raw = {"reference/py": ('["reason", "a", "specific decision"]\n\n'
                            '["function", "a", "f", "bad", 1]\n\n'
                            '{"not": "a record"}\n\n'
                            '<<<<<<< HEAD\n')}
    _ledger(lines, monkeypatch, tmp_path, ledger, raw)
    data = {half: {tier: {"functions": {"f": "bad"}, "sizes": {}}
                   for tier in TIERS}
            for half in ("reference", "selfhost")}
    problems = lines.check(data)
    assert any("invalid uncovered count" in problem for problem in problems)
    assert any("invalid measured count" in problem for problem in problems)
    assert any("not a reason or function record" in problem for problem in problems)
    assert any("unresolved merge conflict marker" in problem for problem in problems)


@pytest.mark.parametrize("functions", [None, [], {"f": True}])
def test_line_check_rejects_malformed_function_population(lines, monkeypatch, tmp_path, functions):
    ledger = {half: {tier: _entry({}, {"a": "specific decision"})
                     for tier in TIERS} for half in ("reference", "selfhost")}
    _ledger(lines, monkeypatch, tmp_path, ledger)
    data = {half: {tier: {"functions": functions, "sizes": {}}
                   for tier in TIERS} for half in ("reference", "selfhost")}
    problems = lines.check(data)
    assert any("reason `a` has no functions" in problem for problem in problems)
    assert any("survey functions are not a map" in problem
               for problem in problems) or any("invalid measured count" in problem
                                                for problem in problems)


# --------------------------------------------------------------------------
# issue #1419: the third option, and the budget.
#
# The gate had two responses to an uncovered statement: reach it with a corpus
# document, or record it. For every refusal a tier states by name, only the
# second was ever possible, because a corpus document is one the reference EMITS
# and a refusal path runs only where it does not. So that population could only
# accumulate, and it did. The tests below hold both halves of the fix: refusal
# documents really are refused (otherwise the directory buys coverage with no
# agreement assertion behind it), and `_budget` really does survive `--write`
# (otherwise the fastest green is still the complete green).


def test_every_refusal_document_is_refused_by_its_tiers_reference(lines):
    """The non-vacuity guard on the refusal corpus.

    A document in `emit_<tier>_refusals/` that the reference EMITS is a
    byte-agreement case: it belongs in that tier's `CORPUS`, where the oracle
    holds its bytes. Left in the refusal directory it would buy line coverage
    with nothing asserting the output is right, which is the shape item 429
    exists to rule out. The frontend has to accept it too: a document that does
    not compile reaches no emitter statement at all and would be a silent
    no-op here.
    """
    from revl import compile_files  # noqa: PLC0415

    found = 0
    for tier in TIERS:
        reference = lines.load_reference(tier)
        for document in lines.refusal_documents(tier):
            found += 1
            ir = compile_files([str(document)])
            with pytest.raises(reference.EmitError):
                reference.emit(ir)
    assert found > 0, (
        "no refusal documents at all: tests/fixtures/emit_<tier>_refusals/ is "
        "the only way a refusal's two mirrored halves ever get reached, and an "
        "empty one makes this gate's third option a claim rather than a fact")


def test_the_rust_required_stream_refusal_is_reached_rather_than_recorded(lines):
    """Issue #1419's own red, held at the fix rather than at the symptom.

    `selfhost/emit_rust.rvl::require_ty` renders a requirement's type and marks a
    `Stream[T]` one instead of splicing it in verbatim; `backends/rust/emit.py::
    _refuse_required_stream` is the reference half that raises. Both were
    recorded as unreachable. Neither is in the ledger now, and this asserts the
    reason: the document that reaches them exists, and it is not a corpus
    document because it cannot be one.
    """
    recorded = lines._load_ledger()
    for half, tier, name in (("reference", "rust", "_refuse_required_stream"),
                             ("selfhost", "rust", "require_ty")):
        entry = recorded[half][tier]["functions"]
        assert name not in entry, (
            f"{half}/{tier} records `{name}` again. It is reached by "
            f"tests/fixtures/emit_rust_refusals/, so recording it is recording "
            f"something that is not true (issue #1419)")
    assert "required_stream_coeffect.rvl" in [
        p.name for p in lines.refusal_documents("rust")]


def test_the_budget_fires_in_both_directions(lines, monkeypatch, tmp_path):
    """Recording is not free, and an improvement is not re-spendable.

    Above the budget the gate names the overrun and asks for the raise by hand.
    Below it the gate asks for the ceiling to come down, which is the half that
    makes the direction stick: headroom left behind by a fix is exactly what the
    next unreached region spends without anyone deciding to.
    """
    def ledger_with(budget):
        return {half: {tier: _entry({"f": ("a", 10)}, budgets={"f": budget})
                       for tier in TIERS}
                for half in ("reference", "selfhost")}
    survey = {half: {tier: {"functions": {"f": 10}, "sizes": {"f": 10}}
                     for tier in TIERS}
              for half in ("reference", "selfhost")}

    _ledger(lines, monkeypatch, tmp_path / "over", ledger_with(4))
    over = lines.check(survey)
    assert any("its budget allows 4" in problem for problem in over)

    _ledger(lines, monkeypatch, tmp_path / "under", ledger_with(40))
    under = lines.check(survey)
    assert any("still allows 40" in problem for problem in under)

    _ledger(lines, monkeypatch, tmp_path / "exact", ledger_with(10))
    exact = lines.check(survey)
    assert not any("budget" in problem for problem in exact)


def test_the_budget_is_per_function_so_a_rise_cannot_hide_behind_a_fall(
        lines, monkeypatch, tmp_path):
    """The per-tier budget let one function's rise ride on another's fall:
    the tier's mass stayed put and only `--write` was needed. Per function,
    both moves need a hand edit, and each names its function (issue #1768)."""
    ledger = {half: {tier: _entry({"f": ("a", 12), "g": ("a", 8)},
                                  budgets={"f": 10, "g": 10})
                     for tier in TIERS}
              for half in ("reference", "selfhost")}
    _ledger(lines, monkeypatch, tmp_path, ledger)
    problems = lines.check({half: {tier: {"functions": {"f": 12, "g": 8},
                                          "sizes": {"f": 12, "g": 8}}
                                   for tier in TIERS}
                            for half in ("reference", "selfhost")})
    assert any("`f` records 12" in p and "allows 10" in p for p in problems)
    assert any("`g` records 8" in p and "still allows 10" in p for p in problems)


def test_a_missing_budget_is_itself_a_failure(lines, monkeypatch, tmp_path):
    """Fail closed. A function with no budget is the state this gate was in
    before #1419, and it has to be a failure rather than a default, or
    deleting one field is a way to make recording free again."""
    raw = {f"{half}/{tier}": ('["reason", "a", "a specific decision"]\n\n'
                              '["function", "a", "f", 1, null]\n')
           for tier in TIERS for half in ("reference", "selfhost")}
    _ledger(lines, monkeypatch, tmp_path, {}, raw)
    problems = lines.check({half: {tier: {"functions": {"f": 1}, "sizes": {"f": 1}}
                                   for tier in TIERS}
                            for half in ("reference", "selfhost")})
    assert sum("has no budget" in problem for problem in problems) == 2 * len(TIERS)


def test_write_does_not_touch_the_budget(lines, monkeypatch, tmp_path):
    """The mechanism has to survive the thing that keeps happening: the gate
    fires, `--write` is the fastest green, and `--write` is always available.

    So `--write` stays available and stops being sufficient. It rewrites every
    per-function count and leaves the budget alone, which leaves `--check` red
    with one number named. Written as an assertion because a future regeneration
    that folded the budget in would restore the old incentive silently.
    """
    base = _ledger(lines, monkeypatch, tmp_path,
                   {half: {tier: _entry({"stdlib_f": ("a", 1)})
                           for tier in TIERS}
                    for half in ("reference", "selfhost")})
    lines.write_ledger({half: {tier: {"statements": 99,
                                      "functions": {"stdlib_f": 40, "new_g": 3},
                                      "sizes": {"stdlib_f": 40, "new_g": 3}}
                               for tier in TIERS}
                        for half in ("reference", "selfhost")})
    rewritten = lines._load_ledger(base)
    assert rewritten["reference"]["py"]["functions"]["stdlib_f"] == {
        "uncovered": 40, "budget": 1, "reason": "a"}, (
        "`--write` moved the budget, so `--write` is a complete answer again")
    assert rewritten["reference"]["py"]["functions"]["new_g"]["budget"] is None
    problems = lines.check({half: {tier: {"functions": {"stdlib_f": 40, "new_g": 3},
                                          "sizes": {"stdlib_f": 40, "new_g": 3}}
                                   for tier in TIERS}
                            for half in ("reference", "selfhost")})
    assert any("its budget allows 1" in problem for problem in problems)
    assert any("`new_g` has no budget" in problem for problem in problems)


# --------------------------------------------------------------------------
# issue #1768: the layout merges.
#
# The ledger used to be one JSON file whose six adjacent `_budget` lines and
# per-tier totals every pull request that moved a count rewrote, so after each
# landing nearly every open pull request touching an emitter conflicted there.
# The tests below hold the layout that replaced it.

from _merge_tree import git_has_merge_tree, merge  # noqa: E402

_needs_merge_tree = pytest.mark.skipif(
    not git_has_merge_tree(), reason="git merge-tree --write-tree needs git 2.38")


def _tier_texts(lines, ledger) -> dict[str, str]:
    return {f"{half}/{tier}.jsonl": lines.tier_text(entry)
            for half in ("reference", "selfhost")
            for tier, entry in ledger.get(half, {}).items()}


def _bump(ledger, half, tier, name, by):
    out = json.loads(json.dumps(ledger))
    f = out[half][tier]["functions"][name]
    f["uncovered"] += by
    f["budget"] += by
    return out


def _old_layout(ledger) -> dict[str, str]:
    """The same ledger as the pre-#1768 single JSON file laid it out."""
    raw = {"_budget": {}}
    for half in ("reference", "selfhost"):
        raw["_budget"][half] = {}
        raw[half] = {}
        for tier, entry in ledger[half].items():
            grouped = {}
            for name, f in entry["functions"].items():
                grouped.setdefault(entry["reasons"][f["reason"]], {})[name] = f["uncovered"]
            mass = sum(f["uncovered"] for f in entry["functions"].values())
            raw["_budget"][half][tier] = mass
            raw[half][tier] = {"statements": 1000, "uncovered_statements": mass,
                               "uncovered": {r: dict(sorted(v.items()))
                                             for r, v in sorted(grouped.items())}}
    return {"selfhost_uncovered_lines.json":
            json.dumps(raw, indent=2, sort_keys=True) + "\n"}


def test_the_committed_ledger_is_in_canonical_record_form(lines):
    """Sorted, one record per line, a blank line between records, byte for
    byte what `--write` lays out, and no stored total."""
    ledger = lines._load_ledger()
    assert ledger["_problems"] == []
    for rel, text in _tier_texts(lines, ledger).items():
        on_disk = (lines.LEDGER / rel).read_text(encoding="utf-8")
        assert on_disk == text, f"{rel} is not in canonical record form"
        assert all(line == "" for line in on_disk.split("\n")[1::2])
    assert sorted(p.relative_to(lines.LEDGER).as_posix()
                  for p in lines.LEDGER.rglob("*.jsonl")) == sorted(
        f"{half}/{tier}.jsonl" for half in ("reference", "selfhost")
        for tier in TIERS)


@_needs_merge_tree
def test_independent_budget_changes_merge_in_the_new_layout_only(lines):
    """The exit test of issue #1768, on the ledger as committed today.

    Two pull requests raise the counts (and, by hand, the budgets) of two
    different functions of the SAME emitter, the commonest pair in the issue's
    measurement. In the records they edit different lines and merge. In the
    single file they replace, both edits rewrite that tier's `_budget` line and
    its `uncovered_statements` total, and conflict."""
    ledger = lines._load_ledger()
    names = sorted(ledger["reference"]["py"]["functions"])
    left = _bump(ledger, "reference", "py", names[0], 3)
    right = _bump(ledger, "reference", "py", names[-1], 5)
    clean, conflicted = merge(_tier_texts(lines, ledger),
                              _tier_texts(lines, left), _tier_texts(lines, right))
    assert clean, conflicted

    clean, conflicted = merge(_old_layout(ledger), _old_layout(left),
                              _old_layout(right))
    assert not clean and conflicted == ["selfhost_uncovered_lines.json"], (
        "the old layout merged, so this test no longer measures the defect")

    # Different emitters: the old file put the six budgets on adjacent lines,
    # so `py` and `rust` (neighbours in sorted order) collided too.
    other = _bump(ledger, "reference", "rust",
                  sorted(ledger["reference"]["rust"]["functions"])[0], 2)
    clean, _ = merge(_tier_texts(lines, ledger), _tier_texts(lines, left),
                     _tier_texts(lines, other))
    assert clean
    clean, _ = merge(_old_layout(ledger), _old_layout(left), _old_layout(other))
    assert not clean


@_needs_merge_tree
def test_neighbouring_functions_merge_and_the_same_function_conflicts(lines):
    ledger = lines._load_ledger()
    entry = ledger["selfhost"]["rust"]
    by_reason = {}
    for name, f in entry["functions"].items():
        by_reason.setdefault(f["reason"], []).append(name)
    first, second = sorted(next(v for v in by_reason.values() if len(v) > 1))[:2]
    left = _bump(ledger, "selfhost", "rust", first, 1)
    right = _bump(ledger, "selfhost", "rust", second, 1)
    clean, conflicted = merge(_tier_texts(lines, ledger),
                              _tier_texts(lines, left), _tier_texts(lines, right))
    assert clean, conflicted
    clean, conflicted = merge(_tier_texts(lines, ledger), _tier_texts(lines, left),
                              _tier_texts(lines, _bump(ledger, "selfhost", "rust",
                                                       first, 2)))
    assert not clean and conflicted == ["selfhost/rust.jsonl"]


def test_a_forgotten_count_or_budget_still_fails(lines, monkeypatch, tmp_path):
    """The currency check is exactly as strict on the new layout: a moved count
    that was not recorded fails, and a recorded count whose budget was not
    raised by hand fails."""
    ledger = lines._load_ledger()
    survey = {half: {tier: {"functions": lines._flatten(ledger[half][tier]),
                            "sizes": {}}
                     for tier in TIERS} for half in ("reference", "selfhost")}
    name = sorted(ledger["reference"]["ts"]["functions"])[0]
    _ledger(lines, monkeypatch, tmp_path / "same", ledger)
    assert lines.check(survey) == []

    moved = json.loads(json.dumps(survey))
    moved["reference"]["ts"]["functions"][name] += 1
    assert any(f"`{name}` went from" in p for p in lines.check(moved))

    recorded = json.loads(json.dumps(ledger))
    recorded["reference"]["ts"]["functions"][name]["uncovered"] += 1
    _ledger(lines, monkeypatch, tmp_path / "recorded", recorded)
    assert any(f"`{name}` records" in p and "allows" in p
               for p in lines.check(moved))


def test_write_resolves_a_conflicted_file(lines, monkeypatch, tmp_path):
    """The one-command resolution: `--write` reads through conflict markers,
    keeps the first copy of a doubled record and rewrites the file clean."""
    raw = {"reference/py": ('["reason", "a", "a specific decision"]\n\n'
                            '<<<<<<< ours\n'
                            '["function", "a", "f", 2, 2]\n'
                            '=======\n'
                            '["function", "a", "f", 3, 3]\n'
                            '>>>>>>> theirs\n')}
    base = _ledger(lines, monkeypatch, tmp_path,
                   {half: {tier: _entry({"f": ("a", 1)}) for tier in TIERS}
                    for half in ("reference", "selfhost")}, raw)
    lines.write_ledger({half: {tier: {"functions": {"f": 3}, "sizes": {"f": 3}}
                               for tier in TIERS}
                        for half in ("reference", "selfhost")})
    text = (base / "reference" / "py.jsonl").read_text()
    assert "<<<<<<<" not in text and text.count('"function"') == 1
    assert lines._load_ledger(base)["reference"]["py"]["functions"]["f"] == {
        "uncovered": 3, "budget": 2, "reason": "a"}
