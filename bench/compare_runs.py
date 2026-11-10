#!/usr/bin/env python3
"""Compare fresh runs of one benchmark rep index, field by field.

Answers one question: given M fresh runs of index N, which fields reproduced?
Reports per field, never one aggregate "pass", because the aggregate hides the
only thing worth knowing, which is which field moved. A run-to-run difference
and a cell-to-cell difference look identical in the log, so this tool reports
agreement between runs of the SAME index and never compares one index to
another.

Field groups:

  identity   must match across every run or the runs are not one cell (exit 2)
  advisory   a name that is not content, checked only when both sides carry it
  outcome    the agreement test, compared field by field
  tokens     reported separately, never folded into the verdict, because
             model verbosity varies even when the action sequence does not
  call_log   compared as a sequence and included in the verdict, so a different
             action order is a behavioural difference even when the counters
             match; when the sequence is identical and only `turns` moved, the
             tool says the calls were packed into different responses. A
             sequence recorded in one run and absent (missing or null) in
             another is a difference, not a skip; the sequence is left out only
             when no run carries it. A recorded empty list is a value: a run
             that made no call differs from one that made calls.

A field empty or null in EVERY run is excluded rather than counted as matching.
Two empty fields agree perfectly and measure nothing, so including them would
inflate the apparent size of the field set and dilute any disagreement. If no
outcome field is populated in any run, nothing was measured and the tool
refuses (exit 2) rather than reporting a vacuous reproduction.

Freshness is checked, not asserted: the same file twice, two names for one
byte-identical file, or the same recorded run stamp are all refused. The tool
cannot prove two processes did not overlap, and says so.

What the run stamp is. The withdraw driver that writes these records
(withdraw_k8_xmodel_resume.py, which is not in this repository) sets
`PROC_ID = f"{os.getpid()}_{int(_PROC_T0)}"` once per process at line 99 and,
inside the per-rep loop, `res["proc_id"] = PROC_ID` at line 119 and
`res["started_at"]` from that rep's own start time `t0` at lines 120-121,
at one-second resolution. So `proc_id` is per process and `started_at` is per
rep. Two records from different processes never share `proc_id`, so a shared
(`proc_id`, `started_at`) pair across two run files is one run recorded twice.
Two reps of ONE process share `proc_id` by design and can share `started_at`
when a rep ends within the second it started, so stamps are never compared
between records of the same file.

Report form is "index N reproduced across M fresh runs on all K compared
outcome fields", never "the determinism control passed": agreement here is
evidence about index N only, not about the other indices or the arms.

Usage:
  python3 bench/compare_runs.py --runs A.jsonl B.jsonl --index 0
  python3 bench/compare_runs.py --runs A.jsonl B.jsonl C.jsonl --index 0 \
      --fields withdrew,break_set_exact,turns

Exit codes:
  0  every compared outcome field and the call sequence reproduced
  1  at least one compared outcome field or the call sequence moved
  2  refused: unreadable or ambiguous input (including a line that is valid
     JSON but not an object), the same file or identical bytes twice, a
     repeated run stamp, an identity field absent or different, or a field set
     with nothing populated to compare
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

# revl_tree is deliberately NOT here. It is a directory NAME, not content: an
# older batch recorded it as null and a fresh run records "revlmain", so
# comparing it flags a mismatch between an absent label and a present one while
# the content sha is identical on both sides. Identity is revl_sha.
IDENTITY = ["model", "revl_sha", "arm_variant", "rep"]
ADVISORY = ["revl_tree"]
OUTCOME = [
    "withdrew", "break_set_exact", "named_breaks", "named_cause",
    "survivors_intact", "survivors_present", "compiles", "compile_err",
    "exhausted", "turns", "tool_calls", "mutating_calls", "edit_calls",
    "load_calls", "hatch_calls", "scaffold_calls", "query_withdraw_calls",
    "refusals", "max_refusal_run", "preflight_before_edit",
    "first_preflight_idx", "first_mutation_idx",
]
TOKENS = ["tokens_in", "tokens_new_prefill", "tokens_out", "write_tokens"]

# The calls the agent made, in order. Not the same question as how many
# responses the model took to make them: one response can carry two calls, so a
# turn-count difference with an identical call sequence is a packing artefact,
# not a different trajectory.
CALL_SEQ = "call_log"
FIELD_SET_NAME = "withdraw-cell outcome fields"


def load(path):
    """Every record in a jsonl file, or exit 2 naming the file and line."""
    records = []
    try:
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError as exc:
                    sys.stderr.write("cannot read %s line %d: %s\n"
                                     % (path, lineno, exc))
                    sys.exit(2)
                if not isinstance(record, dict):
                    sys.stderr.write(
                        "cannot read %s line %d: expected a JSON object, got "
                        "%s\n" % (path, lineno, type(record).__name__))
                    sys.exit(2)
                records.append(record)
    except (OSError, ValueError) as exc:
        sys.stderr.write("cannot read %s: %s\n" % (path, exc))
        sys.exit(2)
    return records


def pick(records, index, path):
    """Exactly one record for this index, or refuse rather than guess."""
    if index is None:
        if len(records) != 1:
            sys.stderr.write(
                "%s holds %d records; pass --index to choose one\n"
                % (path, len(records)))
            sys.exit(2)
        return records[0]
    hits = [r for r in records if r.get("rep") == index]
    if not hits:
        seen = sorted({r.get("rep") for r in records}, key=str)
        sys.stderr.write("%s holds no record with rep=%r; reps present: %s\n"
                         % (path, index, seen))
        sys.exit(2)
    if len(hits) > 1:
        sys.stderr.write(
            "%s holds %d records for rep=%r, so it cannot say which run to "
            "compare. Use a fresh output file per run.\n"
            % (path, len(hits), index))
        sys.exit(2)
    return hits[0]


def is_empty(value):
    return value is None or value == "" or value == [] or value == {}


def has_sequence(record):
    """True when the record carries a call sequence, even an empty one.

    An empty list is a recorded value (the run made no call), so only a
    missing key or null counts as absent.
    """
    return record.get(CALL_SEQ) is not None


def compare_sequences(records, labels):
    """Compare call sequences; None when no record carries one.

    Otherwise a dict with `matched`, `calls` and `lacking`, the labels of the
    records that do not carry the sequence while another does. A sequence
    present on one side and absent on the other is a difference.
    """
    lacking = [label for record, label in zip(records, labels)
               if not has_sequence(record)]
    if len(lacking) == len(records):
        return None
    if lacking:
        return {"matched": False, "calls": None, "lacking": lacking}
    first_seq = records[0].get(CALL_SEQ)
    return {
        "matched": all(r.get(CALL_SEQ) == first_seq for r in records[1:]),
        "calls": len(first_seq) if isinstance(first_seq, list) else None,
        "lacking": [],
    }


def same_file(first, second):
    """True when the two paths name the same file, or one cannot be told."""
    if os.path.realpath(first) == os.path.realpath(second):
        return True
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def file_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def distinct_files(paths):
    """Refuse two names for one run; return a refusal message or None."""
    for i, first in enumerate(paths):
        for second in paths[i + 1:]:
            if same_file(first, second):
                return ("%s and %s are the same file. Comparing a run against "
                        "itself always reports agreement, and that is the "
                        "failure this tool exists to detect, not to reproduce."
                        % (first, second))
            try:
                if file_digest(first) == file_digest(second):
                    return ("%s and %s have identical bytes. Two names for one "
                            "run are not two runs, so no agreement claim is "
                            "made." % (first, second))
            except OSError as exc:
                return "cannot read %s or %s: %s" % (first, second, exc)
    return None


def stamp_refusal(records, labels):
    """Refuse two records that carry the same recorded run stamp.

    Checked pairwise among the records that carry both fields, so a mixed
    generation where one record lacks the stamps still refuses a duplicate
    among the ones that have them.
    """
    stamped = [(record, label) for record, label in zip(records, labels)
               if record.get("proc_id") not in (None, "")
               and record.get("started_at") not in (None, "")]
    for i, (first, first_label) in enumerate(stamped):
        for second, second_label in stamped[i + 1:]:
            if (first.get("proc_id") == second.get("proc_id")
                    and first.get("started_at") == second.get("started_at")):
                return ("%s and %s carry the same run stamp (proc_id %r, "
                        "started_at %r). That is one run recorded twice, not a "
                        "pair." % (first_label, second_label,
                                   first.get("proc_id"),
                                   first.get("started_at")))
    return None


def run_stamps(records, paths):
    """Return per-run stamps for display."""
    return [{
        "name": os.path.basename(path),
        "digest": file_digest(path)[:16],
        "proc_id": record.get("proc_id"),
        "started_at": record.get("started_at"),
    } for record, path in zip(records, paths)]


def compare(records, fields, labels=None):
    """Return (matched, differed, skipped, identity_bad, sequence)."""
    if labels is None:
        labels = ["run %d" % i for i in range(len(records))]
    identity_bad = []
    for field in IDENTITY:
        values = [r.get(field) for r in records]
        if all(is_empty(value) for value in values):
            identity_bad.append((field, values))
        elif any(value != values[0] for value in values[1:]):
            identity_bad.append((field, values))
    for field in ADVISORY:
        values = [r.get(field) for r in records]
        if all(value for value in values) and any(
                value != values[0] for value in values[1:]):
            identity_bad.append((field, values))
    if identity_bad:
        return [], [], [], identity_bad, None

    sequence = compare_sequences(records, labels)

    matched, differed, skipped = [], [], []
    for field in fields:
        values = [r.get(field) for r in records]
        if all(is_empty(value) for value in values):
            skipped.append(field)
        elif all(value == values[0] for value in values[1:]):
            matched.append(field)
        else:
            differed.append((field, values))
    return matched, differed, skipped, identity_bad, sequence


def print_stamps(stamps, paths):
    print("run stamps:")
    for stamp, path in zip(stamps, paths):
        fields = "%s sha256=%s" % (stamp["name"], stamp["digest"])
        if stamp["proc_id"] not in (None, ""):
            fields += " proc_id=%s" % stamp["proc_id"]
        if stamp["started_at"] not in (None, ""):
            fields += " started_at=%s" % stamp["started_at"]
        print("  %s" % fields)
    if all(s["proc_id"] not in (None, "") and s["started_at"] not in (None, "")
           for s in stamps):
        print("freshness: distinct files, distinct digests and distinct "
              "recorded run stamps.")
    else:
        print("freshness: distinct files and digests. Run stamps are not "
              "recorded in every file, so")
        print("          the tool cannot show the runs were separate processes.")


def print_token_movement(records, paths):
    moved = []
    for field in TOKENS:
        values = [r.get(field) for r in records]
        if all(is_empty(value) for value in values):
            continue
        if any(value != values[0] for value in values[1:]):
            moved.append((field, values))
    if not moved:
        present = [f for f in TOKENS
                   if any(not is_empty(r.get(f)) for r in records)]
        if present:
            print("\ntoken counts identical across the runs: %s"
                  % ", ".join(present))
        else:
            print("\nno token field is present in these records")
        return
    print("\ntoken counts also moved, reported separately and not counted as "
          "differing outcomes:")
    for field, values in moved:
        rendered = " ".join(
            "%s=%r" % (os.path.basename(p), v) for p, v in zip(paths, values))
        print("  %-22s %s" % (field, rendered))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", nargs="+", required=True,
                    help="two or more fresh run records (jsonl)")
    ap.add_argument("--index", type=int, default=None,
                    help="rep index to compare")
    ap.add_argument("--fields", default=None,
                    help="comma-separated outcome field set; default is the "
                         "22-field withdraw-cell set in this module")
    args = ap.parse_args(argv)

    if len(args.runs) < 2:
        sys.stderr.write("refusing: --runs needs at least two files; comparing "
                         "one run with itself always reports agreement.\n")
        return 2
    refusal = distinct_files(args.runs)
    if refusal:
        sys.stderr.write("refusing: %s\n" % refusal)
        return 2

    records = [pick(load(path), args.index, path) for path in args.runs]
    refusal = stamp_refusal(records, args.runs)
    if refusal:
        sys.stderr.write("refusing: %s\n" % refusal)
        return 2
    stamps = run_stamps(records, args.runs)

    note = None
    if args.fields:
        fields = [f.strip() for f in args.fields.split(",") if f.strip()]
        if not fields:
            sys.stderr.write("refusing: --fields named no field, so the "
                             "comparison would measure nothing.\n")
            return 2
        token_fields = [f for f in fields if f in TOKENS]
        fields = [f for f in fields if f not in TOKENS]
        if not fields:
            sys.stderr.write(
                "refusing: the explicit field set named only token fields, "
                "which are reported separately. Name at least one outcome "
                "field.\n")
            return 2
        if token_fields:
            note = ("token fields removed from the explicit field set and "
                    "reported separately: %s" % ", ".join(token_fields))
        field_set = "explicit (%d field%s: %s)" % (
            len(fields), "" if len(fields) == 1 else "s", ", ".join(fields))
    else:
        fields = OUTCOME
        field_set = "%s (%d fields)" % (FIELD_SET_NAME, len(fields))

    matched, differed, skipped, identity_bad, sequence = compare(
        records, fields, [os.path.basename(p) for p in args.runs])
    if identity_bad:
        for field, values in identity_bad:
            rendered = " ".join(
                "%s=%r" % (os.path.basename(p), v)
                for p, v in zip(args.runs, values))
            print("IDENTITY MISMATCH %-12s %s" % (field, rendered))
        print("\nThese are not runs of the same configuration, so no agreement "
              "claim is made.")
        return 2

    compared = len(matched) + len(differed)
    if compared == 0:
        sys.stderr.write(
            "refusing: every outcome field is empty in every run, so there is "
            "nothing to compare and no agreement claim can be made.\n")
        return 2

    index = records[0].get("rep")
    print("comparing index %r across %d fresh runs"
          % (index, len(records)))
    print("field set: %s" % field_set)
    if note:
        print("NOTE  %s" % note)
    print("identity fields all match: %s" % ", ".join(IDENTITY))
    if not any("sys" in f or "prompt" in f for f in records[0]):
        print("NOTE: no field in these records identifies the system prompt, so "
              "this equality")
        print("      does not show the runs used the same prompt. Confirm the "
              "prompt separately.")
    print()
    print_stamps(stamps, args.runs)
    print()

    for field, values in differed:
        rendered = " ".join(
            "%s=%r" % (os.path.basename(p), v)
            for p, v in zip(args.runs, values))
        print("DIFFERED  %-24s %s" % (field, rendered))

    sequence_moved = False
    if sequence is None:
        print("call sequence: not present in any record, so it is not "
              "compared")
    elif sequence["lacking"]:
        sequence_moved = True
        print("DIFFERED  call_log                 recorded in some runs and "
              "absent in %s" % ", ".join(sequence["lacking"]))
    elif sequence["matched"]:
        calls = (" (%d calls)" % sequence["calls"]
                 if sequence["calls"] is not None else "")
        print("call sequence: identical%s" % calls)
    else:
        sequence_moved = True
        print("DIFFERED  call_log                 the action sequence changed")

    if (any(f == "turns" for f, _ in differed) and sequence is not None
            and sequence["matched"]):
        print("NOTE  turns differ but the call sequence is IDENTICAL: the same "
              "calls")
        print("      were packed into a different number of model responses, so "
              "the")
        print("      agent did the same work and only its batching changed.")

    print("matched %d of %d outcome fields"
          % (len(matched), compared))
    if skipped:
        print("excluded %d empty in every run: %s"
              % (len(skipped), ", ".join(skipped)))
    else:
        print("no outcome field was empty in every run")

    print_token_movement(records, args.runs)

    print()
    if differed or sequence_moved:
        moved = len(differed) + (1 if sequence_moved else 0)
        print("index %r is NOT reproduced: %d field%s moved between the runs."
              % (index, moved, "" if moved == 1 else "s"))
        return 1
    tail = ""
    if sequence is not None:
        tail = " and an identical call sequence"
    elif compared:
        tail = "; the call sequence was not compared"
    print("index %r reproduced across %d fresh runs on all %d compared "
          "outcome fields%s." % (index, len(records), compared, tail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
