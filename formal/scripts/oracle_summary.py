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

#: The three verdicts an `Oracle` cell can carry, most specific first. The
#: cells open with the verdict and then qualify it, e.g. "**yes** (V rows, 2
#: agree-G2)" or "**yes** for the call-site half"; "no" is the bare word.
_ORACLE = (("partial", re.compile(r"^\**\s*partial\b", re.IGNORECASE)),
           ("yes", re.compile(r"^\**\s*yes\b", re.IGNORECASE)),
           ("no", re.compile(r"^\**\s*no\b", re.IGNORECASE)))


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
    for verdict, pattern in _ORACLE:
        if pattern.match(cell):
            return verdict
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
        f"above: {_joined(yes) if yes else 'none'}.**",
        "",
    ]
    if partial:
        body += [
            f"{_joined(partial)} carries a **partial** oracle row: the cell in "
            "the table says which half is decided on the corpus and which half "
            "is not.",
            "",
        ]
    if no:
        body += [
            f"The {len(no)} row{'s' if len(no) != 1 else ''} with no oracle row "
            f"{'are' if len(no) != 1 else 'is'} {_joined(no)}; each Oracle cell "
            "says what that row is checked against instead.",
            "",
        ]
    body += [
        "An oracle row means the row's verdict is compared against the shipped "
        "checker over the corpus by `harness/diff_corpus.py`; it relates the "
        "model to the checker on the corpus and says nothing about lowering. A "
        "row without one is checked against the design documents and the "
        "reference source read by hand, which is weaker, and says so in its "
        "cell rather than leaving a reader to infer it.",
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
