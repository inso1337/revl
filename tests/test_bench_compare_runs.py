"""The run comparison protocol: agreement named by index and field set.

`bench/compare_runs.py` and `bench/compare_sweep.py` exist because a
cell-to-cell difference and a run-to-run difference look identical in a
benchmark log. The properties pinned here are the ones that keep the report
honest:

  1. The comparison names a specific index and a specific field set, in both
     the pair report and the sweep report.
  2. A field empty in every run is excluded, not counted as agreeing.
  3. A field empty in one run only is compared, not excluded.
  4. The same file twice, two names for identical bytes, or the same recorded
     run stamp is refused, because comparing a run with itself always reports
     agreement.
  5. An identity mismatch or an absent identity field is refused: two
     different or unverifiable configurations are not a determinism pair.
  6. Token movement is reported separately and never decides the verdict.
  7. The action sequence is compared and decides the verdict. A turn-count
     difference with an identical sequence is called a packing difference, and
     an absent sequence is never called identical. A sequence recorded on one
     side and absent on the other moved, and an empty recorded sequence is a
     value, not absence.
  8. A comparison with no populated outcome field is refused rather than
     reported as a vacuous reproduction.
  9. The sweep compares index to index and reports per position, never one
     aggregate pass; an incomplete sweep is refused, including (with --reps)
     two files that lack the same rep.
 10. Run stamps are compared across the two sweep files, never between two
     reps of one process, and a line that is not a JSON object is refused
     with its line number.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bench"))

import compare_runs  # noqa: E402
import compare_sweep  # noqa: E402

CALL_LOG = [
    "read_file", "revl_load", "revl_query_withdraw", "revl_change",
    "revl_check", "revl_verbs->revl_export", "revl_verbs->revl_export",
    "read_file",
]


def record(rep=0, **overrides):
    row = {field: True for field in compare_runs.OUTCOME}
    row.update({
        "model": "test-model",
        "revl_sha": "0123456789ab",
        "arm_variant": "warm",
        "rep": rep,
        "turns": 8,
        "tool_calls": 8,
        "compile_err": "none",
        "tokens_in": 1000,
        "tokens_new_prefill": 500,
        "tokens_out": 200,
        "write_tokens": 20,
        "call_log": list(CALL_LOG),
        "run_seq": 0,
        "wall_s": 100.0,
    })
    row.update(overrides)
    return row


_RUN = [0]


def write(tmp_path, name, rows, stamp=True):
    """Write records, giving each a distinct run stamp by default.

    Two files with identical bytes are refused as one run recorded twice, so a
    test that wants two runs must not accidentally write identical files.
    """
    path = tmp_path / name
    lines = []
    for row in rows:
        row = dict(row)
        _RUN[0] += 1
        if stamp:
            row.setdefault("proc_id", "proc-%d" % _RUN[0])
            row.setdefault(
                "started_at",
                "2026-10-19T19:%02d:%02d+00:00"
                % (_RUN[0] % 60, _RUN[0] % 60))
        lines.append(json.dumps(row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_pair_names_index_and_field_set_and_excludes_empty_in_every_run(
        tmp_path, capsys):
    a = write(tmp_path, "a.jsonl", [record(compile_err=None)])
    b = write(tmp_path, "b.jsonl", [record(compile_err=None)])

    assert compare_runs.main(["--runs", a, b, "--index", "0"]) == 0
    out = capsys.readouterr().out
    assert "comparing index 0 across 2 fresh runs" in out
    assert "field set: withdraw-cell outcome fields (22 fields)" in out
    assert "excluded 1 empty in every run: compile_err" in out
    assert "index 0 reproduced across 2 fresh runs on all 21 compared" in out
    assert "and an identical call sequence" in out


def test_empty_in_one_run_only_is_compared(tmp_path, capsys):
    a = write(tmp_path, "a.jsonl", [record(compile_err="")])
    b = write(tmp_path, "b.jsonl", [record(compile_err="boom")])

    assert compare_runs.main(["--runs", a, b, "--index", "0"]) == 1
    out = capsys.readouterr().out
    assert "DIFFERED  compile_err" in out
    assert "NOT reproduced" in out


def test_same_file_is_refused(tmp_path, capsys):
    a = write(tmp_path, "a.jsonl", [record()])

    assert compare_runs.main(["--runs", a, a]) == 2
    err = capsys.readouterr().err
    assert "are the same file" in err


def test_identical_bytes_under_two_names_are_refused(tmp_path, capsys):
    body = json.dumps(record()) + "\n"
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    a.write_text(body, encoding="utf-8")
    b.write_text(body, encoding="utf-8")

    assert compare_runs.main(["--runs", str(a), str(b), "--index", "0"]) == 2
    assert "identical bytes" in capsys.readouterr().err


def test_same_recorded_run_stamp_is_refused(tmp_path, capsys):
    same = {"proc_id": "1_2", "started_at": "2026-10-19T19:40:29+00:00"}
    a = write(tmp_path, "a.jsonl", [record(**same)], stamp=False)
    b = write(tmp_path, "b.jsonl", [record(**same, tokens_out=999)],
              stamp=False)

    assert compare_runs.main(["--runs", a, b, "--index", "0"]) == 2
    assert "same run stamp" in capsys.readouterr().err


def test_missing_and_duplicate_index_are_refused(tmp_path, capsys):
    a = write(tmp_path, "a.jsonl", [record(rep=0)])
    b = write(tmp_path, "b.jsonl", [record(rep=0), record(rep=0)])

    with pytest.raises(SystemExit) as exc:
        compare_runs.main(["--runs", a, b, "--index", "0"])
    assert exc.value.code == 2
    assert "holds 2 records for rep=0" in capsys.readouterr().err

    with pytest.raises(SystemExit) as exc:
        compare_runs.main(["--runs", a, b, "--index", "7"])
    assert exc.value.code == 2
    assert "holds no record with rep=7" in capsys.readouterr().err


def test_identity_mismatch_is_refused(tmp_path, capsys):
    a = write(tmp_path, "a.jsonl", [record()])
    b = write(tmp_path, "b.jsonl", [record(revl_sha="ffffffffffff")])

    assert compare_runs.main(["--runs", a, b, "--index", "0"]) == 2
    out = capsys.readouterr().out
    assert "IDENTITY MISMATCH revl_sha" in out
    assert "no agreement claim is made" in out


def test_identity_field_absent_from_every_run_is_refused(tmp_path, capsys):
    a = write(tmp_path, "a.jsonl", [dict(record(), model=None)])
    b = write(tmp_path, "b.jsonl", [dict(record(), model=None)])

    assert compare_runs.main(["--runs", a, b, "--index", "0"]) == 2
    out = capsys.readouterr().out
    assert "IDENTITY MISMATCH model" in out


def test_token_movement_does_not_decide_the_verdict(tmp_path, capsys):
    a = write(tmp_path, "a.jsonl", [record(tokens_out=200)])
    b = write(tmp_path, "b.jsonl", [record(tokens_out=999)])

    assert compare_runs.main(["--runs", a, b, "--index", "0"]) == 0
    out = capsys.readouterr().out
    assert "token counts also moved, reported separately" in out
    assert "tokens_out" in out
    assert "reproduced across 2 fresh runs" in out


def test_tokens_named_in_the_field_set_are_reported_separately(
        tmp_path, capsys):
    a = write(tmp_path, "a.jsonl", [record(tokens_out=200)])
    b = write(tmp_path, "b.jsonl", [record(tokens_out=999)])

    assert compare_runs.main(
        ["--runs", a, b, "--index", "0",
         "--fields", "turns,tokens_out"]) == 0
    out = capsys.readouterr().out
    assert "token fields removed from the explicit field set" in out
    assert "field set: explicit (1 field: turns)" in out
    assert "on all 1 compared outcome fields" in out


def test_absent_token_fields_are_not_called_identical(tmp_path, capsys):
    a = write(tmp_path, "a.jsonl", [
        record(tokens_in=None, tokens_new_prefill=None, tokens_out=None,
               write_tokens=None)])
    b = write(tmp_path, "b.jsonl", [
        record(tokens_in=None, tokens_new_prefill=None, tokens_out=None,
               write_tokens=None)])

    assert compare_runs.main(["--runs", a, b, "--index", "0"]) == 0
    assert "no token field is present in these records" in (
        capsys.readouterr().out)


def test_turns_with_identical_call_sequence_is_a_packing_difference(
        tmp_path, capsys):
    a = write(tmp_path, "a.jsonl", [record(turns=8)])
    b = write(tmp_path, "b.jsonl", [record(turns=7)])

    assert compare_runs.main(["--runs", a, b, "--index", "0"]) == 1
    out = capsys.readouterr().out
    assert "DIFFERED  turns" in out
    assert "call sequence is IDENTICAL" in out
    assert "only its batching changed" in out


def test_a_changed_call_sequence_is_a_behavioural_difference(
        tmp_path, capsys):
    a = write(tmp_path, "a.jsonl", [record()])
    b = write(tmp_path, "b.jsonl", [
        record(call_log=list(reversed(CALL_LOG)))])

    assert compare_runs.main(["--runs", a, b, "--index", "0"]) == 1
    out = capsys.readouterr().out
    assert "DIFFERED  call_log" in out
    assert "is NOT reproduced" in out
    assert "reproduced across" not in out


def test_absent_call_sequence_is_not_called_identical(tmp_path, capsys):
    a = write(tmp_path, "a.jsonl", [record(turns=8, call_log=None)])
    b = write(tmp_path, "b.jsonl", [record(turns=7, call_log=None)])

    assert compare_runs.main(["--runs", a, b, "--index", "0"]) == 1
    out = capsys.readouterr().out
    assert "not present in any record" in out
    assert "call sequence is IDENTICAL" not in out


def test_no_populated_outcome_field_is_refused(tmp_path, capsys):
    empty = {field: None for field in compare_runs.OUTCOME}
    a = write(tmp_path, "a.jsonl", [record(**empty)])
    b = write(tmp_path, "b.jsonl", [record(**empty)])

    assert compare_runs.main(["--runs", a, b, "--index", "0"]) == 2
    assert "nothing to compare" in capsys.readouterr().err


def test_three_runs_are_compared_against_the_first(tmp_path, capsys):
    paths = [write(tmp_path, "r%d.jsonl" % i, [record(rep=3)])
             for i in range(3)]

    assert compare_runs.main(["--runs", *paths, "--index", "3"]) == 0
    out = capsys.readouterr().out
    assert "comparing index 3 across 3 fresh runs" in out
    assert "across 3 fresh runs on all 22 compared" in out


def test_explicit_field_set_is_named_and_limits_the_comparison(
        tmp_path, capsys):
    a = write(tmp_path, "a.jsonl", [record(withdrew=True, turns=8)])
    b = write(tmp_path, "b.jsonl", [record(withdrew=True, turns=7)])

    assert compare_runs.main(
        ["--runs", a, b, "--index", "0",
         "--fields", "withdrew,tool_calls"]) == 0
    out = capsys.readouterr().out
    assert "field set: explicit (2 fields: withdrew, tool_calls)" in out
    assert "all 2 compared outcome fields" in out


def test_empty_explicit_field_set_is_refused(tmp_path, capsys):
    a = write(tmp_path, "a.jsonl", [record()])
    b = write(tmp_path, "b.jsonl", [record()])

    assert compare_runs.main(
        ["--runs", a, b, "--index", "0", "--fields", " , "]) == 2
    assert "named no field" in capsys.readouterr().err


def test_sweep_compares_position_to_position_and_names_its_field_set(
        tmp_path, capsys):
    orig = write(tmp_path, "orig.jsonl", [
        record(rep=0),
        record(rep=1, turns=8),
        record(rep=2),
    ])
    fresh = write(tmp_path, "fresh.jsonl", [
        record(rep=0),
        record(rep=1, turns=7),
        record(rep=2),
    ])

    assert compare_sweep.main(["--orig", orig, "--fresh", fresh]) == 1
    out = capsys.readouterr().out
    assert "field set: withdraw-cell outcome fields (22 outcome fields)" in out
    assert "2 of 3 positions reproduced" in out
    assert "position 0: reproduced on 22 of 22 outcome fields" in out
    assert "position 1: DID NOT reproduce on 21 of 22" in out
    assert "call sequence is IDENTICAL" in out
    assert "position 2: reproduced on 22 of 22 outcome fields" in out
    assert "Call sequence reproduced at 3 of 3 positions" in out


def test_sweep_counts_a_changed_sequence_as_not_reproduced(tmp_path, capsys):
    orig = write(tmp_path, "orig.jsonl", [record(rep=0)])
    fresh = write(tmp_path, "fresh.jsonl", [
        record(rep=0, call_log=list(reversed(CALL_LOG)))])

    assert compare_sweep.main(["--orig", orig, "--fresh", fresh]) == 1
    out = capsys.readouterr().out
    assert "22 of 22 outcome fields matched, but the call sequence changed" in out
    assert "DIFFERED call_log" in out
    assert "0 of 1 positions reproduced" in out


def test_sweep_with_a_missing_position_is_incomplete(tmp_path, capsys):
    orig = write(tmp_path, "orig.jsonl", [
        record(rep=0), record(rep=1), record(rep=2)])
    fresh = write(tmp_path, "fresh.jsonl", [record(rep=0), record(rep=1)])

    assert compare_sweep.main(["--orig", orig, "--fresh", fresh]) == 2
    out = capsys.readouterr().out
    assert "POSITIONS PRESENT IN ONLY ONE SWEEP: [2]" in out
    assert "INCOMPLETE" in out
    assert "is not a pass" in out


def test_sweep_identity_mismatch_is_not_a_pass(tmp_path, capsys):
    orig = write(tmp_path, "orig.jsonl", [record(rep=0)])
    fresh = write(tmp_path, "fresh.jsonl", [record(rep=0, revl_sha="f" * 12)])

    assert compare_sweep.main(["--orig", orig, "--fresh", fresh]) == 2
    out = capsys.readouterr().out
    assert "IDENTITY MISMATCH" in out
    assert "not a pass" in out


def test_sweep_identity_field_absent_in_both_is_not_a_pass(tmp_path, capsys):
    orig = write(tmp_path, "orig.jsonl", [dict(record(rep=0), model=None)])
    fresh = write(tmp_path, "fresh.jsonl", [dict(record(rep=0), model=None)])

    assert compare_sweep.main(["--orig", orig, "--fresh", fresh]) == 2
    out = capsys.readouterr().out
    assert "IDENTITY MISMATCH on model" in out
    assert "not a pass" in out


def test_sweep_vacuous_position_is_not_a_pass(tmp_path, capsys):
    empty = {field: None for field in compare_runs.OUTCOME}
    orig = write(tmp_path, "orig.jsonl", [record(rep=0, **empty)])
    fresh = write(tmp_path, "fresh.jsonl", [record(rep=0, **empty)])

    assert compare_sweep.main(["--orig", orig, "--fresh", fresh]) == 2
    out = capsys.readouterr().out
    assert "NOT COMPARED" in out
    assert "not a pass" in out


def test_token_only_explicit_field_set_is_refused(tmp_path, capsys):
    a = write(tmp_path, "a.jsonl", [record()])
    b = write(tmp_path, "b.jsonl", [record()])

    assert compare_runs.main(
        ["--runs", a, b, "--index", "0", "--fields", "tokens_out"]) == 2
    assert "named only token fields" in capsys.readouterr().err


def test_pair_duplicate_stamp_is_refused_when_one_run_lacks_stamps(
        tmp_path, capsys):
    same = {"proc_id": "1_2", "started_at": "2026-10-19T19:40:29+00:00"}
    a = write(tmp_path, "a.jsonl", [record(**same)], stamp=False)
    b = write(tmp_path, "b.jsonl", [record(**same, tokens_out=999)],
              stamp=False)
    c = write(tmp_path, "c.jsonl", [record(tokens_out=777)], stamp=False)

    assert compare_runs.main(["--runs", a, b, c, "--index", "0"]) == 2
    assert "same run stamp" in capsys.readouterr().err


def test_sweep_repeated_run_stamp_is_refused(tmp_path, capsys):
    same = {"proc_id": "1_2", "started_at": "2026-10-19T19:40:29+00:00"}
    orig = write(tmp_path, "orig.jsonl", [record(rep=0, **same)],
                 stamp=False)
    fresh = write(tmp_path, "fresh.jsonl",
                  [record(rep=0, **same, tokens_out=999)], stamp=False)

    assert compare_sweep.main(["--orig", orig, "--fresh", fresh]) == 2
    assert "same run stamp" in capsys.readouterr().err


def test_sweep_same_file_is_refused(tmp_path, capsys):
    a = write(tmp_path, "a.jsonl", [record(rep=0)])

    assert compare_sweep.main(["--orig", a, "--fresh", a]) == 2
    assert "same file" in capsys.readouterr().err


def test_sweep_duplicate_position_is_refused(tmp_path, capsys):
    orig = write(tmp_path, "orig.jsonl", [record(rep=0)])
    fresh = write(tmp_path, "fresh.jsonl", [record(rep=0), record(rep=0)])

    with pytest.raises(SystemExit) as exc:
        compare_sweep.main(["--orig", orig, "--fresh", fresh])
    assert exc.value.code == 2
    assert "holds 2 records for rep=0" in capsys.readouterr().err


def test_call_sequence_on_one_side_only_is_a_difference(tmp_path, capsys):
    a = write(tmp_path, "a.jsonl", [record()])
    lacking = record()
    del lacking["call_log"]
    b = write(tmp_path, "b.jsonl", [lacking])

    assert compare_runs.main(["--runs", a, b, "--index", "0"]) == 1
    out = capsys.readouterr().out
    assert "DIFFERED  call_log" in out
    assert "absent in b.jsonl" in out
    assert "NOT reproduced" in out


def test_empty_recorded_call_sequence_is_a_value(tmp_path, capsys):
    a = write(tmp_path, "a.jsonl", [record(call_log=[])])
    b = write(tmp_path, "b.jsonl", [record()])

    assert compare_runs.main(["--runs", a, b, "--index", "0"]) == 1
    out = capsys.readouterr().out
    assert "the action sequence changed" in out


def test_sweep_call_sequence_on_one_side_only_is_a_difference(
        tmp_path, capsys):
    orig = write(tmp_path, "orig.jsonl", [record(rep=0)])
    fresh = write(tmp_path, "fresh.jsonl", [record(rep=0, call_log=None)])

    assert compare_sweep.main(["--orig", orig, "--fresh", fresh]) == 1
    out = capsys.readouterr().out
    assert "absent in fresh" in out
    assert "0 of 1 positions reproduced" in out


def test_sweep_empty_recorded_call_sequence_is_a_value(tmp_path, capsys):
    orig = write(tmp_path, "orig.jsonl", [record(rep=0, call_log=[])])
    fresh = write(tmp_path, "fresh.jsonl", [record(rep=0)])

    assert compare_sweep.main(["--orig", orig, "--fresh", fresh]) == 1
    out = capsys.readouterr().out
    assert "DIFFERED call_log" in out
    assert "0 of 1 positions reproduced" in out


def test_sweep_reps_refuses_a_rep_missing_from_both_files(tmp_path, capsys):
    orig = write(tmp_path, "orig.jsonl", [record(rep=0), record(rep=1)])
    fresh = write(tmp_path, "fresh.jsonl", [record(rep=0), record(rep=1)])

    assert compare_sweep.main(
        ["--orig", orig, "--fresh", fresh, "--reps", "3"]) == 2
    err = capsys.readouterr().err
    assert "does not cover range(3)" in err
    assert "reps missing: [2]" in err


def test_sweep_reps_covered_is_checked_and_named(tmp_path, capsys):
    orig = write(tmp_path, "orig.jsonl", [record(rep=0), record(rep=1)])
    fresh = write(tmp_path, "fresh.jsonl", [record(rep=0), record(rep=1)])

    assert compare_sweep.main(
        ["--orig", orig, "--fresh", fresh, "--reps", "2"]) == 0
    assert "rep count: both sweeps cover range(2)" in capsys.readouterr().out


def test_sweep_without_reps_says_the_count_was_not_checked(tmp_path, capsys):
    orig = write(tmp_path, "orig.jsonl", [record(rep=0)])
    fresh = write(tmp_path, "fresh.jsonl", [record(rep=0)])

    assert compare_sweep.main(["--orig", orig, "--fresh", fresh]) == 0
    assert "rep count: not checked" in capsys.readouterr().out


def test_sweep_two_reps_of_one_process_are_not_one_run(tmp_path, capsys):
    # The driver writes proc_id once per process and started_at per rep at
    # one-second resolution, so two reps of one sweep can carry the same pair.
    same = {"proc_id": "4242_1760900000",
            "started_at": "2026-10-19T19:40:29+00:00"}
    orig = write(tmp_path, "orig.jsonl",
                 [record(rep=0, **same), record(rep=1, **same)], stamp=False)
    fresh = write(tmp_path, "fresh.jsonl", [record(rep=0), record(rep=1)])

    assert compare_sweep.main(["--orig", orig, "--fresh", fresh]) == 0
    captured = capsys.readouterr()
    assert "same run stamp" not in captured.err
    assert "2 of 2 positions reproduced" in captured.out


def test_sweep_stamp_shared_across_files_at_another_position_is_refused(
        tmp_path, capsys):
    same = {"proc_id": "1_2", "started_at": "2026-10-19T19:40:29+00:00"}
    orig = write(tmp_path, "orig.jsonl",
                 [record(rep=0, **same), record(rep=1)], stamp=False)
    fresh = write(tmp_path, "fresh.jsonl",
                  [record(rep=0), record(rep=1, **same)], stamp=False)

    assert compare_sweep.main(["--orig", orig, "--fresh", fresh]) == 2
    assert "same run stamp" in capsys.readouterr().err


@pytest.mark.parametrize("line", ["[]", "5", '"text"', "null"])
def test_pair_non_object_line_is_refused_with_its_line_number(
        tmp_path, capsys, line):
    a = write(tmp_path, "a.jsonl", [record()])
    b = tmp_path / "b.jsonl"
    b.write_text(json.dumps(record(tokens_out=1)) + "\n" + line + "\n",
                 encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        compare_runs.main(["--runs", a, str(b), "--index", "0"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "b.jsonl line 2" in err
    assert "expected a JSON object" in err


def test_sweep_non_object_line_is_refused_with_its_line_number(
        tmp_path, capsys):
    orig = write(tmp_path, "orig.jsonl", [record(rep=0)])
    fresh = tmp_path / "fresh.jsonl"
    fresh.write_text("[]\n" + json.dumps(record(rep=0, tokens_out=1)) + "\n",
                     encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        compare_sweep.main(["--orig", orig, "--fresh", str(fresh)])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "fresh.jsonl line 1" in err
    assert "expected a JSON object" in err
