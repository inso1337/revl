"""The guarantee x tier block names programs and carries no totals (#1768).

The block is committed, so two branches that each move a different cell
must change different lines of it, or GitHub's plain 3-way merge conflicts
on a generated file and every open self-host branch needs a hand-resolved
regen. Two shapes used to break that:

  * a divergence line read "agrees on N of M reproducers", so any branch that
    fixed or added one reproducer of a code rewrote the same line;
  * the per-tier totals table summed every row, so any branch that moved any
    cell rewrote that tier's totals row.

Now a divergence names each reproducer the gate does not refuse, one per
line, and the totals are printed on demand by `--show`. The checks stay as
strict as before: the block is still regenerated and compared byte for byte
by `python3 tools/conformance.py --check-readme`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "tools"), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import tier_guarantees as tg  # noqa: E402


@pytest.fixture(scope="module")
def data():
    return tg.matrix()


def test_a_divergence_names_the_reproducers_it_does_not_refuse(data):
    divergent = [row for row in data["rows"]
                 if row["cells"][tg.SELFHOST_TIER]["verdict"] == tg.DIVERGENCE]
    assert divergent, "no revl divergence to measure; the test proves nothing"
    block = tg.markdown(data)
    for row in divergent:
        names = row["cells"][tg.SELFHOST_TIER]["names"]
        assert names == sorted(names)
        for name in names:
            path = name.split("`")[1]
            assert path in tg.reproducers()[row["code"]]
            assert f"  - {name}" in block.splitlines()


def test_the_block_carries_no_count_of_reproducers_or_cells(data):
    block = tg.markdown(data)
    assert not re.search(r"agrees on \d+ of \d+", block)
    assert "| tier | proved |" not in block
    for line in block.splitlines():
        # a table row of bare numbers is a totals row
        cells = [c.strip() for c in line.strip("|").split("|")]
        assert not (line.startswith("|") and len(cells) > 1
                    and all(c.isdigit() for c in cells[1:])), line


def test_show_prints_the_per_tier_totals(data):
    shown = tg.totals(data)
    lines = shown.splitlines()
    assert lines[0] == "| tier | proved | div | no repro | unimpl |"
    assert [ln.split(" | ")[0].strip("| ") for ln in lines[2:]] == data["tiers"]
    for ln, tier in zip(lines[2:], data["tiers"]):
        counts = [int(c) for c in ln.strip("|").split("|")[1:]]
        assert sum(counts) == len(data["rows"]), tier


def test_show_is_a_command_line_flag():
    out = subprocess.run([sys.executable, str(ROOT / "tools" / "tier_guarantees.py"),
                          "--show"], capture_output=True, text=True, check=True,
                         cwd=ROOT).stdout
    assert out.startswith("| tier | proved | div | no repro | unimpl |")


def test_moving_one_reproducer_touches_only_its_own_line(data, monkeypatch):
    """The point of naming: the gate changing its verdict on one reproducer
    adds or removes that reproducer's line and nothing else in the block."""
    row = next((r for r in data["rows"]
                if r["cells"][tg.SELFHOST_TIER]["verdict"] == tg.DIVERGENCE
                and len(r["cells"][tg.SELFHOST_TIER]["names"]) >= 1), None)
    if row is None:
        pytest.skip("no revl divergence in the tree")
    code = row["code"]
    index = tg.reproducers()
    agreed = [p for p in index[code]
              if not any(f"`{p}`" in n for n in row["cells"][tg.SELFHOST_TIER]["names"])]
    if not agreed:
        pytest.skip("every reproducer diverges")
    # add a second divergent reproducer by making the gate admit one it refused
    victim = agreed[0]
    victim_text = (ROOT / victim).read_text(encoding="utf-8")
    real = tg._selfhost_admit()
    monkeypatch.setattr(tg, "_selfhost_admit",
                        lambda: lambda src: "" if src == victim_text else real(src))
    before = tg.markdown(data).splitlines()
    after = tg.markdown(tg.matrix()).splitlines()
    added = [ln for ln in after if ln not in before]
    removed = [ln for ln in before if ln not in after]
    assert added == [f"  - `{victim}`: admitted"], added
    assert removed == []
