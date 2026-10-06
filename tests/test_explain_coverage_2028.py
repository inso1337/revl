"""`revl explain` covers every code the compiler emits (issue #2028).

Two halves of one defect. `explain` answered only for the codes in
`diagnostics.GUARANTEES` — 22 of the 56 codes the compiler actually mints — so
34 of them answered "no diagnostic code `X`", including the host-boundary
(`HOST-METHOD`, `HOST-ARITY`), termination (`L1`, `L4`), admission (`R2`) and
lifecycle codes an author meets first. And a rejection carrying one of those
codes came back with neither a `fix` nor a `guarantee` machine field, so the
tool consuming a structured diagnostic had nothing to act on.

The roster is not hand-listed here. `revl.emitted_codes` derives it by walking
`src/revl/**/*.py` for the sites that mint a code, and
`test_every_emitted_code_has_an_entry` asserts the prose table in
`diagnostics.py` closes over that derived set: adding a `code=` site to the
compiler without an entry fails this file, so the two cannot drift apart again.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import diagnostics as dg  # noqa: E402
from revl.emitted_codes import TAG, dynamic_code_sites, emitted_codes  # noqa: E402
from revl.errors import RevlError  # noqa: E402

EMITTED = emitted_codes()

# The one code that deliberately carries neither machine field. Every syntax
# error is raised with a site-specific `fix` naming the token to insert
# (`src/revl/parser.py`), so a canned table entry would be worse than none —
# `classify()` still projects the per-site `fix` when the parser supplies one.
# `tests/test_why_traces.py` pins that absence.
NO_MACHINE_FIELD = {"SYNTAX"}


def _case_twins() -> set:
    """Codes whose uppercase form is itself a distinct code in the roster."""
    by_fold: dict = {}
    for code in EMITTED:
        by_fold.setdefault(code.upper(), []).append(code)
    return {c for group in by_fold.values() if len(group) > 1 for c in group}


_CASE_TWINS = _case_twins()


def _refusal(code: str) -> RevlError:
    return RevlError("x.rvl", 1, f"a refusal carrying {code}", code=code)


# ------------------------------------------------ the roster cannot drift

def test_the_emitter_scan_derives_a_roster():
    """The premise: the scan finds the codes, not an empty or stub set."""
    assert len(EMITTED) > 40
    assert {"G4", "HOST-METHOD", "REVL", "lifecycle"} <= set(EMITTED)
    for code, sites in EMITTED.items():
        assert sites, f"{code} has no emission site"


def test_every_emitted_code_has_an_entry():
    """THE closure. A code the compiler mints and `diagnostics` does not
    answer for is the defect this file exists to prevent from returning."""
    uncovered = sorted(set(EMITTED) - set(dg.GUARANTEES) - set(dg.OTHER_CODES))
    assert not uncovered, (
        "the compiler emits these codes and `revl explain` has no entry for "
        f"them: {uncovered}")


def test_the_prose_table_invents_no_code():
    """The other direction: an entry no emitter site produces is a typo or a
    retired code, and `explain` should not promise it."""
    assert set(dg.OTHER_CODES) & set(dg.GUARANTEES) == set()
    assert set(dg.OTHER_CODES) <= set(EMITTED)


def test_the_scan_sees_every_code_shape():
    """No site mints a code through an expression the scan cannot read, and
    the tag the scan looks for is the tag `classify` looks for."""
    dynamic = dynamic_code_sites()
    assert not dynamic, f"unreadable code sites: {dynamic}"
    assert TAG.pattern == dg._TAG.pattern


# ------------------------------------------------------ explain answers

@pytest.mark.parametrize("code", sorted(EMITTED))
def test_every_emitted_code_is_explained(code):
    record = dg.explain(code)
    assert record["ok"], record
    assert record["code"] == code
    assert record.get("meaning") or record.get("guarantee"), record


@pytest.mark.parametrize("code", sorted(set(EMITTED) - _CASE_TWINS))
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
    run, not a roster of codes that do not include theirs."""
    for code in ("HOST-METHOD", "HOST-ARITY", "L4", "lifecycle"):
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
    assert "G4" in miss["known"] and "HOST-METHOD" in miss["known"]


# ------------------------------------------------------ classify carries it

@pytest.mark.parametrize("code", sorted(EMITTED))
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


@pytest.mark.parametrize("code", sorted(set(EMITTED) - NO_MACHINE_FIELD))
def test_classify_never_returns_an_inert_record(code):
    """Half two of the issue: a structured rejection an agent cannot act on is
    only half a diagnostic. Every emitted code carries a machine field."""
    record = dg.classify(_refusal(code))
    assert record.get("guarantee") or record.get("fix"), record


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

    assert main(["explain", "host-method"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("HOST-METHOD  ")
    assert "fix:" in out
    assert "category: host-boundary" in out

    assert main(["explain", "lifecycle", "--json"]) == 0
    payload = capsys.readouterr().out
    import json
    assert json.loads(payload)["ok"] is True

    assert main(["explain", "no-such-code"]) == 1


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
                                  "arguments": {"code": "HOST-METHOD"}}})
    result = response["result"]
    assert not result.get("isError")
    assert result["structuredContent"]["ok"] is True
    assert result["structuredContent"]["fix"]
