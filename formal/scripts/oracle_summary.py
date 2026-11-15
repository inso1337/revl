#!/usr/bin/env python3
"""The oracle summary in `formal/STATUS.md`, generated from its own table.

The map above the summary carries one `Oracle` cell per row. The summary used
to restate those cells by hand, and by the time issue #2739 was filed it no
longer matched them: it named nine rows as having an oracle and four as having
none, while the table beside it said the four had one. A reader could not tell
which sentence to believe, and nothing compared them.

This script makes the table the only copy. It classifies every row's `Oracle`
cell (`yes`, `partial`, `no`), renders the summary from that classification,
and `--check` fails when the checked-in text is not what the table produces.
It reads no other file and imports nothing but the standard library, so it
runs before the elan guard on a machine with no toolchain (issue #2745's rule
for a summary a run must not be able to skip).

Usage: oracle_summary.py [--write] [-h]

`--write` rewrites the block between the markers in place; no argument prints
the block; `--check` is the gate.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

STATUS = Path(__file__).resolve().parents[1] / "STATUS.md"

BEGIN = ("<!-- BEGIN GENERATED oracle-summary: regenerate with "
         "`python3 formal/scripts/oracle_summary.py --write` -->")
END = "<!-- END GENERATED oracle-summary -->"

#: A table row, its cells split on unescaped pipes. Escaped pipes keep a
#: cell's `\|` intact, so a row whose Oracle cell spells one parses.
_CELL = re.compile(r"(?<!\\)\|")
_HEADER = re.compile(r"^\|\s*Code\s*\|\s*Formal status\s*\|", re.IGNORECASE)
_BOLD = re.compile(r"^\*\*(.+?)\*\*")

#: The three verdicts an `Oracle` cell can carry. A cell opens with the verdict
#: and then qualifies it. `**yes** (V rows, 2 agree-G2)` is a full yes: the
#: parenthesis only counts rows. `**yes** for the call-site half` is NOT, it
#: says the row decides a half of the rule, so it reads as partial (A6 is that
#: row). `**partial**` is partial and a bare `no` is no. Any other opening is
#: refused rather than guessed, so a new spelling is classified on purpose.
_PARTIAL = re.compile(r"^\*\*partial\*\*", re.IGNORECASE)
_YES_QUALIFIED = re.compile(r"^\*\*yes\*\*\s+for\b", re.IGNORECASE)
_YES = re.compile(r"^\*\*yes\*\*(\s*\(|\s*$)", re.IGNORECASE)
_NO = re.compile(r"^no\b", re.IGNORECASE)

#: Rows whose other side RUNS rather than reads: G7 drives the reference
#: runtime over an enumerated teardown corpus, A8 and R4 drive the recovery
#: code over an enumerated WAL corpus. They are named in the summary only when
#: the table counts them as reaching the oracle.
EXECUTING = ("G7", "A8", "R4")


def _cells(line: str) -> list[str]:
    """The cells of one table line, without the leading and trailing pipes."""
    parts = _CELL.split(line.strip())
    if parts and parts[0].strip() == "":
        parts = parts[1:]
    if parts and parts[-1].strip() == "":
        parts = parts[:-1]
    return [p.strip() for p in parts]


def _code(cell: str) -> str:
    """A row's name: its bold code when the first cell carries one, else the
    whole cell. `**G1** declared access` is `G1`; a plain row such as
    `items 66/294/260 capability ceilings` is itself."""
    m = _BOLD.match(cell)
    return m.group(1) if m else cell


def _oracle(cell: str) -> str:
    cell = cell.strip()
    if _PARTIAL.match(cell) or _YES_QUALIFIED.match(cell):
        return "partial"
    if _YES.match(cell):
        return "yes"
    if _NO.match(cell):
        return "no"
    raise ValueError(f"unreadable Oracle cell: {cell!r}")


def rows(text: str) -> list[tuple[str, str]]:
    """`(row name, oracle verdict)` for every row of the guarantee map."""
    out: list[tuple[str, str]] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not _HEADER.match(line):
            continue
        for row in lines[i + 1:]:
            if not row.lstrip().startswith("|"):
                break
            cells = _cells(row)
            if len(cells) < 4 or set(cells[0]) <= set("-: "):
                continue
            out.append((_code(cells[0]), _oracle(cells[3])))
        return out
    raise ValueError("formal/STATUS.md carries no guarantee map header")


def _joined(codes: list[str]) -> str:
    if len(codes) == 1:
        return codes[0]
    return ", ".join(codes[:-1]) + " and " + codes[-1]


def block(text: str) -> str:
    """The summary block the table produces, markers included."""
    rows_ = rows(text)
    if not rows_:
        raise ValueError("the guarantee map has no rows")
    yes = [code for code, verdict in rows_ if verdict == "yes"]
    partial = [code for code, verdict in rows_ if verdict == "partial"]
    no = [code for code, verdict in rows_ if verdict == "no"]
    if len(yes) + len(partial) + len(no) != len(rows_):
        raise ValueError("a row's Oracle cell did not classify")
    body = [
        f"**The oracle reaches {len(yes)} of the {len(rows_)} rows in the table "
        f"above in full: {_joined(yes) if yes else 'none'}.**",
        "",
    ]
    if partial:
        verb = "carries a" if len(partial) == 1 else "carry a"
        body += [
            f"{_joined(partial)} {verb} **partial** oracle row: the row's "
            "entry in the table says which half is decided on the corpus and "
            "which half is not.",
            "",
        ]
    if no:
        body += [
            f"The {len(no)} row{'s' if len(no) != 1 else ''} with no oracle row "
            f"{'are' if len(no) != 1 else 'is'} {_joined(no)}; each row's "
            "entry in the table says what it is checked against instead.",
            "",
        ]
    executing = [code for code in EXECUTING if code in yes]
    other_side = (
        "An oracle row means `harness/diff_corpus.py` compares the model's "
        "verdict with the shipped implementation's over the corpus. For most "
        "rows the other side is the checker, through its verdicts or the "
        "algebra it calls")
    if executing:
        other_side += (
            f"; for {_joined(executing)} it is the runtime or the recovery "
            "code, run over an enumerated scenario corpus rather than read "
            "off the corpus documents")
    body += [
        other_side + ". A row without one is checked against the design "
        "documents and the reference source read by hand, which is weaker, "
        "and says so in its cell rather than leaving a reader to infer it.",
    ]
    inner = "\n".join(body)
    return f"{BEGIN}\n{inner}\n{END}"


def replace(text: str, generated: str) -> str:
    """`text` with its oracle-summary block replaced by `generated`."""
    start = text.find(BEGIN)
    stop = text.find(END)
    if start < 0 or stop < 0 or stop < start:
        raise ValueError("formal/STATUS.md carries no oracle-summary markers")
    return text[:start] + generated + text[stop + len(END):]


def check(text: str) -> str | None:
    """None when the checked-in block is the table's, else the reason."""
    try:
        want = block(text)
        got = replace(text, want)
    except ValueError as e:
        return str(e)
    if got == text:
        return None
    return ("the oracle summary is not the one the table produces; regenerate "
            "with `python3 formal/scripts/oracle_summary.py --write`")


def main(argv: list[str]) -> int:
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    text = STATUS.read_text(encoding="utf-8")
    if "--write" in argv:
        STATUS.write_text(replace(text, block(text)), encoding="utf-8")
        return 0
    if "--check" in argv:
        problem = check(text)
        if problem:
            print(f"oracle summary: {problem}")
            return 1
        print("oracle summary: the summary block agrees with the table")
        return 0
    print(block(text))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
