"""Corpus coverage + the witnessed-fraction measurement (roadmap item 252).

`tools/shell_witnessed_corpus.py` classifies a representative agent shell corpus
and reports the fraction lowered to the witnessed catalog. This suite guards it:

  * every labelled command classifies to its expected verdict (coverage);
  * the SAFETY-CRITICAL direction never regresses — no expected `emission` is
    ever classified `witnessed` (a false "fs-local" on an irreversible command);
  * the reported witnessed fraction is a real, non-trivial number (the corpus
    actually exercises the lowering, not an all-emission degenerate case).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(_ROOT / "tools"))

import shell_witnessed_corpus as corpus  # noqa: E402


def test_every_corpus_label_matches():
    report = corpus.measure()
    assert report["mismatches"] == [], report["mismatches"]


def test_no_dangerous_false_witnessed():
    # the one that must NEVER happen: an irreversible command classified as a
    # reversible, auto-approved witnessed lowering.
    report = corpus.measure()
    assert report["dangerous_false_witnessed"] == []


def test_witnessed_fraction_is_reported_and_nontrivial():
    report = corpus.measure()
    # the corpus genuinely exercises lowering — a meaningful minority is
    # reclaimed (the realistic fs share of a session), not zero and not all.
    assert 0.0 < report["witnessed_fraction"] < 1.0
    assert report["witnessed"] == report["fs_eligible"]
