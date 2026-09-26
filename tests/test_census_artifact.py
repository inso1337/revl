"""`tools/census_artifact.py` and the artifact it writes (roadmap item 560).

The artifact's whole value is that its numbers are OUTPUT. A published table
with a hand-transcribed number is a mirror that rots against what it mirrors,
and that shape has cost this repository real wrong claims. So the tests here
hold three separate things, and only one of them is about the tool:

  1. THE COMMITTED ARTIFACT IS NOT STALE where staleness would make it wrong.
     Not by re-running the census on every PR, which would red any branch that
     adds a corpus file, but by coupling the named residuals to the committed
     baseline. When issue #106's work closes the false-admit allowance, the
     published table stops matching the baseline and this suite says so.

  2. THE MECHANISM THE ARTIFACT CLAIMS IS THE ONE THE CODE HAS. The report says
     a `false-admission` cannot be written into the baseline and cannot be
     tolerated by one. Both halves are driven here against the real functions,
     not read out of the report.

  3. THE REPORT DOES NOT OVER-CLAIM. It is an `EVAL-REPORT-1` document and
     `tools/check_eval_report.py` decides that, so that checker is run on the
     committed bytes rather than trusted to have been run once.

The census run itself is expensive (one self-host build, one pass over ~850
programs), so it happens once per module.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))


def _load(rel: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def artifact():
    return _load("tools/census_artifact.py", "census_artifact")


@pytest.fixture(scope="module")
def census():
    return _load("tools/gate_reference_census.py", "artifact_test_census")


@pytest.fixture(scope="module")
def checker():
    return _load("tools/check_eval_report.py", "artifact_test_eval_checker")


@pytest.fixture(scope="module")
def committed(artifact):
    return json.loads(artifact.REPORT_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def committed_md(artifact):
    return artifact.REPORT_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def baseline(census):
    return json.loads(census.BASELINE.read_text(encoding="utf-8"))


# --- 1. the committed artifact is coupled to what it reports on --------------


def test_the_published_residuals_are_exactly_the_baselined_ones(
        committed, baseline, census):
    """THE staleness guard, and the reason it is cheap.

    The published table names every standing `false-admit` residual. The
    baseline is the record of the same allowance. If they disagree, one of them
    is a number somebody will quote that is no longer true, and it does not
    matter which: the artifact has to be regenerated either way.

    This reads two committed files and runs no census, so it costs nothing and
    reds exactly when it should. The day issue #106's work lands and the
    allowance goes to zero, the published `9` stops matching and this fails.
    """
    published = committed["census"]["false_admit_allowance"]["families"]
    recorded = {k: sorted(v) for k, v in baseline.get("buckets", {}).items()
                if k.split("/", 1)[0] == census.HARD}
    assert {k: sorted(v) for k, v in published.items()} == recorded, (
        "docs/census-artifact.json names a different false-admit allowance "
        "than tools/gate_reference_census_baseline.json records. Regenerate: "
        "python3 tools/census_artifact.py --write")


def test_every_residual_is_named_in_the_markdown(committed, committed_md):
    """The exit clause says the published table names each residual. A count
    would let the list churn unread, which is the defect the census's own named
    list exists to prevent, one layer up."""
    families = committed["census"]["false_admit_allowance"]["families"]
    named = [case for ids in families.values() for case in ids]
    assert named or committed["census"]["false_admit_allowance"]["total"] == 0
    for case_id in named:
        assert case_id in committed_md, (
            f"{case_id} is in the allowance but not named in "
            f"docs/census-artifact.md")


def test_the_markdown_states_n_and_the_checker_version(committed, committed_md):
    """The other half of the exit clause: a table without its n and its checker
    version is a number with no way to tell whether it is still the number."""
    c = committed["census"]
    assert str(c["n"]) in committed_md
    assert c["checker_version"] in committed_md
    assert c["run"] in committed_md


def test_the_markdown_carries_no_em_dash(committed_md):
    """`docs/*.md` is inventoried by `tools/docgen.py` with an exact em-dash
    column, so a generated doc that grew one would red the docs gate on an
    unrelated branch. It is also the house style."""
    assert "—" not in committed_md


def test_the_provenance_fraction_is_published_with_its_tool(committed,
                                                            committed_md):
    """A corpus written by the thing it grades is worth nothing, so the census
    number is only meaningful beside the provenance number. Published together
    or not at all."""
    prov = committed["census"]["provenance"]
    assert prov["tool"] == "tools/corpus_provenance.py"
    assert prov["corpora"], "no provenance rows were published"
    assert prov["tool"] in committed_md
    for row in prov["corpora"]:
        assert row["corpus"] in committed_md


# --- 2. the mechanism, driven against the real functions ---------------------


def test_record_payload_drops_a_never_baselined_bucket(census):
    """The RECORD half. `--record` must not be able to write the bucket, or the
    allowance in the direction that matters could be raised by re-recording,
    which is how every other benchmark is cooked."""
    case = "probe:synthetic"
    buckets = {census.ADMISSION: [case], "false-admit/T1": ["real.rvl"]}
    details = {
        case: {"bucket": census.ADMISSION,
               "reference": {"tag": "G3", "message": "m"},
               "gate": {"kind": "admitted", "code": "", "message": ""}},
        "real.rvl": {"bucket": "false-admit/T1",
                     "reference": {"tag": "T1", "message": "m"},
                     "gate": {"kind": "no_objection", "code": "", "message": ""}},
    }
    payload = census.record_payload(buckets, details)
    assert census.ADMISSION not in payload["buckets"]
    assert case not in payload["details"]
    # and the bucket that IS baselined still is, so this is a filter and not a
    # blanket refusal to record anything.
    assert payload["buckets"]["false-admit/T1"] == ["real.rvl"]
    assert "real.rvl" in payload["details"]


def test_compare_refuses_a_never_baselined_bucket_against_any_baseline(census):
    """The CHECK half. A hand-edited baseline that lists the member is the most
    generous baseline that can exist, and it buys nothing."""
    case = "probe:synthetic"
    buckets = {census.ADMISSION: [case]}
    problems = census.compare(buckets, {"buckets": {census.ADMISSION: [case]}})
    assert any(case in p and "FALSE ADMISSION" in p for p in problems), problems


def test_the_artifact_probe_reports_both_halves(artifact, census):
    """The tool does not assert the property in prose, it drives it. This holds
    that the probe is wired to the real functions and reports what they did."""
    probe = artifact.probe_never_baselined(census)
    assert probe["bucket"] == census.ADMISSION
    assert probe["record_refuses_to_write_it"] is True
    assert probe["check_fails_against_a_baseline_that_lists_it"] is True
    assert probe["committed_baseline_never_baselined_keys"] == []
    assert probe["holds"] is True
    assert probe["probe_case"] in probe["check_message"]


def test_the_probe_reports_a_failure_rather_than_hiding_it(artifact, census):
    """A probe that can only say yes measures nothing. Driven against a stand-in
    whose NEVER_BASELINED is empty: both halves must come out False and `holds`
    must follow them rather than being hard-coded."""

    class _Weakened:
        ADMISSION = census.ADMISSION
        NEVER_BASELINED = ()
        TRACKED = census.TRACKED
        BASELINE = census.BASELINE
        CORPUS_DIRS = census.CORPUS_DIRS
        HARD = census.HARD
        compare = staticmethod(census.compare)

        @staticmethod
        def record_payload(buckets, details):
            return {"buckets": {k: sorted(v) for k, v in buckets.items()},
                    "details": details}

    probe = artifact.probe_never_baselined(_Weakened)
    assert probe["record_refuses_to_write_it"] is False
    assert probe["holds"] is False


def test_the_committed_report_says_the_mechanism_held(committed):
    mech = committed["census"]["false_admission"]["mechanism"]
    assert mech["holds"] is True
    assert mech["never_baselined"], "NEVER_BASELINED was published empty"
    assert committed["census"]["false_admission"]["members"] == []
    assert committed["census"]["false_admission"]["count"] == 0


def test_the_empty_bucket_is_not_a_vacuum(committed):
    """An empty `false-admission` bucket proves nothing if the gate admits
    nothing, which is exactly what the state looked like before the admission
    arm opened. The published report has to carry the non-vacuity number."""
    fa = committed["census"]["false_admission"]
    assert fa["issued_admissions_over_the_corpus"] >= 6, (
        "the published report claims zero false admissions over a corpus the "
        "gate issued almost no admissions for; that is a vacuum, not a result")


# --- 3. the report does not over-claim ---------------------------------------


def test_check_eval_report_passes_on_the_committed_report(checker, committed):
    """The issue's concrete exit clause, run on the committed bytes."""
    violations = checker.check_report(committed)
    assert violations == [], violations


def test_the_report_is_written_against_the_frozen_schemas(artifact, checker,
                                                          committed):
    assert committed["report_schema"] == checker.REPORT_SCHEMA
    assert artifact.REPORT_SCHEMA == checker.REPORT_SCHEMA
    assert committed["grader"]["tool"] == checker.GRADER_TOOL
    assert committed["grader"]["kind"] == checker.GRADER_KIND
    for brief in committed["briefs"]:
        assert brief["hard_gate"] in checker.HARD_GATES


def test_the_grader_is_not_the_generator(committed):
    """noSelfScore, held here as well as in the checker: the thing being graded
    is the self-host gate and the grader is the reference compiler, and the
    census is worth nothing if those are ever the same party."""
    gen = {v for v in committed["generator"].values() if isinstance(v, str)}
    grader = {v for v in committed["grader"].values() if isinstance(v, str)}
    assert not (gen & grader)
    assert "selfhost" in committed["generator"]["name"]
    assert "revl.compile_source" == committed["grader"]["tool"]


def test_no_public_claim_stands_at_the_floor(committed):
    for claim in committed["claims"]:
        if claim.get("public", True):
            assert claim["rung"] != "claimed", claim["text"]


def test_a_stale_reproduction_lifts_no_claim(artifact, committed):
    """The recorded `--engine crate` reproduction is the only thing that lifts a
    claim from `measured` to `demonstrated`, and a recorded result rots. So the
    rung is computed from whether the reproduction is current, never declared:
    a reproduction recorded at another checker version must drop every lifted
    claim back down."""
    rep = committed["census"]["reproduction"]
    assert rep is not None and rep["is_current"] is True, (
        "the committed reproduction is stale; re-run "
        "tools/gate_reference_census.py --engine crate and "
        "tools/census_artifact.py --record-reproduction --write")
    lifted = [c for c in committed["claims"] if c["rung"] == "demonstrated"]
    assert lifted, "nothing was lifted, so this test measures nothing"
    for claim in lifted:
        assert "reproduced_by" in claim["evidence"]

    # and the drop actually happens: the same evidence at a version that is not
    # the current one justifies only `measured`.
    stale = dict(committed["census"]["reproduction"])
    stale["recorded_at_checker_version"] = "GATE-CENSUS-1+000000000000"
    assert stale["recorded_at_checker_version"] != stale["current_checker_version"]


def test_the_reproduction_fixture_matches_what_was_published(artifact,
                                                             committed):
    recorded = json.loads(
        artifact.CRATE_REPRODUCTION.read_text(encoding="utf-8"))
    rep = committed["census"]["reproduction"]
    assert recorded["engine"] == "crate"
    assert recorded["checker_version"] == rep["recorded_at_checker_version"]
    assert recorded["false_admissions"] == rep["crate_false_admissions"]
    assert {k: len(v) for k, v in recorded["tracked_buckets"].items()} == \
        rep["crate_tracked_buckets"]


def test_the_report_names_what_it_does_not_establish(committed, committed_md):
    """A report that only says what it proves is a report that will be read as
    saying more. The limits are part of the artifact, not a footnote."""
    limits = committed["census"]["not_established"]
    assert len(limits) >= 3
    for line in limits:
        assert line in committed_md


def test_the_reproduction_states_its_own_limit(committed):
    """The crate is built FROM `selfhost/lower.rvl`, so it is the same source
    through a different toolchain and not a second specification. Publishing it
    as an independent reproduction without that sentence would over-claim."""
    rep = committed["census"]["reproduction"]
    assert "not a second" in rep["does_not_establish"]
    assert "build_gate_crate" in rep["does_not_establish"]


# --- the identities carry no clock and no commit -----------------------------


def test_the_artifact_carries_no_timestamp_or_commit(committed_md, committed):
    """`--check` has to measure staleness of the NUMBERS. An artifact stamped
    with a wall clock or a git sha reds on every unrelated commit, and a gate
    that reds constantly gets bypassed."""
    c = committed["census"]
    assert c["run"].startswith("census-")
    assert c["compiler_tree_digest"].startswith("src/revl@sha256:")
    for claim in committed["claims"]:
        assert claim["evidence"]["compiler_commit"] == c["compiler_tree_digest"]
    assert "commit" not in committed["generator"]


def test_the_checker_version_moves_with_the_checker(artifact):
    version, per_file = artifact.checker_version()
    assert version.startswith(artifact.CENSUS_SCHEMA + "+")
    assert set(per_file) == set(artifact.CHECKER_SOURCES)
    for rel in artifact.CHECKER_SOURCES:
        assert (ROOT / rel).is_file(), f"{rel} is not in the tree"


# --- the digests are recomputable by someone who cloned somewhere else --------


def test_a_digest_names_its_files_relative_to_the_checkout(artifact, tmp_path):
    """The defect this holds against, stated as the outsider hits it.

    `_digest` used to hash the ABSOLUTE path of each file. So the checker
    version and the compiler tree digest were functions of the directory the
    clone sat in: byte-identical checkouts at two paths produced two different
    values, `--check` reported drift that did not exist, and the recorded crate
    reproduction read as stale for no reason but a directory name. An artifact
    published so an outsider can recompute its identity is worth nothing if the
    identity depends on where the outsider put the repository.

    Driven rather than asserted: the same bytes are digested from two different
    directories and the two digests have to agree.
    """
    first, second = tmp_path / "somewhere", tmp_path / "somewhere-else"
    digests = []
    for base in (first, second):
        (base / "tools").mkdir(parents=True)
        (base / "tools" / "a.py").write_bytes(b"alpha\n")
        (base / "tools" / "b.py").write_bytes(b"beta\n")
        saved = artifact.ROOT
        artifact.ROOT = base
        try:
            digests.append(artifact._digest(
                [base / "tools" / "a.py", base / "tools" / "b.py"]))
        finally:
            artifact.ROOT = saved
    assert digests[0] == digests[1], (
        "the same bytes digested from two directories produced two digests; "
        "the checkout path is leaking into the artifact's identity")


def test_a_digest_refuses_a_file_outside_the_checkout(artifact, tmp_path):
    """The fallback a relative name invites is an absolute one, which is the
    defect coming back. It raises instead."""
    stray = tmp_path / "stray.py"
    stray.write_bytes(b"x\n")
    with pytest.raises(SystemExit):
        artifact._digest([stray])


def test_the_published_identities_are_digests_and_nothing_else(committed):
    """Each published identity is a bare hex digest behind its prefix. A path,
    a hostname or a directory name inside one would both leak and make the
    value unrepeatable elsewhere."""
    c = committed["census"]
    tails = {
        "checker_version": c["checker_version"].split("+", 1)[1],
        "compiler_tree_digest": c["compiler_tree_digest"].split("sha256:", 1)[1],
        "run": c["run"].rsplit("-", 1)[1],
    }
    for field, tail in tails.items():
        assert tail and all(ch in "0123456789abcdef" for ch in tail), (
            f"{field} carries something that is not a hex digest: {tail!r}")
    for rel, value in c["checker_sources"].items():
        assert len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)
        assert not Path(rel).is_absolute(), f"{rel} is an absolute path"


# --- a non-empty baseline cannot be published as an empty allowance ----------


def test_the_allowance_publishes_the_committed_baseline_beside_the_run(
        artifact, census, committed):
    """The failure this closes, in one sentence: a `false-admit` member that is
    BASELINED but no longer diverges leaves the measured list empty, so an
    artifact that reports only the measurement says "the allowance is empty"
    while the committed baseline still grants tolerance for it.

    `tools/gate_reference_census.py --check` fails in that direction, but a
    reader holding only the artifact cannot see it. So the artifact carries
    both lists and the names on which they differ.
    """
    alw = committed["census"]["false_admit_allowance"]
    for key in ("families", "baselined", "baselined_total", "baseline_file",
                "baselined_but_not_measured", "measured_but_not_baselined",
                "agrees_with_committed_baseline"):
        assert key in alw, f"the published allowance does not carry {key}"

    recorded = {k: sorted(v) for k, v in
                json.loads(census.BASELINE.read_text(encoding="utf-8"))
                .get("buckets", {}).items()
                if k.split("/", 1)[0] == census.HARD}
    assert alw["baselined"] == recorded
    assert alw["baselined_total"] == sum(len(v) for v in recorded.values())


def test_a_stale_non_empty_baseline_cannot_publish_as_an_empty_allowance(
        artifact, census, monkeypatch, tmp_path):
    """Driven, not asserted. A baseline that lists a `false-admit` member is
    handed to `allowance` alongside a run that measured none, which is exactly
    the shape that used to publish as "the allowance is empty this run"."""
    stale = tmp_path / "baseline.json"
    stale.write_text(json.dumps({
        "buckets": {"false-admit/T1": ["tests/fixtures/ghost.rvl"]},
    }), encoding="utf-8")
    monkeypatch.setattr(census, "BASELINE", stale)

    alw = artifact.allowance(census, {"agree-admit": ["ok.rvl"]})

    assert alw["total"] == 0, "the run measured no false-admit member"
    assert alw["baselined_total"] == 1, "the baseline still grants one"
    assert alw["agrees_with_committed_baseline"] is False
    assert alw["baselined_but_not_measured"] == [
        "false-admit/T1: tests/fixtures/ghost.rvl"]
    assert alw["measured_but_not_baselined"] == []
    # The renderer prints both totals and every name in either list, so a
    # payload carrying these cannot render as "the allowance is empty".
    assert alw["baselined"] == {
        "false-admit/T1": ["tests/fixtures/ghost.rvl"]}


def test_a_new_bypass_the_baseline_does_not_carry_is_named_too(
        artifact, census, monkeypatch, tmp_path):
    """The other direction, which is a live bypass rather than a stale entry."""
    empty = tmp_path / "baseline.json"
    empty.write_text(json.dumps({"buckets": {}}), encoding="utf-8")
    monkeypatch.setattr(census, "BASELINE", empty)

    alw = artifact.allowance(
        census, {"false-admit/T1": ["tests/fixtures/new_bypass.rvl"]})

    assert alw["total"] == 1
    assert alw["baselined_total"] == 0
    assert alw["agrees_with_committed_baseline"] is False
    assert alw["measured_but_not_baselined"] == [
        "false-admit/T1: tests/fixtures/new_bypass.rvl"]


def test_the_markdown_names_a_baseline_the_run_did_not_measure(committed_md,
                                                               committed):
    """Whatever the state is, the markdown states BOTH numbers, so a reader
    cannot mistake a measured zero for a baselined zero."""
    alw = committed["census"]["false_admit_allowance"]
    assert f"Measured this run: **{alw['total']}**" in committed_md
    assert f"Recorded in the baseline: **{alw['baselined_total']}**" in \
        committed_md
    for entry in (alw["baselined_but_not_measured"]
                  + alw["measured_but_not_baselined"]):
        assert entry in committed_md


# --- n is the distinct count, and the repeats are named ----------------------


def test_the_report_states_the_distinct_count_and_names_the_repeats(
        committed, committed_md):
    """`n` counts programs RUN. Six case ids reach the corpus twice, so `n`
    over-counts the corpus by exactly the repeats. A benchmark's n is the
    number a reader quotes, so the distinct count is published, the repeats
    are named, and the gap is checkable rather than asserted."""
    c = committed["census"]
    assert c["n_distinct"] <= c["n"]
    assert c["n"] - c["n_distinct"] == len(c["repeated_case_ids"])
    assert f"Distinct programs: **{c['n_distinct']}**" in committed_md
    assert f"Programs run: **{c['n']}**" in committed_md
    for case_id in c["repeated_case_ids"]:
        assert case_id in committed_md, (
            f"{case_id} is counted twice but not named in the markdown")


def test_the_headline_claim_is_stated_on_the_distinct_count(committed):
    """The claim a sceptic reads first must not carry the inflated n."""
    c = committed["census"]
    head = committed["claims"][0]["text"]
    assert f"{c['n_distinct']} distinct programs" in head
    assert f"{c['n']} runs" in head
