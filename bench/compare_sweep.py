#!/usr/bin/env python3
"""Position-matched comparison of a fresh K-rep sweep against the original.

This is the control the benchmark spec names: a full sweep of all K reps in a
fresh process, compared to the original sweep position by position. Comparing
aggregates instead hides exactly the within-arm variation the control is for,
and comparing position i of one arm to position j of another is not a position
match at all.

Reuses the field groups from compare_runs.py so the two tools cannot drift:

    outcome fields   compared, per position
    timing fields    excluded, because they carry warmup rather than outcome
    empty in both    excluded rather than counted as agreement
    token fields     reported per position, never folded into the verdict
    call_log         compared as a sequence and included in the verdict; at a
                     position where one record carries it and the other does
                     not, the position did not reproduce

Reports each position separately and never one aggregate pass. A sweep with
positions present in only one file is INCOMPLETE (exit 2): a partial sweep
cannot answer a control defined over all K positions. Two files that lack the
SAME position cannot be told apart from complete ones without the expected
count, so `--reps K` refuses (exit 2) when either file does not cover
range(K); without it the output says the rep count was not checked. A
position with no populated outcome field is refused rather than counted as
reproduced.

Freshness is checked, not asserted: the same file twice, two names for one
byte-identical file, or a run stamp shared across the two files are refused.
The tool cannot prove the runs did not overlap, and says so. The driver writes
`proc_id` once per process and `started_at` per rep at one-second resolution
(see compare_runs.py for the file and lines), so two reps of one sweep share
`proc_id` by design and may share `started_at`. Stamps are therefore compared
only between a record of the original and a record of the fresh sweep, never
between two records of one file.

Usage:
  python3 bench/compare_sweep.py --orig ORIGINAL.jsonl --fresh FRESH.jsonl
  python3 bench/compare_sweep.py --orig ORIGINAL.jsonl --fresh FRESH.jsonl \
      --reps 8

Exit codes:
  0  every shared position reproduced
  1  at least one position did not reproduce (report per position)
  2  refused: unreadable or duplicate positions, a line that is not a JSON
     object, no shared position, a position missing from one file or (with
     --reps) from range(K), the same file or identical bytes, a run stamp
     shared across the two files, an identity field absent or different, or a
     position with nothing populated to compare
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compare_runs import (  # noqa: E402
    ADVISORY, CALL_SEQ, FIELD_SET_NAME, IDENTITY, OUTCOME, TOKENS,
    distinct_files, file_digest, has_sequence, is_empty, stamp_refusal)
from compare_runs import load as load_records  # noqa: E402


def load(path):
    recs = load_records(path)
    if not recs:
        sys.stderr.write("%s holds no records\n" % path)
        sys.exit(2)
    by_rep = {}
    for rec in recs:
        rep = rec.get("rep")
        if rep in by_rep:
            seen = sum(1 for r in recs if r.get("rep") == rep)
            sys.stderr.write(
                "%s holds %d records for rep=%r, so it cannot say which run to "
                "compare.\n" % (path, seen, rep))
            sys.exit(2)
        by_rep[rep] = rec
    return by_rep


def cross_stamp_refusal(orig, fresh, orig_name, fresh_name):
    """Refuse a run stamp shared between a record of each file.

    Records of one file are never compared with each other: one sweep is one
    process, so its reps share `proc_id`, and `started_at` is per rep at
    one-second resolution, so two of its reps can share that too.
    """
    for i, ro in sorted(orig.items(), key=lambda item: str(item[0])):
        for j, rf in sorted(fresh.items(), key=lambda item: str(item[0])):
            refusal = stamp_refusal(
                [ro, rf], ["%s rep %r" % (orig_name, i),
                           "%s rep %r" % (fresh_name, j)])
            if refusal:
                return refusal
    return None


def uncovered(by_rep, reps):
    return [i for i in range(reps) if i not in by_rep]


def has_stamps(by_rep):
    return any(r.get("proc_id") not in (None, "")
               and r.get("started_at") not in (None, "")
               for r in by_rep.values())


def stamp_ids(by_rep, label):
    """A compact summary of the process stamps one file records."""
    values = [(r.get("proc_id"), r.get("started_at"))
              for r in by_rep.values()
              if r.get("proc_id") not in (None, "")
              and r.get("started_at") not in (None, "")]
    if not values:
        return "%s: no run stamps recorded" % label
    ids = sorted({proc_id for proc_id, _ in values}, key=str)
    return "%s: proc_id %s" % (label, ", ".join(ids))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--orig", required=True, help="the original sweep jsonl")
    ap.add_argument("--fresh", required=True, help="the fresh sweep jsonl")
    ap.add_argument("--reps", type=int, default=None,
                    help="the expected rep count K; refuse unless both files "
                         "cover range(K)")
    a = ap.parse_args(argv)
    if a.reps is not None and a.reps < 1:
        sys.stderr.write("refusing: --reps must be at least 1\n")
        return 2

    refusal = distinct_files([a.orig, a.fresh])
    if refusal:
        sys.stderr.write("refusing: %s\n" % refusal)
        return 2

    orig = load(a.orig)
    fresh = load(a.fresh)

    if a.reps is not None:
        for name, by_rep in ((a.orig, orig), (a.fresh, fresh)):
            gaps = uncovered(by_rep, a.reps)
            if gaps:
                sys.stderr.write(
                    "refusing: %s does not cover range(%d); reps missing: %s. "
                    "A partial sweep cannot answer the control.\n"
                    % (name, a.reps, gaps))
                return 2

    refusal = cross_stamp_refusal(orig, fresh, Path(a.orig).name,
                                  Path(a.fresh).name)
    if refusal:
        sys.stderr.write("refusing: %s\n" % refusal)
        return 2

    missing = sorted(set(orig) ^ set(fresh), key=str)
    positions = sorted(set(orig) & set(fresh), key=str)
    print("field set: %s (%d outcome fields)" % (FIELD_SET_NAME, len(OUTCOME)))
    print("run files: %s sha256=%s  %s sha256=%s"
          % (Path(a.orig).name, file_digest(a.orig)[:16],
             Path(a.fresh).name, file_digest(a.fresh)[:16]))
    print("run stamps: %s" % stamp_ids(orig, "orig"))
    print("            %s" % stamp_ids(fresh, "fresh"))
    print("positions in both sweeps: %s" % positions)
    if a.reps is None:
        print("rep count: not checked; pass --reps K to refuse a sweep that "
              "does not cover range(K)")
    else:
        print("rep count: both sweeps cover range(%d)" % a.reps)
    if missing:
        print("POSITIONS PRESENT IN ONLY ONE SWEEP: %s" % missing)
    if not positions:
        sys.stderr.write("the two files share no rep index, so there is nothing "
                         "to compare position by position\n")
        return 2

    reproduced = 0
    sequence_reproduced = 0
    sequence_present = 0
    identity_bad = False
    vacuous = False
    for i in positions:
        ro, rf = orig[i], fresh[i]
        bad = [f for f in IDENTITY
               if (is_empty(ro.get(f)) and is_empty(rf.get(f)))
               or ro.get(f) != rf.get(f)]
        bad += [f for f in ADVISORY
                if ro.get(f) and rf.get(f) and ro.get(f) != rf.get(f)]
        if bad:
            identity_bad = True
            print("\nposition %r: IDENTITY MISMATCH on %s, not compared"
                  % (i, ", ".join(bad)))
            continue
        matched, differed, skipped = [], [], []
        for f in OUTCOME:
            x, y = ro.get(f), rf.get(f)
            if is_empty(x) and is_empty(y):
                skipped.append(f)
            elif x == y:
                matched.append(f)
            else:
                differed.append((f, x, y))
        compared = len(matched) + len(differed)
        if compared == 0:
            vacuous = True
            print("\nposition %r: NOT COMPARED, every outcome field was empty "
                  "in both runs" % (i,))
            continue

        seq_present = has_sequence(ro) or has_sequence(rf)
        seq_lacking = [name for name, rec in (("orig", ro), ("fresh", rf))
                       if seq_present and not has_sequence(rec)]
        seq_matched = (seq_present and not seq_lacking
                       and ro.get(CALL_SEQ) == rf.get(CALL_SEQ))
        if seq_present:
            sequence_present += 1
            if seq_matched:
                sequence_reproduced += 1

        position_ok = not differed and (not seq_present or seq_matched)
        if not differed and not seq_present:
            print("\nposition %r: reproduced on %d of %d outcome fields; call "
                  "sequence not compared" % (i, len(matched), compared))
        elif not differed and seq_matched:
            print("\nposition %r: reproduced on %d of %d outcome fields"
                  % (i, len(matched), compared))
        elif not differed:
            print("\nposition %r: %d of %d outcome fields matched, but the call "
                  "sequence changed" % (i, len(matched), compared))
        else:
            print("\nposition %r: DID NOT reproduce on %d of %d outcome fields"
                  % (i, len(matched), compared))
        if skipped:
            print("  excluded %d empty in both: %s"
                  % (len(skipped), ", ".join(skipped)))
        for f, x, y in differed:
            print("  DIFFERED %-24s orig=%r fresh=%r" % (f, x, y))
        if not seq_present:
            print("  call sequence: not present in either record, so it is not "
                  "compared")
        elif seq_lacking:
            print("  DIFFERED call_log                 recorded in one run and "
                  "absent in %s" % seq_lacking[0])
        elif seq_matched:
            print("  call sequence: identical")
        else:
            print("  DIFFERED call_log                 the action sequence "
                  "changed")
        if (any(f == "turns" for f, _, _ in differed) and seq_matched):
            print("  NOTE turns differ but the call sequence is IDENTICAL: same "
                  "calls,")
            print("       packed into a different number of model responses, so "
                  "the")
            print("       agent did the same work and only its batching changed.")
        moved = [(f, ro.get(f), rf.get(f)) for f in TOKENS
                 if ro.get(f) != rf.get(f)
                 and not (is_empty(ro.get(f)) and is_empty(rf.get(f)))]
        if moved:
            print("  tokens moved (reported, not counted): %s"
                  % ", ".join(f for f, _, _ in moved))
        if position_ok:
            reproduced += 1

    print("\n%d of %d positions reproduced across the two sweeps."
          % (reproduced, len(positions)))
    if sequence_present:
        print("Call sequence reproduced at %d of %d positions where either "
              "record carries it." % (sequence_reproduced, sequence_present))
    else:
        print("Call sequence: no position carries it in either record, so it "
              "is not compared.")
    if has_stamps(orig) and has_stamps(fresh):
        print("Freshness: distinct files and digests; no run stamp is shared "
              "across the two")
        print("           sweeps. Overlapping processes cannot be ruled out "
              "from the records.")
    else:
        print("Freshness: distinct files and digests; one file records no run "
              "stamps, so")
        print("           shared stamps cannot be ruled out from the records.")
    if missing:
        print("\nINCOMPLETE: %d position(s) present in only one sweep (%s). A "
              "partial" % (len(missing), missing))
        print("sweep cannot answer the control, so this is not a pass. Re-run "
              "the missing reps.")
        return 2
    if identity_bad or vacuous:
        print("\nAt least one position could not be compared, so this is not a "
              "pass.")
        return 2
    print("Report per position, as above. Do not compress this to a single "
          "figure: the control exists to show where reproduction stops.")
    return 0 if reproduced == len(positions) else 1


if __name__ == "__main__":
    sys.exit(main())
