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
    call_log         compared as a sequence and included in the verdict

Reports each position separately and never one aggregate pass. A sweep with
positions present in only one file is INCOMPLETE (exit 2): a partial sweep
cannot answer a control defined over all K positions. A position with no
populated outcome field is refused rather than counted as reproduced.

Freshness is checked, not asserted: the same file twice or two names for one
byte-identical file are refused. The tool cannot prove the runs did not
overlap, and says so.

Usage:
  python3 bench/compare_sweep.py --orig ORIGINAL.jsonl --fresh FRESH.jsonl

Exit codes:
  0  every shared position reproduced
  1  at least one position did not reproduce (report per position)
  2  refused: unreadable or duplicate positions, no shared position, the same
     file or identical bytes, an identity field absent or different, or a
     position with nothing populated to compare
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compare_runs import (  # noqa: E402
    ADVISORY, CALL_SEQ, FIELD_SET_NAME, IDENTITY, OUTCOME, TOKENS,
    distinct_files, file_digest, is_empty, stamp_refusal)


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            recs = [json.loads(line) for line in fh if line.strip()]
    except (OSError, ValueError) as exc:
        sys.stderr.write("cannot read %s: %s\n" % (path, exc))
        sys.exit(2)
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
    a = ap.parse_args(argv)

    refusal = distinct_files([a.orig, a.fresh])
    if refusal:
        sys.stderr.write("refusing: %s\n" % refusal)
        return 2

    orig = load(a.orig)
    fresh = load(a.fresh)

    positions = sorted(set(orig) & set(fresh), key=str)
    all_records = ([orig[i] for i in positions]
                   + [fresh[i] for i in positions])
    labels = (["%s rep %r" % (Path(a.orig).name, i) for i in positions]
              + ["%s rep %r" % (Path(a.fresh).name, i) for i in positions])
    refusal = stamp_refusal(all_records, labels)
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

        seq_present = not (is_empty(ro.get(CALL_SEQ))
                           or is_empty(rf.get(CALL_SEQ)))
        seq_matched = seq_present and ro.get(CALL_SEQ) == rf.get(CALL_SEQ)
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
            print("  call sequence: not present in both records, so it is not "
                  "compared")
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
        print("Call sequence reproduced at %d of %d positions where both "
              "records carry it." % (sequence_reproduced, sequence_present))
    else:
        print("Call sequence: no position carries it in both records, so it is "
              "not compared.")
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
