"""Re-verify the witness each `merged_pr_landing_baseline.json` entry records.

WHY THIS EXISTS (issue #1434). The ancestry ratchet in
`tools/check_merged_prs_landed.py` checks that a baselined merge commit is
still unreachable from `main`. That is the one property of a stranded pull
request that can never change, so the check stayed green while the sentence
beside each entry rotted: five of eight notes were found stale by hand, and
one (#1320) named the wrong files, counting a MODIFIED file as ADDED and
leaving the real fifth added file out. A check that verifies the immutable
half of a claim and trusts the mutable half is not a check.

So each entry now carries a structured WITNESS next to its prose, and this
module re-measures it on every run:

    merge      the merge commit GitHub recorded. Cross-checked against the PR
               list by the ancestry audit, so the witness cannot be measured
               against the wrong commit.
    verdict    CARRIED, REWORKED or STRANDED. The note's first verdict word
               must agree.
    added      the files the merge ADDED. Must EQUAL the merge's own added set,
               `git diff --diff-filter=A <merge>^1 <merge>`. This is the #1320
               defect: a modified file listed as added, or an added file left
               out, fails.
    carried_at CARRIED only: the commit that brought the work in. It must be
               an ancestor of the tree under test (HEAD), and the merge must
               not be an ancestor of it. Every byte comparison below is made
               AT THIS COMMIT, not at HEAD.
    identical  paths whose bytes at `carried_at` equal the merge's copy. A
               path absent there, or different there without the entry saying
               so, fails.
    drifted    {path: [pytest node ids]} for a path that was carried already
               MODIFIED: present at `carried_at`, not byte-identical to the
               merge's copy. Its witness is behavioural, and the claim is
               falsifiable three ways: every node id must COLLECT (or, with
               `--pinned run`, PASS; a skip is not a pass), must name a test
               file this merge touched, and must name a test that already
               existed in the merge's copy of that file. A whole-file node id is
               accepted only when that test file was itself byte-identical to
               the merge's copy at `carried_at`, so every test in it was the
               merge's own. A path listed as drifted that was actually
               identical fails too: the record has to be exact both ways.
    absent     STRANDED only: the added files, each of which must be ABSENT.
    pinned_by  REWORKED only: tests that carry the outcome under another name.
               They must collect (or pass), and nothing else is claimed.

WHAT IT CAN AND CANNOT CHECK, stated per entry in the output rather than
implied by a green line:

  * CARRIED is VERIFIED: every added file is accounted for as identical or as
    drifted-and-pinned at `carried_at`, and every path the note names that the
    merge touched is in the witness, so the prose cannot claim a path the
    checker skips.
  * STRANDED is VERIFIED when the merge added files: each must be absent from
    the tree under test. That claim is about the present, so it is the one
    byte-level check made against HEAD: if the files land, the entry is stale.
  * A carried file later REMOVED from the tree is reported, not failed. The
    work being reverted afterwards does not make "it was carried" false.
  * REWORKED is UNCHECKED. The outcome landed by a different mechanism under
    a different name, so there is no path to compare. What is checked is the
    inventory and that the named tests exist and pass; that they carry the
    SAME outcome as the merge is prose, and the report says so.

What no mode checks: that a drifted path's pinned tests actually exercise
that path. The pins are constrained to the merge's own tests, which stops
pinning a drift on an unrelated test, but relevance is still a judgement.

WHY `carried_at` AND NOT HEAD. "This work was carried to main" is a
historical fact. A later edit to a carried file means the code moved on, not
that the claim became false, and checking the bytes at HEAD would make every
ordinary edit to one of those files fail a ledger about somebody else's
merge. Every stale entry #1434 found was wrong on the day it was written, and
a check pinned to the commit the note describes still catches that. What
remains tied to HEAD: STRANDED's `absent` (a claim about now), the ancestry
of `carried_at`, and the pinned tests, which run in the working tree at
`--root` and so must still exist and pass.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, NamedTuple

VERDICTS = ("CARRIED", "REWORKED", "STRANDED")
FIELDS = ("merge", "verdict", "carried_at", "added", "identical", "drifted",
          "absent", "pinned_by", "note")
# Which optional fields each verdict may carry. A field a verdict does not
# use is refused rather than ignored: an ignored field is a claim nobody reads.
ALLOWED = {
    "CARRIED": {"carried_at", "identical", "drifted"},
    "REWORKED": {"pinned_by"},
    "STRANDED": {"absent"},
}
_SHA = re.compile(r"^[0-9a-f]{40}$")
_VERDICT_WORD = re.compile(r"\b(CARRIED|REWORKED|STRANDED)\b")
# A path-shaped token in a note: optional directories, a name, a known suffix.
_PATH_TOKEN = re.compile(
    r"(?<![\w/.-])((?:[\w-]+/)*[\w.-]*\w\.(?:py|rvl|md|json|rs|ts|go|toml|"
    r"ya?ml|txt|java|lean))(?![\w/-])")


class Entry(NamedTuple):
    num: str
    merge: str
    verdict: str
    carried_at: str
    added: tuple[str, ...]
    identical: tuple[str, ...]
    drifted: dict[str, tuple[str, ...]]
    absent: tuple[str, ...]
    pinned_by: tuple[str, ...]
    note: str

    def pins(self) -> tuple[str, ...]:
        """Every node id this entry names, once each, in a stable order."""
        seen: dict[str, None] = {}
        for ids in self.drifted.values():
            seen.update(dict.fromkeys(ids))
        seen.update(dict.fromkeys(self.pinned_by))
        return tuple(seen)


class Report(NamedTuple):
    """Four outcomes, kept apart for the same reason the ancestry audit keeps
    them apart: a finding, a checked claim, a claim that cannot be checked and
    a question that could not be asked want four different responses."""

    findings: list[str]
    verified: list[str]
    unchecked: list[str]
    unresolved: list[str]


# ---------------------------------------------------------------- parsing


def _str_list(raw, where: str, errors: list[str]) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        errors.append(f"{where} must be a list of strings")
        return ()
    return tuple(raw)


def _drifted_field(raw, where: str, errors: list[str]) -> dict[str, tuple[str, ...]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        errors.append(f"{where} must map each path to the tests that pin it")
        return {}
    out = {}
    for path, ids in raw.items():
        pins = _str_list(ids, f"{where}[{path!r}]", errors)
        if not pins:
            errors.append(
                f"{where}[{path!r}] names no test. A drift with nothing "
                f"pinning it is not a witness, it is an exemption")
        out[path] = pins
    return out


def parse_entry(num: str, raw) -> tuple[Entry | None, list[str]]:
    """One baseline entry, or the reasons it is not a well-formed one."""
    if not isinstance(raw, dict):
        return None, [f"#{num}: the entry is bare prose. It must be an object "
                      f"with a `merge`, a `verdict`, an `added` inventory and "
                      f"a witness, so the claim can be re-measured"]
    errors: list[str] = []
    unknown = sorted(set(raw) - set(FIELDS))
    if unknown:
        errors.append(f"unknown field(s) {unknown}")
    merge = raw.get("merge")
    if not isinstance(merge, str) or not _SHA.match(merge):
        errors.append("`merge` must be the full 40-character merge commit")
        merge = ""
    verdict = raw.get("verdict")
    if verdict not in VERDICTS:
        errors.append(f"`verdict` must be one of {', '.join(VERDICTS)}")
    note = raw.get("note")
    if not isinstance(note, str) or len(note) < 40:
        errors.append("`note` must say, in prose, what was measured")
        note = note if isinstance(note, str) else ""
    carried_at = raw.get("carried_at", "")
    if verdict == "CARRIED" and (not isinstance(carried_at, str)
                                 or not _SHA.match(carried_at)):
        errors.append("a CARRIED entry needs `carried_at`, the full sha of "
                      "the commit that brought the work in")
        carried_at = ""
    if "added" not in raw:
        errors.append("`added` is required, even when empty: the inventory is "
                      "checked for every verdict")
    entry = Entry(
        num=num, merge=merge, verdict=verdict or "", note=note,
        carried_at=carried_at if isinstance(carried_at, str) else "",
        added=_str_list(raw.get("added"), "`added`", errors),
        identical=_str_list(raw.get("identical"), "`identical`", errors),
        drifted=_drifted_field(raw.get("drifted"), "`drifted`", errors),
        absent=_str_list(raw.get("absent"), "`absent`", errors),
        pinned_by=_str_list(raw.get("pinned_by"), "`pinned_by`", errors))
    if verdict in ALLOWED:
        stray = sorted(f for f in ("carried_at", "identical", "drifted",
                                   "absent", "pinned_by")
                       if f in raw and f not in ALLOWED[verdict])
        if stray:
            errors.append(f"a {verdict} entry does not carry {stray}")
    return (None if errors else entry), [f"#{num}: {e}" for e in errors]


# ---------------------------------------------------------------- git


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args],
                          capture_output=True, check=False)


def merge_changes(root: Path, merge: str) -> dict[str, str]:
    """{path: status letter} for what `merge` changed against its first
    parent, which is the base branch it landed on. Renames are off so a moved
    file reads as the delete and the add it is."""
    proc = _git(root, "diff", "--name-status", "--no-renames", "-z",
                f"{merge}^1", merge)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace").strip())
    fields = proc.stdout.decode().split("\0")
    return {fields[i + 1]: fields[i][:1]
            for i in range(0, len(fields) - 1, 2) if fields[i]}


def blob_at(root: Path, commit: str, path: str) -> bytes | None:
    proc = _git(root, "cat-file", "blob", f"{commit}:{path}")
    return proc.stdout if proc.returncode == 0 else None


def is_ancestor(root: Path, older: str, newer: str) -> bool:
    return _git(root, "merge-base", "--is-ancestor", older,
                newer).returncode == 0


def on_disk(root: Path, path: str) -> bytes | None:
    target = root / path
    return target.read_bytes() if target.is_file() else None


# ---------------------------------------------------------------- pins


def split_nodeid(nodeid: str) -> tuple[str, list[str]]:
    """`tests/x.py::Cls::test_a[p]` -> (`tests/x.py`, [`Cls`, `test_a`])."""
    file, *parts = nodeid.split("::")
    if parts:
        parts[-1] = parts[-1].split("[", 1)[0]
    return file, parts


def defined_in(source: bytes, parts: list[str]) -> bool:
    """Is `parts` (a function, or a class then a method) defined in `source`?
    Read from the syntax tree, so a name in a comment or a string does not
    count."""
    try:
        body = ast.parse(source).body
    except SyntaxError:
        return False
    for i, name in enumerate(parts):
        want = ast.ClassDef if i < len(parts) - 1 else (
            ast.FunctionDef, ast.AsyncFunctionDef)
        node = next((n for n in body if isinstance(n, want)
                     and n.name == name), None)
        if node is None:
            return False
        body = node.body
    return True


def _matches(pin: str, nodeid: str) -> bool:
    return nodeid == pin or nodeid.startswith((pin + "::", pin + "["))


def collect(root: Path, pins: tuple[str, ...], python: str) -> dict[str, int]:
    """How many tests each node id collects in the tree under test. A node id
    whose file is missing collects nothing; it is not an error for the run."""
    files = sorted({split_nodeid(p)[0] for p in pins
                    if (root / split_nodeid(p)[0]).is_file()})
    collected: list[str] = []
    if files:
        proc = subprocess.run(
            [python, "-m", "pytest", "--collect-only", "-q",
             "-p", "no:cacheprovider", "--continue-on-collection-errors",
             *files],
            cwd=root, capture_output=True, text=True, check=False)
        collected = [ln.split()[0] for ln in proc.stdout.splitlines()
                     if re.match(r"^\S+\.py::\S", ln)]
    return {p: sum(_matches(p, c) for c in collected) for p in pins}


def _junit_nodeid(case: ET.Element) -> str:
    file = case.get("file", "")
    module = file[:-3].replace("/", ".")
    cls = case.get("classname", "")
    inner = cls[len(module) + 1:] if cls.startswith(module + ".") else ""
    prefix = file + ("::" + inner.replace(".", "::") if inner else "")
    return f"{prefix}::{case.get('name', '')}"


def _junit_outcome(case: ET.Element) -> str:
    for tag in ("failure", "error", "skipped"):
        if case.find(tag) is not None:
            return tag
    return "passed"


def run(root: Path, pins: tuple[str, ...], python: str
        ) -> dict[str, dict[str, int]]:
    """Run the named tests once and tally outcomes per node id."""
    present = [p for p in pins if (root / split_nodeid(p)[0]).is_file()]
    outcomes: list[tuple[str, str]] = []
    if present:
        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "pins.xml"
            subprocess.run(
                [python, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                 "-o", "junit_family=xunit1", f"--junitxml={xml}", *present],
                cwd=root, capture_output=True, text=True, check=False)
            if xml.is_file():
                outcomes = [(_junit_nodeid(c), _junit_outcome(c))
                            for c in ET.parse(xml).iter("testcase")]
    tally: dict[str, dict[str, int]] = {}
    for pin in pins:
        counts: dict[str, int] = {}
        for nodeid, outcome in outcomes:
            if _matches(pin, nodeid):
                counts[outcome] = counts.get(outcome, 0) + 1
        tally[pin] = counts
    return tally


class PinResults(NamedTuple):
    """What the tree under test says about every node id in the baseline."""

    mode: str
    counts: dict[str, dict[str, int]]

    def problem(self, pin: str) -> str | None:
        got = self.counts.get(pin, {})
        if self.mode == "collect":
            return None if got.get("collected") else "collects no test"
        total = sum(got.values())
        if not total:
            return "ran no test"
        if got.get("passed") != total:
            bad = ", ".join(f"{v} {k}" for k, v in sorted(got.items())
                            if k != "passed")
            return f"did not pass ({bad}; a skip is not a pass)"
        return None

    def summary(self, pins: tuple[str, ...]) -> str:
        if self.mode == "collect":
            n = sum(self.counts.get(p, {}).get("collected", 0) for p in pins)
            return f"{len(pins)} node id(s) collecting {n} test(s)"
        n = sum(self.counts.get(p, {}).get("passed", 0) for p in pins)
        return f"{len(pins)} node id(s), {n} test(s) passed"


def measure_pins(root: Path, pins: tuple[str, ...], mode: str,
                 python: str) -> PinResults:
    if mode == "collect":
        counts = {p: {"collected": n}
                  for p, n in collect(root, pins, python).items()}
    else:
        counts = run(root, pins, python)
    return PinResults(mode, counts)


# ---------------------------------------------------------------- checks


def check_inventory(e: Entry, changes: dict[str, str]) -> list[str]:
    """The recorded ADDED set must be the merge's added set, exactly."""
    actual = {p for p, s in changes.items() if s == "A"}
    listed = set(e.added)
    out = []
    for p in sorted(listed - actual):
        how = ("MODIFIED" if changes.get(p) == "M" else
               "not touched" if p not in changes else
               f"status {changes[p]}")
        out.append(f"`added` lists {p}, which this merge did not add ({how})")
    for p in sorted(actual - listed):
        out.append(f"this merge ADDED {p}, and `added` leaves it out")
    if len(e.added) != len(listed):
        out.append("`added` lists a path twice")
    return out


def _identical(e: Entry, root: Path, changes: dict[str, str]) -> list[str]:
    out = []
    for p in e.identical:
        if p not in changes:
            out.append(f"`identical` names {p}, which this merge did not "
                       f"touch, so there is no merge copy to compare")
            continue
        then = blob_at(root, e.carried_at, p)
        if then is None:
            out.append(f"CARRIED claims {p}, and it is ABSENT at carried_at "
                       f"{e.carried_at[:12]}")
        elif then != blob_at(root, e.merge, p):
            out.append(f"{p} at carried_at {e.carried_at[:12]} DIFFERS from "
                       f"{e.merge[:12]}'s copy and the entry does not say so. "
                       f"Move it to `drifted` with the merge's own tests that "
                       f"still pin it")
    return out


def _pin_origin(e: Entry, root: Path, changes: dict[str, str],
                pin: str) -> str | None:
    """A drift may only be pinned by the merge's OWN tests."""
    file, parts = split_nodeid(pin)
    if file not in changes:
        return (f"pins {pin}, but this merge did not touch {file}, so it is "
                f"not one of the merge's own tests")
    merged = blob_at(root, e.merge, file)
    if merged is None:
        return f"pins {pin}, and {file} is not in {e.merge[:12]}"
    if not parts:
        if blob_at(root, e.carried_at, file) != merged:
            return (f"pins the whole of {file}, which was not the merge's "
                    f"copy at carried_at {e.carried_at[:12]}. Name the "
                    f"merge's tests one by one")
        return None
    if not defined_in(merged, parts):
        return (f"pins {pin}, which does not exist in {e.merge[:12]}'s copy "
                f"of {file}, so it is not one of the merge's own tests")
    return None


def _drifted(e: Entry, root: Path, changes: dict[str, str],
             pins: PinResults) -> list[str]:
    out = []
    for p, ids in e.drifted.items():
        then = blob_at(root, e.carried_at, p)
        if p not in changes:
            out.append(f"`drifted` names {p}, which this merge did not touch")
        elif then is None:
            out.append(f"`drifted` names {p}, and it is ABSENT at carried_at "
                       f"{e.carried_at[:12]}. A drift is a change, not a "
                       f"removal")
        elif then == blob_at(root, e.merge, p):
            out.append(f"`drifted` names {p}, which at carried_at "
                       f"{e.carried_at[:12]} is byte-IDENTICAL to "
                       f"{e.merge[:12]}'s copy. Record it as `identical`")
        for pin in ids:
            why = _pin_origin(e, root, changes, pin) or (
                f"pins {pin}, which {pins.problem(pin)}"
                if pins.problem(pin) else None)
            if why:
                out.append(f"{p} {why}")
    return out


def _coverage(e: Entry) -> list[str]:
    covered = set(e.identical) | set(e.drifted)
    out = [f"CARRIED, but this merge ADDED {p} and the witness neither "
           f"byte-compares nor pins it"
           for p in e.added if p not in covered]
    if not covered:
        out.append("CARRIED with an empty witness: name a file that is "
                   "identical, or one that drifted and the tests pinning it")
    both = sorted(set(e.identical) & set(e.drifted))
    if both:
        out.append(f"{both} is listed as identical AND drifted")
    return out


def named_paths(note: str, changes: dict[str, str]) -> set[str]:
    """The merge-touched paths the prose names, by full path or by a bare
    file name that matches one."""
    found = set()
    for token in _PATH_TOKEN.findall(note):
        if token in changes:
            found.add(token)
        elif "/" not in token:
            found |= {p for p in changes if p.rsplit("/", 1)[-1] == token}
    return found


def check_prose(e: Entry, changes: dict[str, str]) -> list[str]:
    """Tie the note to the witness, so the prose cannot drift from it."""
    out = []
    first = _VERDICT_WORD.search(e.note)
    if first is None or first.group(1) != e.verdict:
        said = first.group(1) if first else "no verdict word"
        out.append(f"the note leads with {said}, the entry says {e.verdict}")
    if e.verdict == "REWORKED":
        return out
    witnessed = set(e.identical) | set(e.drifted) | set(e.absent)
    for p in sorted(named_paths(e.note, changes) - witnessed):
        out.append(f"the note names {p}, which this merge touched, but the "
                   f"witness does not check it")
    return out


def _carried_at(e: Entry, root: Path) -> list[str]:
    """The carrying commit has to be real history of the tree under test,
    and must not simply contain the stranded merge itself."""
    if not e.carried_at:
        return []
    if not is_ancestor(root, e.carried_at, "HEAD"):
        return [f"carried_at {e.carried_at[:12]} is not an ancestor of the "
                f"tree under test, so nothing was carried by it"]
    if is_ancestor(root, e.merge, e.carried_at):
        return [f"carried_at {e.carried_at[:12]} contains the merge "
                f"{e.merge[:12]} itself, so comparing the two proves nothing"]
    return []


def _stranded(e: Entry, root: Path) -> list[str]:
    out = []
    if set(e.absent) != set(e.added):
        out.append("STRANDED: `absent` must be exactly the merge's added set")
    for p in e.absent:
        if on_disk(root, p) is not None:
            same = on_disk(root, p) == blob_at(root, e.merge, p)
            out.append(f"STRANDED claims {p} is absent, and it is PRESENT"
                       f"{', byte-identical to the merge copy' if same else ''}"
                       f". The work landed; this entry is stale")
    return out


def _check_carried(e: Entry, root: Path, changes: dict[str, str],
                   pins: PinResults) -> tuple[list[str], str]:
    ancestry = _carried_at(e, root)
    if ancestry:
        # a comparison at a commit that is not history of this tree would
        # measure something, and it would not be this claim
        return ancestry, ""
    problems = (_identical(e, root, changes)
                + _drifted(e, root, changes, pins) + _coverage(e))
    drift_pins = tuple(dict.fromkeys(
        p for ids in e.drifted.values() for p in ids))
    what = (f"{len(e.added)} added file(s) accounted for; at carried_at "
            f"{e.carried_at[:12]}, {len(e.identical)} byte-identical to "
            f"{e.merge[:12]}")
    if e.drifted:
        what += (f", {len(e.drifted)} carried modified and pinned by "
                 f"{pins.summary(drift_pins)}")
    gone = [p for p in (*e.identical, *e.drifted) if on_disk(root, p) is None]
    if gone:
        what += (f". Since REMOVED from the tree (reported, not failed): "
                 f"{', '.join(gone)}")
    return problems, what


def check_entry(e: Entry, root: Path, changes: dict[str, str],
                pins: PinResults) -> tuple[list[str], str, bool]:
    """(problems, what was checked, whether the verdict itself was checked)."""
    problems = check_inventory(e, changes) + check_prose(e, changes)
    if e.verdict == "CARRIED":
        more, what = _check_carried(e, root, changes, pins)
        return problems + more, what, True
    if e.verdict == "STRANDED":
        problems += _stranded(e, root)
        return (problems, f"{len(e.absent)} added file(s) absent",
                bool(e.added))
    for pin in e.pinned_by:
        if pins.problem(pin):
            problems.append(f"names {pin}, which {pins.problem(pin)}")
    what = (f"no file witness, so the claim that the outcome landed elsewhere "
            f"is prose and was NOT verified. Checked only: the added set "
            f"({len(e.added)} file(s)) and {pins.summary(e.pinned_by)}")
    return problems, what, False


def audit(raw_entries: dict, root: Path, resolve: Callable[[str], bool],
          mode: str = "collect", python: str = sys.executable) -> Report:
    """Re-measure every entry's witness against the tree at `root`."""
    report = Report([], [], [], [])
    parsed: list[Entry] = []
    for num, raw in raw_entries.items():
        entry, errors = parse_entry(str(num), raw)
        report.findings.extend(errors)
        if entry is not None:
            parsed.append(entry)
    ready: list[tuple[Entry, dict[str, str]]] = []
    for e in parsed:
        missing = [c for c in (e.merge, e.carried_at) if c and not resolve(c)]
        if missing:
            report.unresolved.append(
                f"#{e.num}: {', '.join(c[:12] for c in missing)} could not "
                f"be resolved, so its witness was NOT checked")
            continue
        try:
            ready.append((e, merge_changes(root, e.merge)))
        except RuntimeError as exc:
            report.unresolved.append(f"#{e.num}: {exc}")
    every_pin = tuple(dict.fromkeys(p for e, _ in ready for p in e.pins()))
    pins = measure_pins(root, every_pin, mode, python)
    for e, changes in ready:
        problems, what, checked = check_entry(e, root, changes, pins)
        if problems:
            report.findings.extend(f"#{e.num} {e.verdict}: {p}"
                                   for p in problems)
        elif checked:
            report.verified.append(f"#{e.num} {e.verdict}: {what}")
        else:
            report.unchecked.append(f"#{e.num} {e.verdict}: {what}")
    return report
