"""The conformance matrix's third question: do the tiers agree on the ANSWER?

`tools/conformance.py` asks "did the emitter raise?" and
`tests/test_conformance_validate.py` asks "does that output survive its own
toolchain?". Both stop at compile depth, so a green matrix proved a construct
*emits and compiles* per tier and never what it *evaluates to* — two tiers
could be green on the same case and disagree about the answer (issue #244).
`tsc --noEmit` covered `backends/typescript/demo.ts` the whole time its runtime
assertions were failing, for exactly this reason.

These tests run the executable half of the corpus on every tier whose runtime
is present and check that each computes the ONE answer the corpus declares. It
is a differential: every tier asserts the same literal, so the only way to fail
is to build, run, and mean something else, and a tier whose runtime is absent
reports `-` rather than a pass.

The other half of the file is the bookkeeping that keeps the matrix honest: a
case with no executable answer must be marked `compile-only` in the generated
table, with a reason, so the two claim strengths never render as one uniform
block of `ok` again.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "src"))

import conformance  # noqa: E402
from validate import EXECUTORS  # noqa: E402


# ---------------------------------------------------------------------------
# the differential itself
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def programs() -> list[tuple[str, str]]:
    cases = conformance.executable_cases()
    assert cases, "the corpus declares no executable case at all"
    return cases


@pytest.mark.parametrize("tier", sorted(EXECUTORS))
def test_every_tier_computes_the_declared_answer(tier, programs):
    """One source, one declared answer, every tier that can run it.

    A tier without its runtime skips *loudly* with the reason its own runner
    gave — "nothing ran it" must never be recorded as "it agreed".
    """
    executor = EXECUTORS[tier]
    reason = executor.unavailable()
    if reason:
        pytest.skip(f"{tier}: {reason}")

    results = executor.check(programs)
    assert len(results) == len(programs), f"{tier}: not every case was executed"
    disagreements = {label: detail for label, (status, detail) in results.items()
                     if status != "ok"}
    assert not disagreements, (
        f"{tier} built and ran these constructs and computed a different answer "
        f"than the corpus declares — a compile-depth matrix cannot see this: "
        f"{disagreements}")


def test_the_probe_programs_are_still_admissible(programs):
    """Each executed program is the case plus a probe; the frontend must take
    it. A probe that stops compiling would silently drop its case from the
    executed set, turning a strong claim into a weak one with no signal."""
    for label, source in programs:
        conformance.compile_source(source)  # raises RevlError on rejection
        assert 'test "conformance_answer"' in source, (
            f"{label}: the probe program carries no answer assertion")


def test_every_probe_names_a_real_case():
    """A probe keyed on a label the corpus no longer has would go unrun while
    the matrix still counted its row as executed."""
    labels = {f"{group}/{name}" for group, name, _ in conformance.CASES}
    orphans = set(conformance.PROBES) - labels
    assert not orphans, f"PROBES names cases that do not exist: {sorted(orphans)}"


def test_a_wrong_answer_is_caught():
    """The negative control. A harness that reports `agree` whatever the tier
    computed would look exactly like a green run, so prove it can go red: the
    reference tier must reject an answer the construct does not produce."""
    case = dict((f"{g}/{n}", s) for g, n, s in conformance.CASES)
    source = (case["fn/pure fn"]
              + "\npub fn probe() -> Int { return add(20, 22) }\n"
              + 'test "conformance_answer" { assert probe() == 41 }\n')
    executor = EXECUTORS["python"]
    reason = executor.unavailable()
    if reason:
        pytest.skip(f"python: {reason}")
    (status, detail), = executor.check([("negative control", source)]).values()
    assert status != "ok", "a wrong answer passed — the executor proves nothing"
    assert "41" in detail, f"the failure does not name the answer: {detail}"


# ---------------------------------------------------------------------------
# the claim column — the matrix must say which kind of claim each row makes
# ---------------------------------------------------------------------------

def test_the_claim_split_covers_every_admitted_case():
    """Executed + compile-only must account for every row in the table. A case
    in neither bucket would render with no claim at all."""
    report = conformance.run()
    rows = {row["case"] for row in report["cases"]}
    executed = rows & set(conformance.PROBES)
    compile_only = conformance.compile_only_index(rows)
    counted = executed | {label for group in compile_only.values() for label in group}
    assert counted == rows, f"cases with no claim: {sorted(rows - counted)}"
    assert not (executed & {label for group in compile_only.values()
                            for label in group}), (
        "a case is counted as both executed and compile-only")


def test_every_compile_only_case_carries_a_reason():
    """`compile-only` on its own reads as "not done yet". The reason is what
    makes it a claim about the construct instead."""
    for group, name, _ in conformance.CASES:
        label = f"{group}/{name}"
        reason = conformance.compile_only_reason(label, group)
        if label in conformance.PROBES:
            assert reason is None, f"{label} is executed but also has a reason"
            continue
        assert reason in conformance.COMPILE_ONLY_REASONS, (
            f"{label}: unknown compile-only reason {reason!r}")


def test_the_generated_matrix_marks_each_row_with_its_claim():
    """The rendered table is where this has to be visible — a uniform-looking
    table over two claim strengths is how the gap stayed invisible."""
    block = conformance.readme_block(conformance.run(), None)
    assert "| construct | claim |" in block, "the claim column is missing"
    for line in block.splitlines():
        if not line.startswith("| ") or line.startswith("| construct"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells[0] not in {f"{g}/{n}" for g, n, _ in conformance.CASES}:
            continue
        assert cells[1] == conformance.claim(cells[0]), (
            f"{cells[0]}: the rendered claim {cells[1]!r} is not the corpus's")


def test_the_claim_column_does_not_need_a_toolchain():
    """The claim is corpus data, so it regenerates byte-identically anywhere —
    which is what lets the staleness gate diff a block that carries it. Whether
    the executed rows AGREED is a `--execute` run, gated separately."""
    assert conformance.readme_block(conformance.run(), None) == \
        conformance.readme_block(conformance.run(), None)
