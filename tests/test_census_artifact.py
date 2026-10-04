"""`tools/census_artifact.py` and the artifact it writes (roadmap item 560).

The artifact's whole value is that its numbers are OUTPUT. A published table
with a hand-transcribed number is a mirror that rots against what it mirrors,
and that shape has cost this repository real wrong claims. So the tests here
hold three separate things, and only one of them is about the tool:

  1. THE COMMITTED ARTIFACT IS NOT STALE where staleness would make it wrong,
     by coupling the named residuals to the committed baseline. When issue
     #106's work closes the false-admit allowance, the published table stops
     matching the baseline and this suite says so. The census itself is re-run
     by CI's `census-artifact` job (`--verify --strict`), only on a pull
     request that moves an input, which then regenerates the artifact in the
     same diff (issue #1572); section 5 holds that gate's logic.

  2. THE MECHANISM THE ARTIFACT CLAIMS IS THE ONE THE CODE HAS. The report says
     a `false-admission` cannot be written into the baseline and cannot be
     tolerated by one. Both halves are driven here against the real functions,
     not read out of the report.

  3. THE REPORT DOES NOT OVER-CLAIM. It is an `EVAL-REPORT-1` document and
     `tools/check_eval_report.py` decides that, so that checker is run on the
     report the committed records render rather than trusted to have been run
     once.

Since issue #1768 the repository commits the RECORDS of a run
(`docs/census-artifact/`), one record per line and nothing derived, and the
report is rendered from them. So `committed` below is the report the committed
records render, with no census run, and section 6 holds the layout: sorted
records, no stored aggregate, a report that is the same whether it is built
from a run or from the records, and two independent changes that merge under
git's ordinary line merge.

The census run itself is expensive (one self-host build, one pass over ~850
programs), so it happens once per module.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from _load_by_path import load_by_path  # noqa: E402


def _load(rel: str, name: str):
    module = load_by_path(name, ROOT / rel)
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
    """The report the committed records render, with no census run."""
    return artifact.report_from_records()


@pytest.fixture(scope="module")
def committed_md(artifact, committed):
    return artifact.render_markdown(committed)


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
        "docs/census-artifact/ renders a different false-admit allowance "
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
            f"{case_id} is in the allowance but not named in the rendered "
            f"markdown")


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
    recorded = artifact.load_reproduction()
    rep = committed["census"]["reproduction"]
    assert recorded["n"] == rep["n"] == len(recorded["programs"])
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


# --- 4. the pins and the verifier (issue #1268) --------------------------------
#
# `judge` is pure, so every verdict arm is driven here with synthetic rows and
# pins, and no census runs. The one real end-to-end pass (`--verify` against the
# committed file) is a manual step recorded in docs/design/560, because it costs
# a full census run.

_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _published(rows, **pins):
    table = {}
    for _, _, name in rows:
        table[name] = table.get(name, 0) + 1
    return {
        "buckets": [{"bucket": k, "count": v} for k, v in table.items()],
        "cases": [list(r) for r in rows],
        "false_admission": {"members": [], "mechanism": {"holds": True}},
        "pins": {
            "decides_verdicts": pins.get("deciding",
                                         {"tools/gate_reference_census.py": _SHA_A}),
            "reference": pins.get("reference", {"src/revl/compiler.py": _SHA_A}),
            "report_inputs": pins.get("report", {"tests/fixtures/x.json": _SHA_A}),
        },
    }


def _local(rows, *, holds=True, **pins):
    return {
        "cases": [list(r) for r in rows],
        "mechanism_holds": holds,
        "pins": {
            "decides_verdicts": pins.get("deciding",
                                         {"tools/gate_reference_census.py": _SHA_A}),
            "reference": pins.get("reference", {"src/revl/compiler.py": _SHA_A}),
            "report_inputs": pins.get("report", {"tests/fixtures/x.json": _SHA_A}),
        },
    }


_ROWS = [("a.rvl", _SHA_A, "agree-admit"),
         ("b.rvl", _SHA_B, "agree-refuse/G4"),
         ("oracle-reject:twice", _SHA_A, "agree-refuse/G1"),
         ("oracle-reject:twice", _SHA_A, "agree-refuse/G1")]


def test_verify_reproduces_on_identical_inputs(artifact):
    result = artifact.judge(_published(_ROWS), _local(_ROWS))
    assert result["verdict"] == "reproduced" and result["exit"] == 0
    assert result["checked"] == result["agree"] == len(_ROWS)


def test_verify_refutes_a_verdict_that_differs_on_identical_inputs(artifact):
    local = list(_ROWS)
    local[1] = ("b.rvl", _SHA_B, "agree-admit")
    result = artifact.judge(_published(_ROWS), _local(local))
    assert result["verdict"] == "refuted" and result["exit"] == 1
    assert result["differ"] == [{"case": "b.rvl", "published": "agree-refuse/G4",
                                 "local": "agree-admit"}]
    assert "verdict differs: b.rvl" in artifact.render_verdict(result)


def test_verify_refutes_a_table_that_is_not_the_sum_of_its_rows(artifact):
    """Editing a count without editing the rows is caught with no run at all."""
    pub = _published(_ROWS)
    for row in pub["buckets"]:
        if row["bucket"] == "agree-admit":
            row["count"] += 1
    result = artifact.judge(pub, _local(_ROWS))
    assert result["verdict"] == "refuted"
    assert any("agree-admit" in c for c in result["self_contradictions"])


def test_a_moved_deciding_file_is_different_inputs_not_a_refutation(artifact):
    """A reader on another tree gets a new measurement, never a false alarm
    about the published one, even when verdicts also differ."""
    local = list(_ROWS)
    local[0] = ("a.rvl", _SHA_A, "agree-refuse/G1")
    result = artifact.judge(
        _published(_ROWS),
        _local(local, deciding={"tools/gate_reference_census.py": _SHA_B}))
    assert result["verdict"] == "different-inputs" and result["exit"] == 3
    assert result["decides_verdicts"]["moved"] == [
        "tools/gate_reference_census.py"]


def test_a_file_the_run_read_but_the_publication_never_pinned_is_named(artifact):
    """The hole a declared list leaves: the emitter decides verdicts and was
    in no published identity. Measured pins name it on the reader's side."""
    deciding = {"tools/gate_reference_census.py": _SHA_A,
                "backends/python/emit.py": _SHA_A}
    result = artifact.judge(_published(_ROWS), _local(_ROWS, deciding=deciding))
    assert result["verdict"] == "different-inputs"
    assert result["unpinned_inputs"] == ["backends/python/emit.py"]
    assert "UNPINNED INPUT" in artifact.render_verdict(result)


def test_a_moved_reference_file_is_different_inputs(artifact):
    result = artifact.judge(
        _published(_ROWS),
        _local(_ROWS, reference={"src/revl/compiler.py": _SHA_B}))
    assert result["verdict"] == "different-inputs"


def test_a_moved_report_input_does_not_change_the_verdict(artifact):
    """The baseline or the provenance manifest shapes the report, not any
    program's verdict, so it is named and nothing more."""
    result = artifact.judge(
        _published(_ROWS),
        _local(_ROWS, report={"tests/fixtures/x.json": _SHA_B}))
    assert result["verdict"] == "reproduced"
    assert result["report_inputs"]["moved"] == ["tests/fixtures/x.json"]


def test_an_edited_or_removed_program_makes_the_check_partial(artifact):
    local = [("a.rvl", _SHA_B, "agree-admit")] + list(_ROWS[2:])
    result = artifact.judge(_published(_ROWS), _local(local))
    assert result["verdict"] == "partial" and result["exit"] == 3
    assert result["edited"] == ["a.rvl"]
    assert result["gone"] == ["b.rvl"]
    assert result["checked"] == 2


def test_new_programs_do_not_stop_a_reproduction(artifact):
    """The corpus grows weekly. A published copy stays checkable."""
    local = list(_ROWS) + [("new.rvl", _SHA_B, "agree-admit")]
    result = artifact.judge(_published(_ROWS), _local(local))
    assert result["verdict"] == "reproduced"
    assert result["new"] == ["new.rvl"]


def test_a_repeated_case_id_is_compared_occurrence_by_occurrence(artifact):
    local = list(_ROWS)
    local[3] = ("oracle-reject:twice", _SHA_A, "agree-admit")
    result = artifact.judge(_published(_ROWS), _local(local))
    assert result["verdict"] == "refuted"
    assert [d["case"] for d in result["differ"]] == ["oracle-reject:twice"]


def test_a_false_admission_in_the_local_run_refutes(artifact):
    local = list(_ROWS) + [("new.rvl", _SHA_B, "false-admission")]
    result = artifact.judge(_published(_ROWS), _local(local))
    assert result["verdict"] == "refuted"
    assert result["local_false_admissions"] == ["new.rvl"]


def test_a_mechanism_that_does_not_hold_refutes(artifact):
    result = artifact.judge(_published(_ROWS), _local(_ROWS, holds=False))
    assert result["verdict"] == "refuted"


def test_a_published_false_admission_refutes_the_published_file(artifact):
    rows = list(_ROWS) + [("x.rvl", _SHA_A, "false-admission")]
    result = artifact.judge(_published(rows), _local(rows))
    assert result["verdict"] == "refuted"
    assert result["self_contradictions"]


def test_verify_refuses_a_file_with_nothing_to_verify_against(artifact,
                                                              tmp_path):
    """Exit 2 before any census runs: a report without rows and pins cannot be
    checked case by case, and saying so beats a vacuous pass."""
    stale = tmp_path / "old.json"
    stale.write_text(json.dumps({"census": {"schema": artifact.CENSUS_SCHEMA}}))
    code, text = artifact.verify(stale)
    assert code == 2 and "predates" in text
    code, _ = artifact.verify(tmp_path / "absent.json")
    assert code == 2


def test_recording_reads_sees_an_open_and_leaves_no_state(artifact, tmp_path):
    """The audit hook cannot be removed, so it must be inert outside a block.
    Process-global state is compared before and after."""
    before = list(artifact._READS)
    target = tmp_path / "read-me.txt"
    target.write_text("x")
    with artifact.recording_reads() as outer:
        with artifact.recording_reads() as inner:
            target.read_text()
        (tmp_path / "second.txt").write_text("y")
    assert str(target) in {str(Path(p)) for p in inner}
    assert str(target) not in {str(Path(p)) for p in outer}
    assert artifact._READS == before == []
    target.read_text()
    assert artifact._READS == []


def test_a_bytecode_read_is_pinned_as_its_source(artifact):
    pyc = (artifact.ROOT / "tools" / "__pycache__"
           / "gate_reference_census.cpython-312.pyc")
    assert artifact.tree_file(str(pyc)) == "tools/gate_reference_census.py"
    assert artifact.tree_file("/definitely/not/in/the/tree.py") is None


def test_the_published_rows_add_up_to_the_published_table(committed):
    c = committed["census"]
    counts: dict[str, int] = {}
    for _, _, name in c["cases"]:
        counts[name] = counts.get(name, 0) + 1
    assert counts == {row["bucket"]: row["count"] for row in c["buckets"]}
    assert len(c["cases"]) == c["n"]


def test_the_published_pins_cover_what_the_checker_version_does_not(committed):
    """The emitter that turns the gate into python and the classifier that
    turns a reference error into a tag both decide verdicts and neither is in
    `CHECKER_SOURCES`. The measured pins carry them."""
    deciding = committed["census"]["pins"]["decides_verdicts"]
    for rel in ("backends/python/emit.py", "tests/test_selfhost_lower.py",
                "tools/gate_reference_census.py"):
        assert rel in deciding, f"{rel} decides verdicts and is not pinned"
    for rel, sha in deciding.items():
        assert len(sha) == 64 and not rel.startswith("/")


# --- 5. the repository's gate on its own committed copy (issue #1572) --------
#
# `--verify` answers a reader holding an old copy, so new programs and moved
# report inputs do not stop it. The repository's own copy is held to more:
# `--verify --strict` in CI, on every pull request that moves an input. These
# drive each arm of that stricter answer and of the diff filter that decides
# when it runs, with no census run.


def test_strict_accepts_only_a_full_reproduction_with_nothing_left_over(artifact):
    result = artifact.judge(_published(_ROWS), _local(_ROWS))
    assert artifact.current_problems(result) == []


def test_strict_fails_on_a_program_the_committed_copy_does_not_carry(artifact):
    """The reader's verdict stays `reproduced` here; the gate must not."""
    local = list(_ROWS) + [("new.rvl", _SHA_B, "agree-admit")]
    result = artifact.judge(_published(_ROWS), _local(local))
    assert result["verdict"] == "reproduced"
    assert artifact.current_problems(result) == [
        "1 program in the corpus is not in the committed artifact"]


def test_strict_fails_on_a_moved_report_input(artifact):
    result = artifact.judge(
        _published(_ROWS),
        _local(_ROWS, report={"tests/fixtures/x.json": _SHA_B}))
    assert result["verdict"] == "reproduced"
    assert artifact.current_problems(result) == [
        "report input moved: tests/fixtures/x.json"]


def test_strict_fails_on_every_verdict_but_reproduced(artifact):
    moved = artifact.judge(
        _published(_ROWS),
        _local(_ROWS, reference={"src/revl/compiler.py": _SHA_B}))
    assert moved["verdict"] == "different-inputs"
    assert artifact.current_problems(moved) == [
        "the verdict is different-inputs, not reproduced"]
    edited = list(_ROWS)
    edited[0] = ("a.rvl", _SHA_B, "agree-admit")
    partial = artifact.judge(_published(_ROWS), _local(edited))
    assert partial["verdict"] == "partial"
    assert artifact.current_problems(partial) == [
        "the verdict is partial, not reproduced"]


def _committed_for_filter():
    return {"census": {
        "cases": [["examples/a.rvl", _SHA_A, "agree-admit"],
                  ["oracle-reject:twice", _SHA_A, "agree-refuse/G1"]],
        "pins": {"decides_verdicts": {"backends/python/emit.py": _SHA_A},
                 "reference": {"src/revl/lower.py": _SHA_A},
                 "report_inputs": {"tools/gate_reference_census_baseline.json":
                                   _SHA_A}}}}


def test_the_diff_filter_names_every_kind_of_input(artifact):
    committed = _committed_for_filter()
    inputs = ["backends/python/emit.py", "src/revl/lower.py",
              "tools/gate_reference_census_baseline.json", "examples/a.rvl",
              "tests/fixtures/brand_new.rvl", "crates/revl-gate/src/admission.rs",
              "crates/revl-gate/Cargo.toml", "tools/census_artifact.py",
              "docs/census-artifact/cases.jsonl", "selfhost/lower.rvl"]
    assert artifact.moved_inputs(inputs, committed) == inputs


def test_the_diff_filter_ignores_what_the_census_does_not_read(artifact):
    """A reference module the census never opens is not an input: this is
    what keeps an unrelated compiler change from owing a regeneration."""
    committed = _committed_for_filter()
    unrelated = ["src/revl/mcp/http_face.py", "docs/v2.0-roadmap.md",
                 "bench/results/x.rvl", "backends/python/.venv/lib/y.rvl",
                 "crates/revl-gate/README.md", ""]
    assert artifact.moved_inputs(unrelated, committed) == []


def test_an_unreadable_artifact_makes_every_path_an_input(artifact):
    """A broken committed copy cannot switch its own gate off."""
    assert artifact.moved_inputs(["docs/x.md"], None) == ["docs/x.md"]


def test_the_committed_reference_pins_are_what_the_run_opened(committed):
    """Measured, not globbed: a subset of `src/revl`, and not all of it."""
    reference = committed["census"]["pins"]["reference"]
    assert reference and all(rel.startswith("src/revl/") and rel.endswith(".py")
                             for rel in reference)
    everything = list((ROOT / "src" / "revl").rglob("*.py"))
    assert len(reference) < len(everything)
    assert "src/revl/compiler.py" in reference
    # The MCP HTTP face is never imported by a census run. Under the old glob
    # it was pinned anyway, so a change to it made the artifact stale.
    assert "src/revl/mcp/http_face.py" not in reference


# --- 6. the committed layout merges (issue #1768) -----------------------------
#
# The repository commits the records of a run, not the report. Every aggregate
# the old `docs/census-artifact.{json,md}` stored (the run id and the compiler
# digest six times each, `n`, the bucket table, the claims, every number in
# the markdown, and the sha256 of every pinned file and program) changed on
# every pull request that moved the corpus or a module the census opens, so
# after each landing almost every open pull request conflicted in it. The
# records carry nothing derived, so two pull requests conflict there only when
# both moved the same program's verdict.

from _merge_tree import git_has_merge_tree, merge  # noqa: E402

_needs_merge_tree = pytest.mark.skipif(
    not git_has_merge_tree(), reason="git merge-tree --write-tree needs git 2.38")


@pytest.fixture(scope="module")
def records(artifact):
    return artifact.load_records()


@pytest.fixture(scope="module")
def sources(census):
    _reference, oracle = census._reference()
    return census.load_corpus(oracle)


def _texts(artifact, records) -> dict[str, str]:
    """The record files for unhydrated `records`, as `--write` writes them."""
    measured = {"case_rows": [[cid, "", b] for cid, b in records["cases"]],
                "pins": {g: {rel: "" for rel in records["pins"][g]}
                         for g in artifact.VERDICT_PIN_GROUPS},
                "engine": records["facts"]["engine"],
                "issued_admissions": records["facts"]["issued_admissions"],
                "reference_faults": records["facts"]["reference_faults"]}
    texts = artifact.record_texts(measured, records["facts"]["mechanism"])
    return {f"docs/census-artifact/{name}": text
            for name, text in texts.items()}


def _old_layout(artifact, census, records, sources) -> dict[str, str]:
    """The same run as the pre-#1768 layout committed it: the whole rendered
    report, digests included, as JSON beside its markdown."""
    provenance = _load("tools/corpus_provenance.py", "artifact_test_provenance")
    hydrated = artifact.hydrate(records, artifact.corpus_sources(sources))
    report = artifact.build_report(
        census, provenance, artifact.measured_from_records(hydrated),
        artifact._read_crate(None), probe=records["facts"]["mechanism"])
    return {"docs/census-artifact.json": artifact._serialise(report),
            "docs/census-artifact.md": artifact.render_markdown(report)}


def _with(records, *, add=(), bucket=None):
    """A copy of `records` with programs added or a program's verdict moved,
    as a pull request would leave them."""
    out = json.loads(json.dumps(records))
    out["cases"] += [list(row) for row in add]
    out["cases"].sort(key=lambda row: row[0])
    for index, name in (bucket or {}).items():
        out["cases"][index][1] = name
    return out


def test_the_committed_records_are_sorted_one_per_line_and_canonical(
        artifact, records):
    """Sorted, one record per line, a blank line between records, and byte
    for byte what `--write` produces from them. A hand-merged or reordered
    file fails here before CI's `--verify --strict` names it too."""
    for rel, text in _texts(artifact, records).items():
        assert (ROOT / rel).read_text(encoding="utf-8") == text, (
            f"{rel} is not in canonical record form; regenerate it: "
            f"python3 tools/census_artifact.py --write")
    for name in (artifact.CASES_RECORDS, artifact.PINS_RECORDS):
        lines = (artifact.RECORDS / name).read_text().split("\n")
        assert all(line == "" for line in lines[1::2]), (
            f"{name}: records are not separated by a blank line")
        assert all(line.startswith("[") for line in lines[0:-1:2])
    ids = [row[0] for row in records["cases"]]
    assert ids == sorted(ids)
    for group in artifact.VERDICT_PIN_GROUPS:
        assert records["pins"][group] == sorted(records["pins"][group])


def test_the_records_store_no_aggregate_and_no_digest(artifact, records,
                                                      committed):
    """The values that used to churn are rendered, never stored: no count,
    no identity, and no sha256 of a file the checkout already has."""
    import re  # noqa: PLC0415
    c = committed["census"]
    stored = "".join((artifact.RECORDS / name).read_text()
                     for name in artifact.RECORD_FILES)
    for derived in (c["run"], c["compiler_tree_digest"], c["checker_version"],
                    committed["claims"][0]["text"]):
        assert derived not in stored, f"{derived!r} is stored in the records"
    assert not re.search(r"[0-9a-f]{64}", stored), "a sha256 is stored"
    assert set(records["facts"]) == {
        "schema", "note", "engine", "issued_admissions", "reference_faults",
        "mechanism"}


def test_a_report_rendered_from_records_is_the_report_a_run_builds(
        artifact, census):
    """The derivability the layout rests on, driven with a synthetic run:
    whatever `build_report` reads from a measurement survives the trip
    through the record files and the checkout unchanged, so rendering the
    committed records is rendering the run that wrote them."""
    provenance = _load("tools/corpus_provenance.py", "artifact_test_prov2")
    cases = [("examples/b.rvl", "b"), ("oracle-reject:twice", "t"),
             ("examples/a.rvl", "a"), ("oracle-reject:twice", "t"),
             ("admission:one", "o")]
    pinned = {"decides_verdicts": ["tools/gate_reference_census.py"],
              "reference": ["src/revl/compiler.py"],
              "report_inputs": ["tests/fixtures/corpus_provenance.json"]}
    measured = {
        "cases": cases, "details": {}, "engine": "selfhost",
        "buckets": {"agree-admit": ["examples/b.rvl", "admission:one"],
                    "agree-refuse/G1": ["oracle-reject:twice",
                                        "oracle-reject:twice"],
                    "agree-refuse/G4": ["examples/a.rvl"]},
        "issued_admissions": ["admission:one"], "reference_faults": [],
        "n_distinct": 4, "repeated_case_ids": ["oracle-reject:twice"],
        "pins": {"note": artifact.PINS_NOTE,
                 **{g: {rel: artifact._sha(rel) for rel in rels}
                    for g, rels in pinned.items()}},
    }
    measured["case_rows"] = artifact.case_rows(measured)
    probe = artifact.probe_never_baselined(census)
    fresh = artifact.build_report(census, provenance, measured, None,
                                  probe=probe)
    texts = artifact.record_texts(measured, probe)
    parsed = {
        "cases": [json.loads(x) for x in texts["cases.jsonl"].split("\n") if x],
        "pins": {g: [] for g in artifact.VERDICT_PIN_GROUPS},
        "facts": json.loads(texts["facts.json"]),
    }
    for line in texts["pins.jsonl"].split("\n"):
        if line:
            group, rel = json.loads(line)
            parsed["pins"][group].append(rel)
    hydrated = artifact.hydrate(parsed, artifact.corpus_sources(cases))
    rendered = artifact.build_report(
        census, provenance, artifact.measured_from_records(hydrated), None,
        probe=parsed["facts"]["mechanism"])
    assert rendered == fresh


def test_records_that_name_an_absent_input_render_nothing(artifact, records):
    """A records file that pins a file the tree lacks, or carries a program
    the corpus lacks, describes another checkout; rendering it would name
    inputs the run did not have."""
    gone = json.loads(json.dumps(records))
    gone["pins"]["reference"].append("src/revl/not_a_module.py")
    with pytest.raises(artifact.StaleRecords):
        artifact.hydrate(gone, {})
    extra = {"cases": [["examples/nowhere.rvl", "agree-admit"]],
             "pins": {g: [] for g in artifact.VERDICT_PIN_GROUPS},
             "facts": records["facts"]}
    with pytest.raises(artifact.StaleRecords):
        artifact.hydrate(extra, {})


@_needs_merge_tree
def test_two_independent_census_changes_merge_in_the_new_layout_only(
        artifact, census, records, sources):
    """The exit test of issue #1768, on the records as committed today.

    Two pull requests, built the way the conflicting ones in the issue were:
    each adds a program to the corpus and edits a different module the census
    opens. In the records each adds one line, at different places, and they
    merge. In the layout they replace, the same two changes collide on the
    run id, `n`, the bucket counts and the claims, and conflict in both
    files."""
    left = _with(records, add=[("examples/zz_merge_probe_left.rvl",
                                "agree-admit")])
    right = _with(records, add=[("tests/fixtures/aa_merge_probe_right.rvl",
                                 "agree-refuse/G4")])
    clean, conflicted = merge(_texts(artifact, records),
                              _texts(artifact, left), _texts(artifact, right))
    assert clean, f"the records conflict on independent changes: {conflicted}"

    def old(recs, extra):
        src = list(sources) + [(cid, cid) for cid, _ in extra]
        return _old_layout(artifact, census, recs, src)

    left_add = [("examples/zz_merge_probe_left.rvl", "agree-admit")]
    right_add = [("tests/fixtures/aa_merge_probe_right.rvl", "agree-refuse/G4")]
    clean_old, conflicted_old = merge(old(records, []), old(left, left_add),
                                      old(right, right_add))
    assert not clean_old and set(conflicted_old) == {
        "docs/census-artifact.json", "docs/census-artifact.md"}, (
        "the old layout merged, so this test no longer measures the defect")


@_needs_merge_tree
def test_neighbouring_verdicts_merge_and_the_same_verdict_conflicts(
        artifact, records):
    """The blank line between records is what lets two pull requests that
    move the verdicts of neighbouring programs merge; moving the same
    program's verdict two ways is a real conflict and stays one."""
    base = _texts(artifact, records)
    left = _with(records, bucket={10: "agree-refuse/G1"})
    right = _with(records, bucket={11: "agree-refuse/G4"})
    clean, conflicted = merge(base, _texts(artifact, left),
                              _texts(artifact, right))
    assert clean, conflicted
    clean, conflicted = merge(
        base, _texts(artifact, left),
        _texts(artifact, _with(records, bucket={10: "agree-refuse/T1"})))
    assert not clean and conflicted == ["docs/census-artifact/cases.jsonl"]


def test_a_forgotten_regeneration_still_fails_the_strict_check(
        artifact, records, sources, tmp_path):
    """The currency check is exactly as strict on the records as it was on
    the report. A pull request that adds a program, moves a verdict or makes
    the run read a new file, and does not regenerate, fails `--strict`."""
    by_id = artifact.corpus_sources(sources)
    committed = artifact.published_from_records(artifact.hydrate(records, by_id))

    def local(recs, extra=()):
        src = dict(by_id)
        for cid, text in extra:
            src[cid] = [text]
        hydrated = artifact.hydrate(recs, src)
        return {"pins": artifact.published_from_records(hydrated)["pins"],
                "cases": hydrated["cases"],
                "mechanism_holds": records["facts"]["mechanism"]["holds"]}

    added = _with(records, add=[("examples/zz_unrecorded.rvl", "agree-admit")])
    result = artifact.judge(committed,
                            local(added, [("examples/zz_unrecorded.rvl", "x")]))
    assert artifact.current_problems(result) == [
        "1 program in the corpus is not in the committed artifact"]

    moved = _with(records, bucket={0: "agree-refuse/G1"})
    result = artifact.judge(committed, local(moved))
    assert result["verdict"] == "refuted"
    assert artifact.current_problems(result)

    opened = json.loads(json.dumps(records))
    opened["pins"]["decides_verdicts"].append("tools/corpus_provenance.py")
    result = artifact.judge(committed, local(opened))
    assert result["unpinned_inputs"] == ["tools/corpus_provenance.py"]
    assert artifact.current_problems(result) == [
        "the verdict is different-inputs, not reproduced"]

    for name in artifact.RECORD_FILES:
        (tmp_path / name).write_text(
            (artifact.RECORDS / name).read_text(encoding="utf-8"),
            encoding="utf-8")
    fresh = {name.split("/")[-1]: text
             for name, text in _texts(artifact, added).items()}
    problems = artifact.record_problems(fresh, tmp_path)
    assert problems and all("cases.jsonl" in p for p in problems)


def test_verify_reads_the_records_directory(artifact, tmp_path):
    """`--verify` defaults to the records; an unreadable directory is
    unusable input (exit 2) before any census runs."""
    (tmp_path / "cases.jsonl").write_text("not json\n")
    code, text = artifact.verify(tmp_path)
    assert code == 2 and "cannot read the records" in text


# --- the crate reproduction stores no count (issue #1768) ---------------------
#
# `tests/fixtures/census_crate_reproduction.json` stored `n`, the number of
# programs the crate run covered. It was the one line every corpus-moving pull
# request that re-recorded rewrote, so any two of them conflicted on it. The
# directory that replaces it records WHICH programs the run covered, one per
# line, and the count is derived.


def _reproduction(programs, version="GATE-CENSUS-1+aaaaaaaaaaaa"):
    return {"note": "n", "engine": "crate", "checker_version": version,
            "tracked_buckets": {}, "false_admissions": [],
            "programs": sorted(programs)}


def _repro_files(artifact, recorded) -> dict[str, str]:
    return {f"tests/fixtures/census_crate_reproduction/{name}": text
            for name, text in artifact.reproduction_texts(recorded).items()}


def test_the_committed_reproduction_is_canonical_and_stores_no_count(artifact):
    recorded = artifact.load_reproduction()
    assert recorded is not None and recorded["programs"]
    facts = json.loads((artifact.CRATE_REPRODUCTION
                        / artifact.REPRODUCTION_FACTS).read_text())
    assert "n" not in facts and "programs" not in facts
    for rel, text in _repro_files(artifact, recorded).items():
        assert (ROOT / rel).read_text(encoding="utf-8") == text, (
            f"{rel} is not in canonical form; re-record it")
    assert recorded["programs"] == sorted(recorded["programs"])


def test_trim_records_the_programs_and_derives_the_count(artifact, census,
                                                         tmp_path):
    raw = {"engine": "crate",
           "buckets": {"agree-admit": ["b.rvl", "a.rvl"],
                       "agree-refuse/G1": ["oracle-reject:twice",
                                           "oracle-reject:twice"],
                       "false-admit/T1": ["c.rvl"]}}
    recorded = artifact.trim_reproduction(census, raw)
    assert recorded["programs"] == ["a.rvl", "b.rvl", "c.rvl",
                                    "oracle-reject:twice",
                                    "oracle-reject:twice"]
    assert recorded["tracked_buckets"] == {"false-admit/T1": ["c.rvl"]}
    assert "n" not in recorded
    for name, text in artifact.reproduction_texts(recorded).items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    loaded = artifact.load_reproduction(tmp_path)
    assert loaded["n"] == 5 and loaded["programs"] == recorded["programs"]


@_needs_merge_tree
def test_two_re_recordings_that_add_programs_merge(artifact):
    """The exit test for this file. Two pull requests each add a program and
    re-record the crate run at the same checker version. In the directory
    each adds one line and they merge; in the single file both rewrite `n`
    and conflict."""
    base = [f"examples/p{i:03d}.rvl" for i in range(0, 40, 2)]
    left = base + ["examples/p011.rvl"]
    right = base + ["examples/p031.rvl", "examples/p033.rvl"]
    clean, conflicted = merge(_repro_files(artifact, _reproduction(base)),
                              _repro_files(artifact, _reproduction(left)),
                              _repro_files(artifact, _reproduction(right)))
    assert clean, conflicted

    def old(programs):
        recorded = _reproduction(programs)
        recorded["n"] = len(recorded.pop("programs"))
        return {"tests/fixtures/census_crate_reproduction.json":
                json.dumps(recorded, indent=1, sort_keys=True) + "\n"}

    clean, conflicted = merge(old(base), old(left), old(right))
    assert not clean and conflicted == [
        "tests/fixtures/census_crate_reproduction.json"]


def test_the_reproduction_check_is_as_strict_and_names_what_it_missed(
        artifact, census, records, sources):
    """A reproduction recorded at another checker version still lifts no
    claim, whatever it covered, and one at the current version over fewer
    programs than the census now runs is reported with the gap counted."""
    provenance = _load("tools/corpus_provenance.py", "artifact_test_prov3")
    hydrated = artifact.hydrate(records, artifact.corpus_sources(sources))
    measured = artifact.measured_from_records(hydrated)
    version, _ = artifact.checker_version()
    ids = sorted({row[0] for row in measured["case_rows"]})

    def report(recorded):
        loaded = dict(recorded, n=len(recorded["programs"]))
        return artifact.build_report(census, provenance, measured, loaded,
                                     probe=records["facts"]["mechanism"])

    stale = report(_reproduction(ids))
    assert stale["census"]["reproduction"]["is_current"] is False
    assert all(c["rung"] != "demonstrated" for c in stale["claims"])

    short = report(_reproduction(ids[3:], version))
    rep = short["census"]["reproduction"]
    assert rep["is_current"] is True
    assert rep["census_programs_not_in_reproduction"] == 3
    assert ("census programs this run read that the reproduction did not: "
            "**3**") in artifact.render_markdown(short)
