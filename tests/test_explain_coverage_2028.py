"""`revl explain` covers every code the compiler emits (issue #2028).

Two halves of one defect. `explain` answered only for the codes in
`diagnostics.GUARANTEES` — 22 of the 56 codes the compiler actually mints — so
34 of them answered "no diagnostic code `X`", including the host-boundary
(`HOST-METHOD`, `HOST-ARITY`), termination (`L1`, `L4`), admission (`R2`, `R4`)
and lifecycle codes an author meets first. And a rejection carrying one of those
codes came back with neither a `fix` nor a `guarantee` machine field, so the
tool consuming a structured diagnostic had nothing to act on.

Eight of the 34 are excepted, and deliberately. The evolution curriculum
(roadmap item 533, docs/design/533-evolution-curriculum.md) keeps one task per
*reserved* code — a refusal an agent receives and cannot look up — and the easy
rung of that curriculum IS the proof that the code is unanswerable. `revl
explain` answering for one of those does not close the gap, it deletes the task,
so the roster stops short of them: `HOST-ARITY`, `HOST-METHOD`, `L1`, `L4`,
`R2`, `R4`, `SYNTAX` and `lifecycle` still answer "no diagnostic code", and
`tests/test_evolve_curriculum.py` is the test that holds them there. The other
26 now answer.

Neither set is hand-listed here. `revl.emitted_codes` derives both by walking
`src/revl/**/*.py` — once for the sites that mint a code, once for the narrower
set the reference *refuses* with — and this file closes the loop at both ends:
the roster must cover the emitters' complement of the reserved set, the reserved
set must be exactly what `explain` refuses, and `refusal_codes()` must agree
code-for-code with the curriculum generator's own scan. A `code=` site added to
the compiler fails here until it has an entry; a reserved code added to the
roster fails here and in the curriculum.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from _load_by_path import load_by_path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import diagnostics as dg  # noqa: E402
from revl.emitted_codes import (  # noqa: E402
    TAG,
    dynamic_code_sites,
    emitted_codes,
    refusal_codes,
    reserved_codes,
)
from revl.errors import RevlError  # noqa: E402

EMITTED = emitted_codes()

# The codes the reference refuses with that no guarantee catalogue holds: the
# evolution curriculum's easy rung, and the one population `revl explain` must
# keep answering "no diagnostic code" for (see the module docstring). Derived
# from the reference's own raise sites, never listed by hand — the assertion
# that this set is *exactly* what `explain` refuses is below, so a hand-edited
# roster row cannot quietly shrink the curriculum.
RESERVED = reserved_codes()

# What the roster must answer for: everything the compiler mints, less the
# reserved set.
COVERED = frozenset(EMITTED) - RESERVED


def _case_twins() -> set:
    """Codes whose uppercase form is itself a distinct code in the roster."""
    by_fold: dict = {}
    for code in COVERED:
        by_fold.setdefault(code.upper(), []).append(code)
    return {c for group in by_fold.values() if len(group) > 1 for c in group}


_CASE_TWINS = _case_twins()


def _refusal(code: str) -> RevlError:
    return RevlError("x.rvl", 1, f"a refusal carrying {code}", code=code)


@pytest.fixture(scope="module")
def curriculum():
    """The curriculum generator, loaded by path — the authority on which codes
    are reserved, and the derivation `refusal_codes()` must agree with."""
    return load_by_path("evolve_curriculum",
                        ROOT / "tools" / "evolve_curriculum.py")


# ------------------------------------------------ the roster cannot drift

def test_the_emitter_scan_derives_a_roster():
    """The premise: the scan finds the codes, not an empty or stub set."""
    assert len(EMITTED) > 40
    assert {"G4", "HOST-METHOD", "REVL", "lifecycle"} <= set(EMITTED)
    for code, sites in EMITTED.items():
        assert sites, f"{code} has no emission site"


def test_every_emitted_code_has_an_entry():
    """THE closure. A code the compiler mints and `diagnostics` does not
    answer for — and the curriculum does not reserve — is the defect this file
    exists to prevent from returning."""
    uncovered = sorted(COVERED - set(dg.GUARANTEES) - set(dg.OTHER_CODES))
    assert not uncovered, (
        "the compiler emits these codes, the curriculum reserves none of them, "
        f"and `revl explain` has no entry for them: {uncovered}")


def test_the_reserved_set_is_exactly_what_explain_refuses():
    """The other half of the closure, and the one the curriculum depends on: a
    code `explain` refuses is a reserved code and nothing else, and every
    reserved code is refused. Add a reserved code to the roster and this fails
    here, in the same commit that would delete a curriculum task."""
    refused = {code for code in EMITTED if not dg.explain(code)["ok"]}
    assert refused == set(RESERVED), (
        f"explain refuses {sorted(refused)}, but the curriculum reserves "
        f"{sorted(RESERVED)}")


def test_a_reserved_code_answers_exactly_what_the_curriculum_requires():
    """The shape `tests/test_evolve_curriculum.py` asserts, restated at the
    source of the roster so the two cannot disagree about it."""
    assert RESERVED, "the curriculum's easy rung is empty — the derivation broke"
    for code in sorted(RESERVED):
        record = dg.explain(code)
        assert record["ok"] is False, record
        assert record["message"].startswith("no diagnostic code"), record
        assert code not in record["known"], (
            f"{code} is reserved and must not be advertised as answerable")
        assert code not in dg.ALL_CODES, code


def test_the_reserved_derivation_agrees_with_the_curriculum(curriculum):
    """`refusal_codes()` is a second implementation of the curriculum
    generator's `enforced_codes()` — `src/revl` must not import `tools/`, so
    the rule is repeated rather than shared. This is the assertion that makes
    the repetition safe: same keys, code for code, so the roster's complement
    and the curriculum's easy rung cannot drift into disagreeing about which
    codes the reference stamps."""
    mine, theirs = refusal_codes(), curriculum.enforced_codes()
    assert set(mine) == set(theirs), {
        "only_mine": sorted(set(mine) - set(theirs)),
        "only_curriculum": sorted(set(theirs) - set(mine)),
    }
    assert set(RESERVED) == set(theirs) - set(dg.GUARANTEES)


def test_the_prose_table_invents_no_code():
    """The other direction: an entry no emitter site produces is a typo or a
    retired code, and `explain` should not promise it."""
    assert set(dg.OTHER_CODES) & set(dg.GUARANTEES) == set()
    assert set(dg.OTHER_CODES) <= set(EMITTED)
    assert not (set(dg.OTHER_CODES) & set(RESERVED))


def test_the_scan_sees_every_code_shape():
    """No site mints a code through an expression the scan cannot read, and
    the tag the scan looks for is the tag `classify` looks for."""
    dynamic = dynamic_code_sites()
    assert not dynamic, f"unreadable code sites: {dynamic}"
    assert TAG.pattern == dg._TAG.pattern


# ------------------------------------------------------ explain answers

@pytest.mark.parametrize("code", sorted(COVERED))
def test_every_emitted_code_is_explained(code):
    record = dg.explain(code)
    assert record["ok"], record
    assert record["code"] == code
    assert record.get("meaning") or record.get("guarantee"), record


@pytest.mark.parametrize("code", sorted(COVERED - _CASE_TWINS))
def test_explain_is_still_case_insensitive(code):
    """`revl explain g4` must keep working. Excluded: the codes that have a
    case-twin, where the exact match deliberately wins (below)."""
    assert dg.explain(code.upper()) == dg.explain(code)
    assert dg.explain(code.lower()) == dg.explain(code)


def test_a_case_pair_is_two_different_codes():
    """`HALTED` (the gate's verdict) and `halted` (the HTTP face's JSON body)
    differ only in case, so the exact match must win over the fold."""
    assert dg.explain("HALTED")["code"] == "HALTED"
    assert dg.explain("halted")["code"] == "halted"


def test_an_emitted_code_names_the_rewrite_that_satisfies_it():
    """The point of the fix: an author who meets one of these has a command to
    run, not a roster of codes that do not include theirs. Every code that is
    answered for and is not already a guarantee code is one of these — the 26
    that answered "no diagnostic code" before #2028."""
    newly = sorted(COVERED - set(dg.GUARANTEES))
    assert len(newly) == 26, newly
    for code in newly:
        if code == "REVL":
            continue        # the fallback: its guarantee is the honest "unknown"
        record = dg.explain(code)
        assert record["ok"], record
        assert record["fix"], f"{code} has no fix line"
        assert record["category"], f"{code} has no category"


def test_the_fallback_says_so_rather_than_nothing():
    """`REVL` is the total fallback for a refusal no pattern matched. Its
    guarantee is `unclassified` — a machine-readable "we do not know", which is
    the honest answer and the one a consumer can branch on."""
    assert dg.explain("REVL")["guarantee"] == "unclassified"
    assert "fix" not in dg.explain("REVL")


def test_a_miss_still_lists_the_roster():
    miss = dg.explain("no-such-code")
    assert miss["ok"] is False
    assert "G4" in miss["known"] and "R1" in miss["known"]
    # the roster is what `explain` answers for, so it does not advertise the
    # codes it deliberately refuses (the curriculum's easy rung)
    assert not (set(miss["known"]) & set(RESERVED)), sorted(RESERVED)


# ------------------------------------------------------ classify carries it

@pytest.mark.parametrize("code", sorted(COVERED))
def test_classify_emits_the_catalogue_entry(code):
    """`explain` and `classify` are two projections of one table: whatever a
    code promises to `explain` is what a rejection carrying it projects."""
    entry = dg.explain(code)
    record = dg.classify(_refusal(code))
    assert record["code"] == code
    if entry.get("guarantee"):
        assert record.get("guarantee") == entry["guarantee"], record
    if entry.get("fix"):
        assert record.get("fix") == entry["fix"], record


@pytest.mark.parametrize("code", sorted(COVERED))
def test_classify_never_returns_an_inert_record(code):
    """Half two of the issue: a structured rejection an agent cannot act on is
    only half a diagnostic. Every answered code carries a machine field."""
    record = dg.classify(_refusal(code))
    assert record.get("guarantee") or record.get("fix"), record


def test_classify_leaves_a_reserved_code_as_it_found_it():
    """A reserved code is a refusal the agent is meant to work out, so
    `classify` adds no machine field for it — the same answer base gave, and
    the reason `SYNTAX` still projects only its site-specific fix."""
    for code in sorted(RESERVED):
        record = dg.classify(_refusal(code))
        assert record["code"] == code
        assert "guarantee" not in record, record
        assert "fix" not in record, record


def test_classify_uses_the_site_specific_fix_when_the_parser_supplies_one():
    """The declared exception: `SYNTAX` has no canned entry because the parser
    attaches the exact token to insert. That per-site fix still projects."""
    record = dg.classify(RevlError("x.rvl", 1, "expected `}`, found `;`",
                                   code="SYNTAX", fix="add the closing brace"))
    assert record["fix"] == "add the closing brace"
    assert "guarantee" not in record


def test_classify_falls_back_to_the_unclassified_guarantee():
    """An error with no code at all is the fallback `REVL` — and now says so."""
    record = dg.classify(RevlError("x.rvl", 1, "something went wrong"))
    assert record["code"] == "REVL"
    assert record["guarantee"] == "unclassified"


# ------------------------------------------------------------ the two faces

def test_the_cli_explains_an_emitted_code(capsys):
    from revl.__main__ import main

    assert main(["explain", "r1"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("R1  ")
    assert "fix:" in out
    assert "category: runtime" in out

    assert main(["explain", "g4", "--json"]) == 0
    payload = capsys.readouterr().out
    import json
    assert json.loads(payload)["ok"] is True

    assert main(["explain", "no-such-code"]) == 1


def test_the_cli_refuses_a_reserved_code(capsys):
    """`revl explain host-method` exits 1 and hands back the roster — the
    curriculum task the code stands for is what makes it unanswerable, so the
    CLI must not be a second door onto it."""
    import json
    from revl.__main__ import main

    for code in sorted(RESERVED):
        assert main(["explain", code.lower(), "--json"]) == 1, code
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False, payload
        assert payload["message"].startswith("no diagnostic code"), payload
        assert code not in payload["known"], code


def test_the_cli_renders_every_field_the_producer_emits(capsys):
    """The renderer re-spells the `explain` envelope, and the vocabulary ledger
    (`tests/fixtures/vocabulary_mirror_ledger.json`, checked by
    `tools/check_vocabulary_mirrors.py`) holds the two in step. `category` was
    the field the producer emitted and the renderer never read — invisible to
    the human, which is what the lint step reds on. Every field an entry
    carries must reach the terminal."""
    from revl.__main__ import main

    for code, entry in sorted(dg.OTHER_CODES.items()):
        assert main(["explain", code]) == 0
        out = capsys.readouterr().out
        assert f"category: {entry['category']}" in out
        if entry["guarantee"]:
            assert entry["guarantee"] in out
        if entry["fix"]:
            assert f"fix: {entry['fix']}" in out


def test_the_mcp_tool_explains_an_emitted_code():
    """`revl_explain` is the MCP twin of the same table (issue label: mcp)."""
    from revl.mcp.server import handle

    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "revl_explain",
                                  "arguments": {"code": "R1"}}})
    result = response["result"]
    assert not result.get("isError")
    assert result["structuredContent"]["ok"] is True
    assert result["structuredContent"]["fix"]
