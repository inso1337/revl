"""The guarantee x tier matrix is generated, total, and its gate can fire.

`tools/tier_guarantees.py` derives, per guarantee and per tier, one of `proved`
/ `divergence` / `no reproducer` / `unimplemented` from the checker's own
registers (issue #1197, roadmap item 523). A generated support table is only
worth more than a hand-written one if the thing that generates it REFUSES the
cases a hand-written one would have rendered quietly, so most of this file is
the refusals, each constructed rather than hoped for:

  * a guarantee added with no reproducer and no acknowledgement;
  * an acknowledgement left behind after the gap it excused closed;
  * a parity subject added to the marker check with no decision here;
  * a runtime divergence pinned with no guarantee to hang it on;
  * a gate tag aliased to a code the compiler's register does not carry;
  * a hand edit to the committed block.

One rule here is not a refusal but a measurement, and it is the one issue
#1190 made necessary: a gate TAG names a construct and a construct can span
several guarantees, so a tag in `SELFHOST_TAG_CODES` is credited to a row only
when the gate's sentence is the reference's sentence. The two tests at the end
of the refusal block hold both halves of that.

Each of those has a test that asserts the failure, and
`test_the_unmodified_tree_generates` is the control: it passes on the same tree
every one of them fails on, so a broken fixture cannot masquerade as a working
gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "tools"), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import conformance  # noqa: E402
import tier_guarantees as tg  # noqa: E402


@pytest.fixture(scope="module")
def data():
    return tg.matrix()


# ------------------------------------------------------------------ the shape

def test_the_unmodified_tree_generates(data):
    """THE CONTROL. The generator succeeds, with a cell for every guarantee on
    every tier, on exactly the tree every refusal test below fails on."""
    from revl.diagnostics import GUARANTEES

    assert {row["code"] for row in data["rows"]} == set(GUARANTEES)
    assert data["tiers"][-1] == tg.SELFHOST_TIER
    assert len(data["tiers"]) == len(conformance.TIERS) + 1
    for row in data["rows"]:
        for tier in data["tiers"]:
            cell = row["cells"][tier]
            assert cell["verdict"] in tg._CELL, (row["code"], tier)
            assert cell["why"].strip(), (
                f"{row['code']} on {tier} has a verdict and no reason; a cell "
                "that cannot say why is the blank row this gate exists to stop")


def test_every_cell_links_or_explains_its_evidence(data):
    """A verdict with no path to check it against is an opinion."""
    for row in data["rows"]:
        evidence = row["evidence"]
        assert evidence, f"{row['code']} has no evidence path"
        assert (ROOT / evidence).exists(), (row["code"], evidence)


def test_the_rows_are_the_compilers_own_register():
    """Not a copy of it. A code added to `revl.diagnostics.GUARANTEES` shows up
    here with no edit to the matrix, which is what makes a missing row
    impossible rather than merely unlikely."""
    from revl.diagnostics import GUARANTEES

    assert tg.guarantee_codes() and set(tg.guarantee_codes()) == set(GUARANTEES)


def test_the_block_carries_no_line_numbers(data):
    """A roadmap line number in the block would re-stale docs/conformance.md on
    every unrelated roadmap edit, and a generated file that reds the build for
    somebody else's commit gets regenerated without being read."""
    block = tg.markdown(data)
    assert "L4" not in block and "L1" not in block


# ------------------------------------------------- the gate, made to fire

def test_a_guarantee_with_no_row_fails_generation(monkeypatch):
    """THE CASE THE ISSUE NAMES: a guarantee arrives and some tier has nothing
    to say about it. The generator must refuse, not render a blank."""
    from revl import diagnostics

    monkeypatch.setitem(diagnostics.GUARANTEES, "G99",
                        "a guarantee nothing in the tree enforces")
    with pytest.raises(tg.MatrixError) as caught:
        tg.matrix()
    message = str(caught.value)
    assert "G99" in message
    assert "ACKNOWLEDGED" in message


def test_a_stale_acknowledgement_fails_generation(monkeypatch):
    """The other direction. An excuse that outlives its reason is exactly how a
    support table rots, so the ratchet refuses a spent entry too."""
    monkeypatch.setitem(tg.ACKNOWLEDGED, "G1", "a gap that has since closed")
    with pytest.raises(tg.MatrixError) as caught:
        tg.matrix()
    assert "G1" in str(caught.value)


def test_a_parity_subject_with_no_decision_fails_generation(monkeypatch):
    """`--check-tier-parity` gaining a subject must force a decision here.

    Otherwise the marker check would start speaking about a guarantee this
    matrix silently ignores, which is the same invisible-gap bug one level up.
    """
    import check_roadmap_markers as markers

    monkeypatch.setattr(markers, "TIER_SUBJECTS",
                        markers.TIER_SUBJECTS + ("quarantine",))
    with pytest.raises(tg.MatrixError) as caught:
        tg.parity_divergences(conformance.TIERS)
    assert "quarantine" in str(caught.value)
    assert "SUBJECT_CODES" in str(caught.value)


def test_a_tag_aliased_to_an_unregistered_code_fails_generation(monkeypatch):
    """`SELFHOST_TAG_CODES` names a code the compiler's register does not carry.

    Such an entry can never match a row, so it credits nothing while reading
    like a decision somebody made. Same direction as every other refusal here:
    say so rather than generate around it.
    """
    monkeypatch.setitem(tg.SELFHOST_TAG_CODES, "COUNCIL",
                        frozenset({"G-COUNCIL-SPILT"}))
    with pytest.raises(tg.MatrixError) as caught:
        tg.matrix()
    message = str(caught.value)
    assert "G-COUNCIL-SPILT" in message
    assert "SELFHOST_TAG_CODES" in message


# ------------------------------------------- a gate tag is not a guarantee

def test_the_council_tag_stands_for_every_code_the_council_raises():
    """One gate FAMILY, two guarantees, and the table has to say both.

    `selfhost/lower.rvl` tags every council refusal `COUNCIL` because the tag
    names the CONSTRUCT (`docs/design/556-model-council-selfhost.md` section
    1.1), and `docs/design/557-council-disagreement.md` section 3 splits that
    one construct across two codes: the refusals whose subject is a `model
    role` keep item 512's, the rest carry the council's. Both are module
    constants in `src/revl/model_council.py`, so this reads the split off the
    module that performs it rather than restating it.

    A third code raised from that module with no third entry here would credit
    nothing and read as if it did.
    """
    from revl import model_council

    assert tg.SELFHOST_TAG_CODES["COUNCIL"] == {model_council.CODE,
                                                model_council.PLACE_CODE}


def test_an_aliased_tag_is_not_credited_on_the_tag_alone(monkeypatch):
    """The fail-open case the SET exists to keep shut.

    A tag that stands for several codes cannot say which of them a refusal is
    evidence for; the SENTENCE can. So the gate answering the right family
    with the wrong sentence must not leave the cell `proved`. It is a
    divergence, and crediting the family would credit whichever guarantee the
    reader assumed.
    """
    index = tg.reproducers()
    assert index.get("G-COUNCIL-SPLIT"), (
        "no fixture in examples/rejections/ is refused under G-COUNCIL-SPLIT; "
        "this test measures the tag-vs-sentence rule on a real reproducer")

    truthful = tg.selfhost_verdicts(index)
    assert truthful["G-COUNCIL-SPLIT"][0] == tg.PROVED

    real = tg._selfhost_admit()

    def wrong_sentence(source: str) -> str:
        answer = real(source)
        tag, sep, message = answer.partition("|")
        if tag == "COUNCIL":
            return f"{tag}{sep}{message} (and something else)"
        return answer

    monkeypatch.setattr(tg, "_selfhost_admit", lambda: wrong_sentence)
    spoiled = tg.selfhost_verdicts(index)
    assert spoiled["G-COUNCIL-SPLIT"][0] != tg.PROVED
    # and the unaliased rows are untouched: this rule reaches only the tags
    # `SELFHOST_TAG_CODES` resolves.
    assert spoiled["G1"] == truthful["G1"]


def test_an_unmapped_runtime_divergence_fails_generation(monkeypatch):
    """The pinned cross-tier register is EMPTY on main, which is the state in
    which a register silently stops being read. This proves the path is armed
    while it has nothing to say: put an entry in and generation refuses it."""
    import importlib.util
    import types

    fake = types.ModuleType("fake_xtier")
    fake.DIVERGENCES = {"a pinned runtime behaviour": ("src", {"java": "fail"})}

    real = importlib.util.spec_from_file_location
    def spec_from(name, location, *a, **k):
        if name == "tier_guarantees_xtier":
            class _Loader:
                @staticmethod
                def exec_module(module):
                    module.DIVERGENCES = fake.DIVERGENCES
            return types.SimpleNamespace(loader=_Loader(), name=name,
                                         submodule_search_locations=None,
                                         origin=str(location))
        return real(name, location, *a, **k)

    monkeypatch.setattr(importlib.util, "spec_from_file_location", spec_from)
    monkeypatch.setattr(importlib.util, "module_from_spec",
                        lambda spec: types.ModuleType(spec.name))
    with pytest.raises(tg.MatrixError) as caught:
        tg.runtime_divergences()
    assert "RUNTIME_DIVERGENCE_CODES" in str(caught.value)


def test_a_parity_finding_moves_the_cells_it_names():
    """The register is wired to the cells, on a roadmap text this test writes.

    Driven by a synthetic document rather than the live roadmap: the live one
    is being edited constantly, so a test that asserted today's findings would
    be a tripwire for other people's commits rather than a check on this code.
    """
    import check_roadmap_markers as markers

    backends = set(markers.TIER_ALIASES)
    text = ("## Open, in rough priority order\n\n"
            "999. **A CAPABILITY AUDIT (2026-09-19).** Findings below.\n\n"
            "**F1 HIGH, \u2705 FIXED 2026-09-19** (`fix/999-secret-scrub`). "
            "The scrub is placed at the one choke point in "
            "`backends/rust/emit.py`, so a declared secret marking redacts the "
            "taint before the capability boundary.\n")

    records = markers.tier_parity_records(text, backends)
    assert records, "the synthetic finding did not reach the marker check"
    assert records[0]["tier"] == "rust"
    assert "secret" in records[0]["subjects"]

    # and the map turns that record into cells on every tier it does not name
    moved = {code for subject in records[0]["subjects"]
             for code in tg.SUBJECT_CODES[subject]}
    assert moved, "a secret/redaction finding must move at least one guarantee"
    assert moved <= set(tg.guarantee_codes())
    assert set(records[0]["others"]) == backends - {"rust"}

    # the escape hatch the gate documents: name the other tier and it stops
    named = text.replace(
        "before the capability boundary.",
        "before the capability boundary. The ts tier has no marking pass at "
        "all and is out of scope here; it is filed separately.")
    assert markers.tier_parity_records(named, backends) == []


def test_every_parity_subject_maps_to_real_guarantee_codes():
    """A mapping to a code that does not exist would move no cell and look
    fine. Every non-empty mapping has to name a live register key."""
    codes = set(tg.guarantee_codes())
    for subject, mapped in tg.SUBJECT_CODES.items():
        for code in mapped:
            assert code in codes, (subject, code)


# ----------------------------------------------------- the committed block

def _fresh_block() -> str:
    return tg.block()


def test_the_committed_block_matches_a_fresh_generation():
    """A hand edit to the generated block fails here and in
    `python3 tools/conformance.py --check-readme`."""
    doc = (ROOT / "docs" / "conformance.md").read_text(encoding="utf-8")
    start, end = tg.START, tg.END
    assert start in doc and end in doc, (
        "the GUARANTEE-TIER-MATRIX markers are missing from docs/conformance.md")
    committed = doc[doc.index(start):doc.index(end) + len(end)]
    assert committed == _fresh_block(), (
        "docs/conformance.md's guarantee x tier matrix is stale — run "
        "`python3 tools/conformance.py --write-readme` and commit")


def test_generation_is_deterministic():
    """Two generations byte-identical, or the staleness gate flaps."""
    assert _fresh_block() == _fresh_block()


# -------------------------------------------------------- per-tier support

def test_support_table_rows_and_cells_follow_the_check_registries(monkeypatch):
    """Every positive cell is derived from an existing measurement source.

    Mutating each source changes its cell; adding no source never leaves a
    literal `ok` behind. This is the support table's anti-opinion gate.
    """
    import copy

    report = conformance.run()
    selfhost = conformance.selfhost_column()
    tests_root = ROOT / "tests"
    ci_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    data = tg.support_matrix(report=report, selfhost=selfhost,
                             tests_root=tests_root, ci_text=ci_text)

    assert data["tiers"] == [tg._short(tier) for tier in conformance.TIERS]
    assert {row["tier"] for row in data["rows"]} == set(data["tiers"])
    for row in data["rows"]:
        statuses = {row[key]["status"] for key in
                    ("emits", "executes", "durable_recovery", "selfhost_agreement")}
        assert statuses <= {tg.PROVED, tg.DIVERGENCE, tg.UNMEASURED}
        for key in ("emits", "executes", "durable_recovery", "selfhost_agreement"):
            if row[key]["status"] != tg.UNMEASURED:
                assert row[key]["evidence"], (row["tier"], key)

    tiers = {row["tier"]: row for row in data["rows"]}
    assert tiers["py"]["durable_recovery"]["status"] == tg.PROVED
    assert ("test_a_crash_mid_activation_rolls_back" in
            tiers["py"]["durable_recovery"]["detail"])
    assert tiers["ts"]["durable_recovery"]["status"] == tg.UNMEASURED
    assert "no crash-recovery test" in tiers["ts"]["durable_recovery"]["detail"]
    revl_summary = next(row for row in conformance._summary_rows(report, selfhost)
                        if row[0] == f"{tg.SELFHOST_TIER} (self-host)")
    _, exact, frontier, gaps = revl_summary
    expected_shared = (f"shared revl measure: {exact}/{exact + frontier + gaps} "
                       f"byte-identical; {frontier} frontier limits; {gaps} gaps")
    assert tiers["py"]["selfhost_agreement"]["detail"] == expected_shared
    assert len({row["selfhost_agreement"]["detail"] for row in data["rows"]}) == 1

    # A measured emitter gap changes the cell, rather than leaving an authored
    # success status behind.
    changed_report = copy.deepcopy(report)
    changed_report["cases"][0]["emit_kind"]["python"] = "gap"
    changed = tg.support_matrix(report=changed_report, selfhost=selfhost,
                                tests_root=tests_root, ci_text=ci_text)
    py_emits = next(row for row in changed["rows"] if row["tier"] == "py")["emits"]
    assert py_emits["status"] == tg.DIVERGENCE
    assert "1 gap" in py_emits["detail"]

    # The executed column follows the existing executor registry.
    import validate
    executors = dict(validate.EXECUTORS)
    executors.pop("typescript")
    monkeypatch.setattr(validate, "EXECUTORS", executors)
    changed = tg.support_matrix(report=report, selfhost=selfhost,
                                tests_root=tests_root, ci_text=ci_text)
    ts = next(row for row in changed["rows"] if row["tier"] == "ts")
    assert ts["executes"]["status"] == tg.UNMEASURED

    # A CI command is evidence too. Removing the validator step removes the
    # compile claim even though the validator class still exists.
    ci_without_validator = ci_text.replace(
        "pytest tests/test_conformance_validate.py -q", "")
    changed = tg.support_matrix(report=report, selfhost=selfhost,
                                tests_root=tests_root,
                                ci_text=ci_without_validator)
    assert all(row["emits"]["status"] == tg.UNMEASURED for row in changed["rows"])

    # A self-host result is the existing `revl` column measurement, not a
    # repeated literal in the new table.
    changed_selfhost = dict(selfhost)
    first_case = report["cases"][0]["case"]
    changed_selfhost[first_case] = "limit"
    changed = tg.support_matrix(report=report, selfhost=changed_selfhost,
                                tests_root=tests_root, ci_text=ci_text)
    changed_summary = next(
        row for row in conformance._summary_rows(report, changed_selfhost)
        if row[0] == f"{tg.SELFHOST_TIER} (self-host)")
    _, exact, frontier, gaps = changed_summary
    changed_expected = (f"shared revl measure: {exact}/{exact + frontier + gaps} "
                        f"byte-identical; {frontier} frontier limits; {gaps} gaps")
    changed_py = next(row for row in changed["rows"] if row["tier"] == "py")
    assert changed_py["selfhost_agreement"]["detail"] == changed_expected


def test_support_table_marks_a_recovery_test_unmeasured_until_ci_collects_it(
        tmp_path):
    """The durable column follows a crash test plus a CI collection path."""
    tests_root = tmp_path / "tests"
    tests_root.mkdir()
    (tests_root / "test_conformance_validate.py").write_text(
        "KNOWN_FAILURES: dict[str, set[str]] = {}\n", encoding="utf-8")
    (tests_root / "test_crash_recovery.py").write_text(
        "def test_a_crash_mid_activation_rolls_back(): pass\n", encoding="utf-8")

    report = conformance.run()
    selfhost = conformance.selfhost_column()
    ci = """jobs:
  support:
    steps:
      - run: |
          pytest tests/test_conformance_validate.py -q
          python3 tools/conformance.py --execute --require-execution
          pytest tests/ -q
"""
    baseline = tg.support_matrix(report=report, selfhost=selfhost,
                                 tests_root=tests_root, ci_text=ci)
    by_tier = {row["tier"]: row for row in baseline["rows"]}
    assert by_tier["py"]["durable_recovery"]["status"] == tg.PROVED
    assert by_tier["ts"]["durable_recovery"]["status"] == tg.UNMEASURED

    no_execute = ci.replace(
        "          python3 tools/conformance.py --execute --require-execution\n", "")
    no_exec = tg.support_matrix(report=report, selfhost=selfhost,
                                tests_root=tests_root, ci_text=no_execute)
    assert all(row["executes"]["status"] == tg.UNMEASURED for row in no_exec["rows"])

    # A test file alone is not enough if CI never collects it.
    (tests_root / "test_ts_crash_recovery.py").write_text(
        "def test_a_ts_process_crash_rolls_back(): pass\n", encoding="utf-8")
    ci_without_test_collection = ci.replace("          pytest tests/ -q\n", "")
    still_unmeasured = tg.support_matrix(report=report, selfhost=selfhost,
                                         tests_root=tests_root,
                                         ci_text=ci_without_test_collection)
    assert next(row for row in still_unmeasured["rows"] if row["tier"] == "ts")\
        ["durable_recovery"]["status"] == tg.UNMEASURED

    ci_with_ts = ci_without_test_collection.replace(
        "          python3 tools/conformance.py --execute --require-execution\n",
        "          python3 tools/conformance.py --execute --require-execution\n"
        "          pytest tests/test_ts_crash_recovery.py -q\n")
    measured = tg.support_matrix(
        report=report, selfhost=selfhost, tests_root=tests_root,
        ci_text=ci_with_ts)
    assert next(row for row in measured["rows"] if row["tier"] == "ts")\
        ["durable_recovery"]["status"] == tg.PROVED


def test_the_committed_support_block_matches_a_fresh_generation():
    doc = (ROOT / "docs" / "conformance.md").read_text(encoding="utf-8")
    assert tg.SUPPORT_START in doc and tg.SUPPORT_END in doc
    committed = doc[doc.index(tg.SUPPORT_START):
                    doc.index(tg.SUPPORT_END) + len(tg.SUPPORT_END)]
    assert committed == tg.support_block(), (
        "docs/conformance.md's per-tier support matrix is stale; run "
        "`python3 tools/conformance.py --write-readme`")
