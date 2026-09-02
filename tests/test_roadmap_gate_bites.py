"""Every check in tools/check_roadmap_markers.py, seen to fail and seen to pass.

Item 418's review on 2026-09-02 found four Lean theorems that were true for the
wrong reason and two that were CONTENTLESS, and the discipline it left behind
was: a gate nobody has watched fail is not known to work. These four checks
read prose with regular expressions, which is the easiest kind of check to
write so that it can never fire.

So each check below gets a pair. One fixture is the real failure, reduced to
its shape and taken from the actual roadmap text that fell through on
2026-09-02. The other is the same fixture with the one thing changed that
should silence it, which pins the ESCAPE HATCH as well: what a writer has to
do to satisfy the gate honestly, rather than by deleting the sentence.

These fixtures are synthetic. They touch no git and no network.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import check_roadmap_markers as gate  # noqa: E402

SECTION = "## Open, in rough priority order\n\n"

# orphan_findings needs the same three arguments the branch scanner uses. No
# tree directories, one namespace, no live remote heads: a branch is then
# recognised by its spelling alone, which is what a unit test can pin.
DIRS: set[str] = set()
NAMESPACES = {"fix", "agent"}
HEADS: set[str] = set()
BACKENDS = {"python", "typescript", "go", "java", "rust", "wasm"}


def _labels(findings: list[str]) -> list[str]:
    return [f.splitlines()[0] for f in findings]


# --------------------------------------------------------------------------
# (A) SELF-CONTRADICTION, shape one: an item header that claims closure over
# finding blocks whose own heads say open. This is item 422 as it read before
# it was reworded: "ALL SEVEN FINDINGS FIXED" above four findings that were
# untouched on the typescript tier, one of them a CRITICAL.
# --------------------------------------------------------------------------
_A_HEADER_BITES = SECTION + """422. ✅ **ALL SEVEN FINDINGS FIXED (2026-09-02).** The workspace jail's own
docstring calls `resolve_within` the single choke point. It is not.

**F1 CRITICAL, ❌ STILL OPEN.** The ts tier carries the pre-fix shape.

**F5 LOW, ✅ FIXED** (`1602cc94`).
"""

# The one change that should silence it: say in the header what is true. This
# is the wording item 422 actually carries on main today.
_A_HEADER_PASSES = _A_HEADER_BITES.replace(
    "✅ **ALL SEVEN FINDINGS FIXED (2026-09-02).**",
    "✅ **ALL SEVEN FINDINGS FIXED ON THE PY TIER, AND F1 IS STILL OPEN ON "
    "THE TS TIER.**",
)


def test_a_header_claim_over_open_findings_bites():
    found = gate.contradiction_findings(_A_HEADER_BITES)
    assert len(found) == 1, _labels(found)
    assert "item 422's header claims closure" in found[0]
    assert "F1" in found[0]


def test_a_header_claim_passes_once_the_header_qualifies_itself():
    assert gate.contradiction_findings(_A_HEADER_PASSES) == []


# --------------------------------------------------------------------------
# (A) shape two: a finding whose own head claims closure and whose own body
# says otherwise.
# --------------------------------------------------------------------------
_A_BODY_BITES = SECTION + """421. **CAPABILITY AUDIT (2026-09-02).** Findings below.

**F2 HIGH, ✅ FIXED 2026-09-02** (`4e818869`). The fold now keys on the
declared token. The same hole is still open on the ts tier.
"""

_A_BODY_PASSES = _A_BODY_BITES.replace(
    "**F2 HIGH, ✅ FIXED 2026-09-02**",
    "**F2 HIGH, ◑ FIXED ON THE PY TIER, STILL OPEN ON THE TS TIER**",
)


def test_a_finding_body_contradicting_its_own_head_bites():
    found = gate.contradiction_findings(_A_BODY_BITES)
    assert len(found) == 1, _labels(found)
    assert "item 421 F2 claims closure" in found[0]


def test_a_finding_passes_once_its_head_admits_the_residual():
    assert gate.contradiction_findings(_A_BODY_PASSES) == []


# --------------------------------------------------------------------------
# (A) the false positive this check must NOT have: a correctly closed finding
# quoting its original text in past tense. Every closed finding in the roadmap
# does this, so getting it wrong makes the check unusable.
# --------------------------------------------------------------------------
_A_RETROSPECTIVE = SECTION + """421. **CAPABILITY AUDIT (2026-09-02).** Findings below.

**F1 CRITICAL, ✅ FIXED 2026-09-02** (`4e818869`). The fold now keys on the
declared token. Original finding: the key spelling was still open on every
tier and the guard was not fixed by the 416 pass.
"""


def test_a_does_not_fire_on_a_past_tense_restatement():
    assert gate.contradiction_findings(_A_RETROSPECTIVE) == []


# The other three false positives this check produced while it was being
# built, each measured against the real file and each fixed by a bound rather
# than by loosening the check. They are pinned here so the bounds cannot be
# quietly removed.
def test_a_does_not_read_not_fixed_by_x_as_a_contradiction():
    """Item 422 F7: "**NOT fixed by refusing**" says HOW, not that it is open."""
    text = SECTION + """422. **FS CONFINEMENT AUDIT.** Findings below.

**F7 INFO. ✅ FIXED** (`src/revl/compiler.py`). The search path is not
identity-pinned. **NOT fixed by refusing**: a local file winning outright is
item 319's design, so refusal would break supported behaviour.
"""
    assert gate.contradiction_findings(text) == []


def test_a_does_not_read_a_lower_case_topic_name_as_an_open_status():
    """Item 104's block (c) is headed "(c) **partial import**", a subject."""
    text = SECTION + """104. ✅ **`revl import cordis` cannot see DSH's plugin shapes.** Landed.

(c) **partial import** is what the importer produces for a decorated method.
"""
    assert gate.contradiction_findings(text) == []


def test_a_does_not_read_a_bold_panel_title_as_a_closure_claim():
    """Item 434's header is "PARTIALLY LANDED ...", with a **WHAT LANDED** panel."""
    text = SECTION + """434. PARTIALLY LANDED 2026-09-02 on `fix/434-go-codegen-perf`: (h), (f) and
(e) are done; (d) and (g) are NOT, see the **WHAT LANDED** panel.

(d) **❌ NOT DONE, AS THE AUDIT ORDERED.** Text.
"""
    assert gate.contradiction_findings(text) == []


# --------------------------------------------------------------------------
# (B) DANGLING DELEGATION. Item 425 F3 read "folded into the item-416c fix"
# while 416 was closed, so the residual was tracked nowhere and owned by
# nobody. Item 427 F5 carried the identical sentence.
# --------------------------------------------------------------------------
_B_BITES = SECTION + """416. ✅ **MCP ARGUMENT LEAK AUDIT.** Landed 2026-09-01.

425. **MCP SERVER AUDIT (2026-09-02).** Findings below.

**F3 MEDIUM, ✅ FIXED 2026-09-02**, folded into the item-416c fix.
"""

# Silenced by the target being live AND owned, not by deleting the sentence.
# Live alone is not enough: an open target with no owner of its own is where
# item 425 F3 and item 427 F5 both point today.
_B_TARGET_OPEN = _B_BITES.replace(
    "416. ✅ **MCP ARGUMENT LEAK AUDIT.** Landed 2026-09-01.",
    "416. **MCP ARGUMENT LEAK AUDIT.** Being fixed on `fix/416c-arg-redaction`.")

_B_TARGET_OPEN_UNOWNED = _B_BITES.replace(
    "416. ✅ **MCP ARGUMENT LEAK AUDIT.** Landed 2026-09-01.",
    "416. **MCP ARGUMENT LEAK AUDIT.** (c) **HYPOTHESES, unexecuted.** Text.")

_B_TARGET_MISSING = _B_BITES.replace("the item-416c fix", "the item-999 fix")


def test_b_delegation_to_a_closed_target_bites():
    found = gate.delegation_findings(_B_BITES, DIRS, NAMESPACES, HEADS)
    assert len(found) == 1, _labels(found)
    assert "delegates its closure to item 416c" in found[0]
    assert "orphan by construction" in found[0]


def test_b_delegation_to_a_live_target_passes():
    assert gate.delegation_findings(_B_TARGET_OPEN, DIRS, NAMESPACES, HEADS) == []


def test_b_delegation_to_an_open_but_unowned_target_bites():
    """The item 425 F3 case as it stands on main: 416c is live and unowned."""
    found = gate.delegation_findings(_B_TARGET_OPEN_UNOWNED, DIRS, NAMESPACES,
                                     HEADS)
    assert len(found) == 1, _labels(found)
    assert "itself OPEN with no branch" in found[0]


def test_b_delegation_to_a_target_that_does_not_exist_bites():
    found = gate.delegation_findings(_B_TARGET_MISSING, DIRS, NAMESPACES, HEADS)
    assert len(found) == 1, _labels(found)
    assert "not a top-level item" in found[0]


def test_b_does_not_read_uncovered_by_as_a_delegation():
    """"a real defect UNCOVERED BY item 298" is the opposite of delegating."""
    text = _B_BITES.replace("folded into the item-416c fix",
                            "a real defect uncovered by item 416")
    assert gate.delegation_findings(text, DIRS, NAMESPACES, HEADS) == []


# --------------------------------------------------------------------------
# (C) ORPHAN. An open finding naming nothing a reader could follow to a live
# owner. The re-verification sha is the interesting case: it says the finding
# is still live, which is the opposite of saying somebody is closing it.
# --------------------------------------------------------------------------
_C_BITES = SECTION + """428. **SUPPLY-CHAIN AUDIT (2026-09-02).** Findings below.

**F7 MED, ❌ STILL OPEN, RE-VERIFIED IN SOURCE ON `bd0f4d19`.** The relabelling
bypass stands.
"""

_C_PASSES = _C_BITES.replace(
    "The relabelling\nbypass stands.",
    "The relabelling bypass stands. Being fixed on `fix/428-f7-ceiling`.")


def test_c_open_finding_with_no_owner_bites():
    found = gate.orphan_findings(_C_BITES, DIRS, NAMESPACES, HEADS)
    assert len(found) == 1, _labels(found)
    assert "item 428 F7 is open" in found[0]
    assert "re-verified on" in found[0]


def test_c_passes_once_the_finding_names_a_branch():
    assert gate.orphan_findings(_C_PASSES, DIRS, NAMESPACES, HEADS) == []


def test_c_leaves_lettered_sub_parts_alone():
    """(a)/(b)/(c) are sub-parts of one item, not independently owned findings."""
    text = SECTION + "74. **A WORK ITEM.** Body.\n\n(a) **A sub-part.** Text.\n"
    assert gate.orphan_findings(text, DIRS, NAMESPACES, HEADS) == []


# --------------------------------------------------------------------------
# (D) CROSS-TIER PARITY. Item 421 F6's secret marking landed on the python
# tier and nowhere else: `secret` appears 38 times in backends/python/emit.py
# and zero times in the ts, go, java, rust and wasm emitters.
# --------------------------------------------------------------------------
_D_BITES = SECTION + """421. **CAPABILITY AUDIT (2026-09-02).** Findings below.

**F6 HIGH, ✅ FIXED 2026-09-02** (`fix/421-f5f6-seam-value-leaks`). The scrub is
placed at the one choke point in `backends/python/emit.py`, so a declared
secret marking redacts the taint before the capability boundary.
"""

# The escape hatch, and the discipline: name the other tier.
_D_PASSES = _D_BITES.replace(
    "before the capability boundary.",
    "before the capability boundary. The ts tier has no marking pass at all "
    "and is out of scope here; it is filed separately.")


def test_d_single_tier_fix_for_a_language_wide_guarantee_bites():
    found = gate.tier_parity_findings(_D_BITES, BACKENDS)
    assert len(found) == 1, _labels(found)
    assert "cites only backends/python/" in found[0]
    assert "cannot decide parity" in found[0]


def test_d_passes_once_the_finding_names_the_other_tier():
    assert gate.tier_parity_findings(_D_PASSES, BACKENDS) == []


def test_d_ignores_a_single_tier_finding_that_is_not_about_a_guarantee():
    """A codegen performance finding is about one emitter by construction."""
    text = SECTION + """436. **PYTHON CODEGEN PERF AUDIT.** Findings below.

**F1 HIGHEST, ✅ FIXED 2026-09-02**. `backends/python/emit.py` turns every
accumulation loop quadratic; the emitted list concat is now in place.
"""
    assert gate.tier_parity_findings(text, BACKENDS) == []


def test_d_ignores_a_word_used_in_another_sense():
    """"escape" and "leak" were dropped from TIER_SUBJECTS for exactly this."""
    text = SECTION + """106. **RUST STRING ESCAPES.** Findings below.

**F1 LOW, ✅ FIXED 2026-09-02**. The `\\u{...}` escape in
`backends/rust/emit.py` collided with `format!()` placeholders and leaked a
brace.
"""
    assert gate.tier_parity_findings(text, BACKENDS) == []
