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


# --------------------------------------------------------------------------
# (B), the disavowal bound. Once 425 F3 and 427 F5 were REWORDED to say the
# marker was wrong, the sentence still contained the phrase and the target,
# and (B) went on reporting the correction it had itself asked for. The bound
# is two-part on purpose: quoted AND disowned. Either half alone is still a
# delegation.
# --------------------------------------------------------------------------
_B_DISOWNED = SECTION + """416. ✅ **MCP ARGUMENT LEAK AUDIT.** Landed 2026-09-01.

425. **MCP SERVER AUDIT (2026-09-02).** Findings below.

**F3 MEDIUM, ✅ RESOLVED, AND THE MARKER `folded into the item-416c fix` WAS
WRONG.** The 416c fix redacts a dimension declared `Secret[T]`, which is the
half this finding says is NOT sufficient. Closed on `fix/425-mcp-tail`.
"""


def test_b_does_not_read_a_disowned_quoted_marker_as_a_delegation():
    """The roadmap correcting a bad delegation is not a delegation."""
    assert gate.delegation_findings(_B_DISOWNED, DIRS, NAMESPACES, HEADS) == []


def test_b_still_bites_a_quoted_marker_nobody_disowns():
    """Backticks alone are not the escape hatch: the prose has to disown it."""
    text = _B_DISOWNED.replace("` WAS\nWRONG.**", "`.** It is closed.**")
    found = gate.delegation_findings(text, DIRS, NAMESPACES, HEADS)
    assert len(found) == 1, _labels(found)
    assert "delegates its closure to item 416c" in found[0]


def test_b_still_bites_the_same_disavowal_without_the_quote():
    """And the disavowal alone is not it either: an UNQUOTED phrase still
    reads as the delegation being made."""
    text = _B_DISOWNED.replace("`folded into the item-416c fix`",
                               "folded into the item-416c fix")
    found = gate.delegation_findings(text, DIRS, NAMESPACES, HEADS)
    assert len(found) == 1, _labels(found)


# --------------------------------------------------------------------------
# (E) DUPLICATE ITEM HEADER. Three shapes, all three on the file on
# 2026-09-02: item 106's merge residue from `bd0f4d19`, items 100-103 carrying
# a done block and an in-progress block each, and the two different items that
# were both numbered 445. The carve-out is cross-section reuse, which is the
# documented convention: item numbers restart per section.
# --------------------------------------------------------------------------
_E_RESIDUE = SECTION + """106. \U0001f6a7 **The rust emitter's `\\u{...}` non-ASCII escape collides with
106. ✅ **The rust emitter's `\\u{...}` non-ASCII escape collides with
    `format!()` placeholders in assert/format strings.** Fixed on `38c84207`;
    the emitter now writes the escape without a brace.
"""

_E_CONFLICT = SECTION + """100. ✅ **`_tool_admit` compiles before honoring `replacing`.** Landed.

100. \U0001f6a7 **The `advance` lifecycle statement is py/ts-only; go and rust
    lifecycle emitters fail hard on it.** Being worked.
"""

_E_COLLISION = SECTION + """445. **THE UNIQUE-OWNERSHIP ANALYSIS IS WRITTEN TWICE.** Findings below.

445. **THE CROSS-TIER MATRIX NEVER RAN THE SLOW LANE.** Findings below.
"""


def test_e_merge_residue_bites():
    """Item 106 exactly: a truncated copy of the header with no body, kept by
    a conflict resolution, and the only reason a landed item read as open."""
    found = gate.duplicate_header_findings(_E_RESIDUE)
    assert len(found) == 1, _labels(found)
    assert "MERGE RESIDUE" in found[0]
    assert "Delete the residue line" in found[0]


def test_e_residue_passes_once_the_stale_line_is_gone():
    text = "\n".join(line for line in _E_RESIDUE.splitlines()
                     if not line.startswith("106. \U0001f6a7"))
    assert gate.duplicate_header_findings(text) == []


def test_e_conflicting_statuses_bite():
    found = gate.duplicate_header_findings(_E_CONFLICT)
    assert len(found) == 1, _labels(found)
    assert "CONFLICTING statuses" in found[0]


def test_e_conflict_passes_once_one_block_is_renumbered():
    text = _E_CONFLICT.replace("100. \U0001f6a7", "447. \U0001f6a7")
    assert gate.duplicate_header_findings(text) == []


def test_e_two_different_items_sharing_a_number_bite():
    """The 445 collision, which had no status disagreement to give it away."""
    found = gate.duplicate_header_findings(_E_COLLISION)
    assert len(found) == 1, _labels(found)
    assert "DIFFERENT items sharing one number" in found[0]


def test_e_leaves_the_same_number_in_another_section_alone():
    """Numbers restart per section by design: item 1 exists five times in the
    real file. Reporting that would bury the three shapes that matter."""
    text = ("## Done (dependency order as built)\n\n"
            "106. ✅ **Lexer.** Multi-char operators.\n\n"
            + _E_RESIDUE.replace("106. \U0001f6a7 **The rust emitter's "
                                 "`\\u{...}` non-ASCII escape collides with\n",
                                 ""))
    assert gate.duplicate_header_findings(text) == []


def test_e_leaves_a_number_with_one_block_alone():
    assert gate.duplicate_header_findings(
        SECTION + "428. **SUPPLY-CHAIN AUDIT.** Findings below.\n") == []


# --------------------------------------------------------------------------
# (E, one level down) THE SAME F-BLOCK WRITTEN TWICE INSIDE ONE ITEM. The
# per-NUMBER check above sees an item number that opens two entries; it is
# blind to the identical merge-both-sides shape one level down, a single item
# whose body carries the same F-labelled finding twice. Merge `9e7e551b` kept
# both sides of item 428's finding-set hunk, so F5-F13 were written twice;
# item 433 carried F1-F10 twice, the measured verdicts beside the original
# audit text. The escape hatch is the same reading job every check here leaves
# to a human: fold each label back to one block.
# --------------------------------------------------------------------------
_E_FBLOCK = SECTION + """428. **SUPPLY-CHAIN AND ATTESTATION AUDIT (2026-09-02).** Findings below.
    **F5 MED-HIGH, ✅ FIXED 2026-09-02** (`f2a38301`, with F3). The reconciled entry.
    **F6 MED-HIGH, ✅ FIXED 2026-09-02** (`f2a38301`, with F3). The reconciled entry.
    **F5 MED-HIGH, ✅ FIXED 2026-09-02** (`f2a38301`). The original audit text, kept as filed; the reconciled entry above says the same thing, because the merge kept both sides of the hunk.
    **F6 MED-HIGH, ✅ FIXED 2026-09-02** (`f2a38301`). The original audit text, kept as filed.
"""


def test_e_fblock_written_twice_inside_one_item_bites():
    """Item 428 exactly: the same F-labels open two blocks under one item,
    the finding-level twin of item 106's merge residue one level up."""
    found = gate.duplicate_finding_blocks(_E_FBLOCK)
    assert len(found) == 1, _labels(found)
    assert "item 428" in found[0]
    assert "F5" in found[0] and "F6" in found[0]
    assert "same finding block more than once" in found[0]


def test_e_fblock_passes_once_the_second_copy_is_gone():
    text = SECTION + """428. **SUPPLY-CHAIN AND ATTESTATION AUDIT (2026-09-02).** Findings below.
    **F5 MED-HIGH, ✅ FIXED 2026-09-02** (`f2a38301`, with F3). The reconciled entry.
    **F6 MED-HIGH, ✅ FIXED 2026-09-02** (`f2a38301`, with F3). The reconciled entry.
"""
    assert gate.duplicate_finding_blocks(text) == []


def test_e_fblock_leaves_a_lettered_sub_finding_alone():
    """Item 421's F6 sits beside its own F6(b), a DISTINCT block. `units()`
    labels both "F6" because UNIT_LABEL_RE's capture stops before the "(b)";
    the sub-part must be kept attached so the pair is not read as a duplicate."""
    text = SECTION + """421. **CAPABILITY-TOKEN AUDIT (2026-09-02).** Findings below.
    **F6 HIGH, ✅ FIXED 2026-09-02** (`fix/421-f5f6-seam-value-leaks`). The scrub is placed at the one choke point.
    **F6(b), the declared `Secret[T]` config field reaching the checker. ✅ LANDED 2026-09-02 via PR #211.** A distinct sub-finding, one level down from F6.
"""
    assert gate.duplicate_finding_blocks(text) == []


def test_e_fblock_leaves_distinct_labels_alone():
    text = SECTION + """428. **SUPPLY-CHAIN AND ATTESTATION AUDIT.** Findings below.
    **F1 HIGH, ✅ FIXED 2026-09-02** (`a358278c`). One.
    **F2 HIGH, ✅ FIXED 2026-09-02** (`a358278c`). Two.
"""
    assert gate.duplicate_finding_blocks(text) == []


# --------------------------------------------------------------------------
# (F) A MARKER NAMING THE PR'S OWN HEAD BRANCH, in in-flight phrasing. Rules 1
# to 3 catch a marker that is already stale; this catches the one that is
# about to become stale. A PR adding "FIXING on `fix/277-rust-vec-char`" is
# green while it is open, because that branch exists. Merging deletes the
# branch, so the marker the PR just introduced reddens main's lint on landing
# and, since lint runs on branch tips, every open PR whose merge-ref carries
# the new text. Four occurrences on 2026-09-02.
#
# The bound that makes this usable is PAST TENSE. Two open PRs add text naming
# their own branch and are entirely correct (#147's ``LANDED SO FAR
# (`fix/391-selfhost-parity`)``, #157's ``REVL SIDE LANDED``), because a
# sentence about what a branch DID stays true after the branch is deleted.
# Only a sentence about what a branch IS DOING goes stale. Both shapes are
# pinned below, and the check reads `collect_markers` — the gate's own
# MARKER_RE/BRANCH_RE/WINDOW — so it cannot drift from the gate it extends.
# --------------------------------------------------------------------------
SELF_BRANCH = "fix/277-rust-vec-char"

_F_BITES = SECTION + """277. **RUST TIER AUDIT (2026-09-02).** Findings below.

**F1 HIGH, ❌ STILL OPEN.** The `Vec[Char]` lowering drops the last
element. FIXING on `fix/277-rust-vec-char`.
"""

# The escape hatch, and the house style: cite the PR, which outlives the merge.
_F_PASSES = _F_BITES.replace(
    "FIXING on `fix/277-rust-vec-char`.",
    "Being fixed in PR #175.")


def _markers(text: str) -> list[dict]:
    return gate.collect_markers(text, DIRS, NAMESPACES, HEADS)


def test_f_marker_naming_the_prs_own_branch_in_flight_bites():
    found = gate.self_branch_findings(_markers(_F_BITES), SELF_BRANCH)
    assert len(found) == 1, _labels(found)
    assert "THIS PR's own head branch" in found[0]
    assert "stale the moment it lands" in found[0]


def test_f_passes_once_the_marker_cites_the_pr_instead_of_the_branch():
    """`PR #175` is not a branch, so nothing is collected to go stale."""
    assert _markers(_F_PASSES) == []
    assert gate.self_branch_findings(_markers(_F_PASSES), SELF_BRANCH) == []


def test_f_past_tense_self_naming_passes():
    """The #147 shape. ``LANDED SO FAR (`fix/391-selfhost-parity`)`` on a PR
    from `fix/391-selfhost-parity` looks exactly like the trap and is not one:
    what a branch DID stays true after the branch is deleted."""
    text = SECTION + """391. **SELF-HOST PARITY (2026-09-02).** Findings below.

**F1 MEDIUM, ◑ PARTLY CLOSED.** LANDED SO FAR (`fix/391-selfhost-parity`):
the lowerer's fn-body path and the wasm flow emitter.
"""
    assert _markers(text) == []
    assert gate.self_branch_findings(
        _markers(text), "fix/391-selfhost-parity") == []


def test_f_past_tense_passes_on_the_same_sentence_the_in_flight_form_fails():
    """The one word that decides it, on one fixture, so the two shapes cannot
    drift apart: 'FIXING on X' fails and 'landed via X' passes."""
    past = _F_BITES.replace("FIXING on `fix/277-rust-vec-char`.",
                            "Landed via `fix/277-rust-vec-char`.")
    assert _markers(past) == []
    assert gate.self_branch_findings(_markers(past), SELF_BRANCH) == []


def test_f_leaves_a_marker_naming_a_DIFFERENT_branch_alone():
    """The false positive this check must not have. A marker naming another
    live branch is legitimate and is `branch_findings`' business, not this
    one's — the merge of THIS PR does not delete that branch."""
    other = _F_BITES.replace("fix/277-rust-vec-char", "fix/999-somewhere-else")
    assert len(_markers(other)) == 1, "fixture stopped producing a marker"
    assert gate.self_branch_findings(_markers(other), SELF_BRANCH) == []


def test_f_leaves_a_marker_citing_a_sha_alone():
    """A sha has no slash, so it is not a branch reference and outlives the
    merge anyway. The second half of the documented remedy."""
    sha = _F_BITES.replace("FIXING on `fix/277-rust-vec-char`.",
                           "Being fixed on `1602cc94`.")
    assert gate.self_branch_findings(_markers(sha), SELF_BRANCH) == []


def test_f_the_in_progress_glyph_form_bites_too():
    """`collect_markers`' other source: an item whose leading status glyph is
    the in-progress one, with no phrase at all. It asserts the same thing and
    goes stale the same way."""
    text = SECTION + ("277. \U0001f6a7 **The rust `Vec[Char]` lowering drops "
                      "the last element.** On `fix/277-rust-vec-char`.\n")
    found = gate.self_branch_findings(_markers(text), SELF_BRANCH)
    assert len(found) == 1, _labels(found)
    assert "carries the in-progress glyph" in found[0]


def test_f_is_off_when_no_head_branch_is_known():
    """A push to main passes an empty `--head-branch`. Nothing is a self-name
    then, and main's own behaviour is unchanged."""
    assert gate.self_branch_findings(_markers(_F_BITES), "") == []
