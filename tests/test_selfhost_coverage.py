"""The blind-spot gate for the self-host byte-agreement oracles (item 429).

`tools/selfhost_coverage.py` measures what the oracles in
`tests/test_selfhost_emit_*.py` do NOT assert: the constructs both the reference
emitter and its `selfhost/*.rvl` port implement while no corpus document reaches
them, and the constructs only the reference implements while no corpus document
would notice. Item 429 records five same-day defects that lived in exactly those
two populations, the worst of them a self-hosted python emitter that could not
emit `Secret[T]` redaction at either end while every oracle stayed green.

This module runs that gate, and — more importantly — keeps it from passing
VACUOUSLY. A coverage gate whose extractor silently returns nothing is worse
than no gate: it is a green light with no lamp behind it. So the tests below pin
that both construct tables are populated, that the corpus survey is populated,
and that the gate REPRODUCES the item-429(d) failure when the corpus case that
closed it is taken away.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# The mirrored pairs this gate measures, spelled as paths so
# `tools/affected_tests.py::_tier_tests` selects this module whenever one of
# them changes: a new branch in a reference emitter is precisely the event that
# can open a fresh blind spot.
MIRRORED = (
    ("backends/python/emit.py", "selfhost/emit_py.rvl"),
    ("backends/typescript/emit.py", "selfhost/emit_ts.rvl"),
    ("backends/go/emit.py", "selfhost/emit_go.rvl"),
    ("backends/java/emit.py", "selfhost/emit_java.rvl"),
    ("backends/rust/emit.py", "selfhost/emit_rust.rvl"),
    ("backends/wasm/emit.py", "selfhost/emit_wasm.rvl"),
)
TIERS = ("py", "ts", "go", "java", "rust", "wasm")


def _load(stem: str):
    spec = importlib.util.spec_from_file_location(
        f"{stem}_tool", ROOT / "tools" / f"{stem}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _load("selfhost_coverage")



@pytest.fixture(scope="module")
def data(tool):
    return tool.survey()


def test_the_recorded_blind_spots_are_the_actual_blind_spots(tool, data):
    """The gate itself.

    Failing here means one of four things, and the message says which: a
    construct both sides mirror has no corpus case; a reference-only construct
    no corpus case reaches has appeared; or a ledger entry has gone stale
    because coverage caught up. Never silence it by widening the ledger without
    reading what it names.
    """
    problems = tool.check(data)
    assert problems == [], "\n".join(problems)


@pytest.mark.parametrize("tier", TIERS)
def test_the_construct_tables_are_not_empty(tool, data, tier):
    """Non-vacuity, reference side and self-host side.

    Both extractors read source with a fixed grammar (`ast` for the reference,
    the `value_field`/`node_kind` spellings for the port). If a refactor changes
    either spelling the extractor would quietly return an empty table and the
    gate would pass while measuring nothing. Every emitter dispatches on the
    three base expression kinds, so demand them.
    """
    found = data[tier]
    for construct in ("kind=lit", "kind=var", "kind=bin"):
        assert construct in found["reference"], (
            f"the reference construct table for {tier} lost `{construct}`: the "
            f"`ast` extractor in tools/selfhost_coverage.py no longer matches "
            f"how backends/*/emit.py spells its dispatch")
        assert construct in found["mirrored"], (
            f"the self-host construct table for {tier} lost `{construct}`: the "
            f"regex grammar in tools/selfhost_coverage.py no longer matches how "
            f"selfhost/emit_{tier}.rvl spells its dispatch")


@pytest.mark.parametrize("tier", TIERS)
def test_the_corpus_survey_is_not_empty(tool, tier):
    """Non-vacuity, corpus side: an empty survey would mark EVERY construct
    blind, which the ledger would then have to absorb wholesale. Pin that the
    tier's corpus compiles and exhibits the base kinds."""
    exhibited = tool.corpus_constructs(tier)
    assert {"kind=lit", "kind=var", "kind=bin"} <= exhibited


def test_the_gate_reproduces_the_item_429d_secret_gap(tool, monkeypatch):
    """The load-bearing proof.

    `secrets.rvl` is the corpus document item 429 exit (5) added after finding
    that `selfhost/emit_py.rvl` could emit NEITHER end of the `Secret[T]`
    redaction markings — a self-hosted emitter producing a program that does not
    redact, invisible because the corpus contained no `Secret[` at all. Take
    that document back out and this gate must NAME the two markings. A gate that
    stays green under the ablation would be decoration.
    """
    keep = tool.corpus_documents

    def without_secrets(tier):
        return [p for p in keep(tier) if p.name != "secrets.rvl"]

    monkeypatch.setattr(tool, "corpus_documents", without_secrets)
    problems = tool.check(tool.survey())
    named = [p for p in problems if "secret" in p]
    assert len(named) >= 2, (
        "dropping secrets.rvl from the py corpus must make the `secret` and "
        f"`secret_return` markings blind again; the gate said: {problems}")
    assert any("secret_return=<true>" in p for p in named)
    assert any("secret=<true>" in p for p in named)


def test_every_recorded_blind_spot_carries_a_reason(tool):
    """The `blind` half of the ledger is reason-first on purpose: a construct
    both sides implement and nothing exercises is a DECISION, and the decision
    has to be written down where the next reader hits it."""
    ledger = tool._ledger()
    for tier, entry in ledger.items():
        for reason, constructs in entry.get("blind", {}).items():
            assert len(reason) > 40, (
                f"{tier}: `{reason}` is not a reason, it is a label")
            assert constructs, f"{tier}: reason with no constructs: {reason}"


def test_generic_baseline_cannot_masquerade_as_closure(tool, monkeypatch, tmp_path):
    ledger = {tier: {"blind": {"NOT TRIAGED": ["kind=lit"]},
                     "unported": {"deliberate decision": ["kind=var"]}}
              for tier in TIERS}
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(ledger))
    monkeypatch.setattr(tool, "LEDGER", path)
    problems = tool.check({tier: {"blind": [], "unported": []} for tier in TIERS})
    assert any("generic reason" in problem for problem in problems)


def test_construct_closure_rejects_duplicates_and_missing_tiers(tool, monkeypatch, tmp_path):
    ledger = {"py": {"blind": {"specific decision": ["kind=lit"]},
                      "unported": {"another decision": ["kind=lit"]}}}
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(ledger))
    monkeypatch.setattr(tool, "LEDGER", path)
    problems = tool.check({"py": {"blind": [], "unported": []}})
    assert any("classified more than once" in problem for problem in problems)
    assert any("missing construct ledger tier" in problem for problem in problems)


def test_construct_check_fails_closed_on_malformed_maps(tool, monkeypatch, tmp_path):
    ledger = {tier: {"blind": None, "unported": []} for tier in TIERS}
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(ledger))
    monkeypatch.setattr(tool, "LEDGER", path)
    data = {tier: {"blind": None, "unported": []} for tier in TIERS}
    problems = tool.check(data)
    assert any("missing `blind` reason map" in problem for problem in problems)
    assert any("construct populations must be lists" in problem for problem in problems)
