"""`tools/docgen.py --check`, run somewhere a documentation-only diff reaches.

The gate that judges documents lived in exactly one place, the `frontend` job,
and `frontend` is skipped for a documentation-only pull request. So on the one
diff that moves a document, the document gate did not run. Issue #1358 then
showed the other half: `frontend` ran `docgen.py --check` after four other
gates, the first of which had been failing, so the step reported `skipped` on
every build of `main` as well (measured on run 35636928793, sha 421695acb).
Between the two holes the drift grew from 3 findings to 9 with nothing saying
so.

`tests/test_check_vision_claims.py` (item 534) and
`tests/test_selfhost_residual_is_generated.py` (issue #1300) each closed this
hole for one block, by putting that block's check in the root suite and adding
the module to `tools/affected_tests.py`'s documentation rule, which the ungated
`root-suite-affected` job consumes. Two blocks out of the whole generator is a
sample, not a gate. This module runs what the CI step runs, all of
`docgen.BLOCKS` and all of `docgen.CHECKS`, and is wired into the same rule.

The overlap with those two modules is deliberate. They hold their block against
a SYNTHETIC tree one perturbation at a time, which is what proves the rendering
rule; this holds the REAL tree against the real generator, which is what proves
the committed bytes.

## The baseline, and why it is an equality

The 9 findings the gate was not reporting are real and they are not this
module's to fix: they belong to issue #1342, and #1358 says so explicitly. They
are enumerated below rather than tolerated by a predicate, and the assertion is
`==`, not `<=`. That direction matters in both senses:

  * a NEW finding is a red here, which is what would have caught 3 growing to
    9;
  * a finding that is FIXED is also a red here, which is what makes the
    baseline shrink instead of outliving its reason. When #1342 lands, delete
    the entries it fixed. When the lists are empty, delete them and assert the
    plain empty result.

The line number is stripped from a finding before comparison: an unrelated edit
above it in the same document must not read as a new finding.

Nothing here writes. `run_blocks(write=False)` compares and reports.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import docgen  # noqa: E402

# Issue #1342 owns these. `docs/selfhost-compile.md` and
# `docs/selfhost-findings.md` carry a residual the ledger disagrees with, in a
# generated block and in prose beside it. The fix is
# `python3 tools/docgen.py --write` for the blocks and prose edits for the
# figures, in that issue's branch, not here.
KNOWN_STALE_BLOCKS: set[str] = set()

#: Emptied 2026-09-22. PR #1330 regenerated the selfhost-residual blocks and
#: PR #1375 cleared the residual-claims coverage failures, so every entry that
#: used to live here is fixed. The ratchet is SHRINK-ONLY in both directions:
#: a new finding reds, and a baselined finding that no longer fires reds too,
#: which is what happened here. That second direction is the point - a baseline
#: that silently keeps stale entries rots into a permitted-failure list, which
#: is the opposite of a ratchet.
KNOWN_COVERAGE_FAILURES: set[tuple[str, str, str]] = set()

_LOCATION = re.compile(r"^(?P<doc>[^\s:]+):(?P<line>\d+): says (?P<claim>.*?)\. ")


def _identity(key: str, failure: str) -> tuple[str, str, str]:
    """(check, document, claim), with the line number dropped.

    A coverage failure reports a line, and a line moves whenever anything above
    it in the document is edited. Comparing the located string would turn every
    unrelated documentation edit into a new finding, which is the kind of noise
    that gets a baseline widened instead of emptied.
    """
    match = _LOCATION.match(failure)
    if match is None:
        return (key, "", failure)
    return (key, match.group("doc"), match.group("claim"))


def test_every_generated_block_is_current():
    """The byte comparison half of `docgen.py --check`. A stale block is very
    often inherited: the blocks are a pure function of their sources, so any
    landing that touches a source re-stales them for every open branch at once.
    `python3 tools/docgen.py --write` (or `make docs-gen`) is the fix, and it is
    the whole fix. Never edit a generated block by hand."""
    stale = set(docgen.run_blocks(write=False))
    new = stale - KNOWN_STALE_BLOCKS
    assert not new, (
        f"{len(new)} generated block(s) went stale and are not in the #1342 "
        "baseline:\n"
        + "\n".join(f"  {line}" for line in sorted(new))
        + f"\n  fix: {docgen.WRITE_HINT}\n"
        "  Check main before assuming this is yours: a merge that touched a "
        "source re-stales these for every open branch."
    )
    fixed = KNOWN_STALE_BLOCKS - stale
    assert not fixed, (
        f"{len(fixed)} baselined block(s) are current again:\n"
        + "\n".join(f"  {line}" for line in sorted(fixed))
        + "\n  Delete them from KNOWN_STALE_BLOCKS. A baseline that outlives "
        "what it excuses is how the last one reached 9 findings."
    )


def test_every_documentation_coverage_check_passes():
    """The set-comparison half. These do NOT have a regeneration: a subcommand
    or an MCP verb exists in the code with nothing describing it, or a document
    states a derived figure its source disagrees with. The fix is prose, or
    taking the figure from the block that generates it. Never narrowing the
    check."""
    found = {_identity(key, f) for key, _, _, fn in docgen.CHECKS for f in fn()}
    new = found - KNOWN_COVERAGE_FAILURES
    assert not new, (
        f"{len(new)} documentation coverage failure(s) not in the #1342 "
        "baseline:\n"
        + "\n".join(f"  [{key}] {doc}: {claim}" for key, doc, claim in sorted(new))
        + "\n  fix: write the missing section or row, or take the figure from "
        "the block that generates it. Never delete the check."
    )
    fixed = KNOWN_COVERAGE_FAILURES - found
    assert not fixed, (
        f"{len(fixed)} baselined coverage failure(s) no longer fail:\n"
        + "\n".join(f"  [{key}] {doc}: {claim}" for key, doc, claim in sorted(fixed))
        + "\n  Delete them from KNOWN_COVERAGE_FAILURES."
    )


def test_the_baseline_is_the_whole_of_what_is_wrong_today():
    """What the gate was not saying, stated once as a number so it is readable
    without running anything. 3 stale blocks and 6 coverage failures is the 9
    findings #1358 counts, and issue #1342 owns all nine."""
    assert len(KNOWN_STALE_BLOCKS) == 0
    assert len(KNOWN_COVERAGE_FAILURES) == 0, (
        "the baseline is empty as of 2026-09-22: PR #1330 regenerated the "
        "selfhost-residual blocks and PR #1375 cleared the residual-claims "
        "failures. An entry here again means a NEW finding nobody has owned, "
        "and it needs an owner named beside it, not a silent addition."
    )


def test_this_module_covers_the_whole_generator():
    """The reason to have this module rather than one more per-block sample.
    A block or a check added to `tools/docgen.py` is covered here the day it is
    added, with no list to remember to edit. Stated as an assertion so that
    stays true if the drivers are ever refactored into something narrower."""
    # A block key identifies a marker pair, and the same marker can be spliced
    # into more than one document (`selfhost-residual` is in two), so the
    # identity of a row is (key, document).
    blocks = {(key, rel) for key, rel, _, _ in docgen.BLOCKS}
    checks = {(key, rel) for key, rel, _, _ in docgen.CHECKS}
    assert len(blocks) == len(docgen.BLOCKS), f"duplicate (key, document) in BLOCKS: {docgen.BLOCKS}"
    assert len(checks) == len(docgen.CHECKS), f"duplicate (key, document) in CHECKS: {docgen.CHECKS}"
    assert blocks and checks
    # `run_blocks` reads BLOCKS directly and the test above reads CHECKS
    # directly, so coverage is by construction. What can still drift is a
    # driver that stops reading the table, which is what this catches.
    rendered = {(key, rel) for key, rel, _, render in docgen.BLOCKS if callable(render)}
    assert rendered == blocks, f"a block with no renderer: {sorted(blocks - rendered)}"
    runnable = {(key, rel) for key, rel, _, fn in docgen.CHECKS if callable(fn)}
    assert runnable == checks, f"a check with no function: {sorted(checks - runnable)}"


def test_the_coverage_baseline_is_seen_to_fire():
    """A gate nobody has watched fail is what issue #1358 is about, so watch
    this one. A planted finding that is not in the baseline is new, and a
    baseline entry whose line number moved is not."""
    planted = _identity("residual-claims", "docs/new-doc.md:12: says `7 things`, but there are 8. rest")
    assert planted not in KNOWN_COVERAGE_FAILURES
    assert planted == ("residual-claims", "docs/new-doc.md", "`7 things`, but there are 8")

    # The line-number strip is what keeps an ordinary documentation edit from
    # reading as a new finding. It is asserted directly rather than against a
    # live baseline entry, because the baseline is empty and a test that needs
    # a real failure to prove itself stops working the day the failure is fixed
    # - which is exactly what happened here on 2026-09-22.
    same_claim_two_lines = {
        _identity(
            "residual-claims",
            f"docs/selfhost-compile.md:{line}: says `41 residual documents`, "
            "but the residual is 43. the residual is generated: state it "
            "inside the block.",
        )
        for line in (12, 9999)
    }
    assert len(same_claim_two_lines) == 1, (
        "the line-number strip stopped working, so every documentation edit "
        f"now reads as a new finding: {same_claim_two_lines}"
    )
    assert same_claim_two_lines == {(
        "residual-claims",
        "docs/selfhost-compile.md",
        "`41 residual documents`, but the residual is 43",
    )}

    unparsed = _identity("verbs-documented", "docs/mcp-reference.md has no section for `revl.plan`")
    assert unparsed not in KNOWN_COVERAGE_FAILURES
    assert unparsed[1] == "", "a failure with no location must stay whole, not lose its text"
