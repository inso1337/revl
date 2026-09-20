"""FRAMEWORK-BENCH-1: the suite of roadmap item 548 (issue #1267).

These tests hold the benchmark to the properties that make it worth publishing,
not to the numbers it currently produces. A test that pinned "41 residual
documents" would fail the day somebody closes a gap, which is the wrong
direction for a gate to point. What is pinned instead:

  * the refused column is recomputed from the ledgers the tests gate, and
    disagrees with none of them;
  * a source that will not read is reported as unavailable and never as zero;
  * a cell is a measured number or `not-run`, and a not-run cell says what it is
    blocked on;
  * the report the harness emits passes the frozen honesty protocol;
  * the pin records what the endpoint said rather than what the roadmap says,
    and an unreachable endpoint produces an unreachable pin rather than a
    fabricated one.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "bench"
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(ROOT / "tools"))

import check_eval_report  # noqa: E402
import framework_bench  # noqa: E402
import model_pin  # noqa: E402
import refusal_inventory  # noqa: E402


# ---------------------------------------------------------------------------
# hosts.json: the registry an outsider edits
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def hosts() -> dict:
    return json.loads((BENCH / "hosts.json").read_text())


def test_the_registry_names_three_hosts_and_the_refused_column_leads(hosts):
    ids = [h["id"] for h in hosts["hosts"]]
    assert ids == ["raw-ts", "framework", "revl"], (
        "the comparison is three hosts; a two-host table is a different claim")
    # "leading" is a property of the data, not of the prose: the refused column
    # is first and is the only one flagged `lead`.
    assert hosts["columns"][0]["id"] == "refused"
    leads = [c["id"] for c in hosts["columns"] if c.get("lead")]
    assert leads == ["refused"]


def test_the_raw_typescript_host_records_the_asymmetry_rather_than_a_compile_rate(hosts):
    raw = next(h for h in hosts["hosts"] if h["id"] == "raw-ts")
    assert raw["scored_on"] == "residue"
    assert raw["not_scored_on"] == "compile-rate"
    # The asymmetry has to be carried by the artifact, because the artifact is
    # what a reader who skips the design note sees.
    assert "always compiles" in " ".join(raw["asymmetry"])


def test_an_unnamed_framework_host_is_blocked_rather_than_guessed(hosts):
    fw = next(h for h in hosts["hosts"] if h["id"] == "framework")
    if fw.get("runnable"):
        assert fw["name"] and fw["prompt"], (
            "a runnable framework host must name the framework and its prompt")
    else:
        assert fw["name"] is None and fw.get("blocked_on")
        assert fw.get("selection_criteria"), (
            "an unnamed host must say what would qualify one, or the gap is "
            "unactionable")


def test_the_registry_discloses_who_wrote_the_prompts(hosts):
    # Two of three prompts and all thirty briefs are ours. A suite that did not
    # say so would be inviting exactly the dismissal issue #1267 predicts.
    assert hosts["who_wrote_what"]
    authored = {h["id"]: h.get("authored_by") for h in hosts["hosts"]}
    assert authored["raw-ts"] == "this repository"
    assert authored["revl"] == "this repository"
    assert hosts["tasks"]["authored_by"] == "this repository"


def test_the_pinned_model_is_local_and_substitutable(hosts):
    pin = hosts["pinned_model"]
    assert pin["endpoint"].startswith("http://127.0.0.1"), (
        "the pin exists so there is no third-party service in the loop")
    assert pin["substitutable"] is True
    assert pin["sampling"]["temperature"] == 0, (
        "one number per cell with an n needs a deterministic decode")


# ---------------------------------------------------------------------------
# The refused column
# ---------------------------------------------------------------------------


def test_the_residual_count_agrees_with_the_ledger_the_tests_gate():
    """Recomputed, not transcribed.

    This is the property the whole column rests on: the benchmark's number and
    the number `test_the_residual_is_located_in_lower_not_in_the_emitter` pins
    are the same number, read from the same place.
    """
    section = refusal_inventory.native_chain_residual()
    source = ast.literal_eval(
        _module_literal(ROOT / "tests" / "test_selfhost_compile.py",
                        "LOWER_GAP_DOCS"))
    assert set(section["tiers"]) == set(source)
    for tier, docs in source.items():
        assert section["tiers"][tier]["residual"] == len(docs)
        assert section["tiers"][tier]["documents"] == sorted(docs)
    assert section["total_residual"] == sum(len(v) for v in source.values())


def _module_literal(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text())
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return ast.unparse(node.value)
    raise AssertionError(f"{path.name} has no module-level {name}")


def test_every_residual_is_measured_against_its_own_corpus_size():
    """A residual of 9 on a corpus of 59 and of 9 on a corpus of 12 are
    different results, so the denominator travels with the numerator."""
    section = refusal_inventory.native_chain_residual()
    for tier, entry in section["tiers"].items():
        assert entry["corpus"], f"{tier} has no corpus size"
        assert entry["residual"] <= entry["corpus"]
        assert entry["reproduced"] == entry["corpus"] - entry["residual"]


def test_the_inventory_publishes_the_fail_open_direction_too():
    """A table of refusals that hid where the gate is too permissive would be
    the selective argument this suite claims to correct."""
    inv = refusal_inventory.build()
    div = inv["sections"].get("gate-reference-divergence")
    assert div is not None, "the fail-open section must be present"
    assert div["total_false_admit"] == len(div["named_programs"])
    # Named, not just counted: a bucket count nobody can look up is not an
    # admission of anything.
    assert all(p.endswith(".rvl") for p in div["named_programs"])


def test_an_unreadable_source_is_unavailable_and_never_zero(monkeypatch, tmp_path):
    """The distinction this asserts is the difference between 'I could not read
    the ledger' and 'there is nothing left to refuse'."""
    monkeypatch.setattr(refusal_inventory, "BLIND_SPOTS", tmp_path / "gone.json")
    inv = refusal_inventory.build()
    assert "unported-constructs" not in inv["sections"]
    assert "unported-constructs" in inv["unavailable"]
    rendered = refusal_inventory.render(inv)
    assert "Sources this run could not read" in rendered
    assert "unported-constructs" in rendered


def test_every_inventory_section_names_the_gate_that_fails_when_it_drifts():
    inv = refusal_inventory.build()
    for name, section in inv["sections"].items():
        assert section.get("gate"), f"{name} publishes a count with no gate"
        assert section.get("source"), f"{name} publishes a count with no source"
        assert section.get("means"), f"{name} publishes a count with no meaning"


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


class _Args:
    """The harness's argument surface, as the tests drive it."""

    admits_from = None
    residue_from = "hand-corpus"
    tokens_from = None
    attempt = 1
    compiler_root = None
    pin = None
    measure_latency = False
    latency_iters = 50


@pytest.fixture(scope="module")
def report() -> dict:
    return framework_bench.build_report(_Args())


def test_the_report_passes_the_frozen_honesty_protocol(report):
    """The exit criterion of issue #1267: the checker version is frozen in the
    report and `tools/check_eval_report.py` passes on it."""
    violations = check_eval_report.check_report(report)
    assert violations == [], violations
    assert report["report_schema"] == check_eval_report.REPORT_SCHEMA


def test_the_report_freezes_the_checker_version(report):
    checker = report["checker"]
    assert checker["available"], checker
    # The frontier is the field that makes two admission rates comparable. If
    # G1 tightens and the rate drops, this is what says the drop is a tightened
    # gate rather than a regression.
    assert checker["frontier"]
    assert checker["language"]
    assert checker["gate_api"]
    assert checker["compiler_commit"]


def test_every_column_is_a_measurement_or_a_named_blocker(report):
    for name, cell in report["columns"].items():
        if cell["status"] == framework_bench.NOT_RUN:
            assert cell.get("blocked_on"), (
                f"column {name} is not run and does not say what blocks it")
        else:
            assert cell["status"] == "measured", (
                f"column {name} has status {cell['status']!r}; a cell is a "
                "measurement or not-run, there is no third state")


def test_no_cell_is_attributed_to_the_pinned_model_without_a_pinned_run(report):
    """The specific dishonesty this guards: a number from one model printed in
    a table headed by another model's name."""
    for name in ("admits", "tokens-to-green"):
        cell = report["columns"][name]
        if cell["status"] == "measured":
            assert cell["is_pinned_model"] is False
            assert "not the pinned model" in cell["note"] or cell["corpus"]


def test_the_refused_column_leads_the_rendered_table(report):
    body = framework_bench.render(report)
    table = body.index("## The table")
    refused_section = body.index("## Refused")
    assert refused_section < table, (
        "the refusal section must precede the table it heads")
    first_row = body[table:].split("|---|---|---|---|\n", 1)[1].splitlines()[0]
    assert "**refused**" in first_row


def test_the_rendered_table_states_the_typescript_asymmetry(report):
    body = framework_bench.render(report)
    assert "Why the raw-TypeScript row is not a compile-rate" in body
    assert "TypeScript always compiles" in body


def test_no_claim_stands_above_measured_without_an_independent_reproducer(report):
    for claim in report["claims"]:
        assert claim["rung"] in ("measured", "demonstrated", "supported")
        if claim["rung"] != "measured":
            assert claim["evidence"].get("reproduced_by"), (
                "a claim above 'measured' needs a reproducer that is not us")
        assert " n=" in claim["text"] or "(n=" in claim["text"], (
            f"claim states no n: {claim['text']}")


def test_the_report_names_what_it_is_not(report):
    gates = {g["gate"] for g in report["remaining_gates"]}
    assert "publication" in gates, (
        "publishing outside this repository is a gate, not a quiet step")
    assert "independent reproduction" in gates
    assert any("pinned-model run" in g for g in gates)
    for gate in report["remaining_gates"]:
        assert gate.get("why"), f"{gate['gate']} is unexplained"


def test_a_residue_number_from_a_corpus_we_wrote_says_so(report):
    residue = report["columns"]["residue"]
    if residue["status"] == "measured" and residue["corpus"].endswith("hand-corpus"):
        assert residue["dismissibility"], (
            "a hand-authored corpus must carry what its number does not show")
        assert "hand" in framework_bench.render(report).lower()


def test_the_injection_column_is_empty_rather_than_filled_with_a_neighbour(report):
    cell = report["columns"]["injection-escape"]
    assert cell["status"] == framework_bench.NOT_RUN
    assert "test_adversarial_gate" in cell["nearest_existing"]
    assert cell["note"], (
        "the cell must say why the nearest available number was not used")


# ---------------------------------------------------------------------------
# The pin
# ---------------------------------------------------------------------------


OLLAMA_TAGS = {
    "models": [{
        "name": "hf.co/ornith-ai/Ornith-1.5-35B-A3B-GGUF:Q4_K_M",
        "digest": "e418857d0565",
        "size": 22616286186,
        "capabilities": ["completion"],
        "details": {"quantization_level": "unknown", "parameter_size": "35.5B",
                    "context_length": 262144, "family": "qwen35moe",
                    "format": "gguf"},
    }],
}


def test_the_pin_resolves_the_name_the_roadmap_writes_and_says_it_normalised():
    """The roadmap writes one string, the endpoint answers to another, and the
    same weights sit behind both. Resolving silently would reintroduce exactly
    the drift the pin exists to stop."""
    ident = model_pin.identity_from_ollama_tags(
        OLLAMA_TAGS, "ornith-ai/Ornith-1.5-35B-A3B:Q4_K_M")
    assert ident["resolution"] == "normalised"
    assert ident["requested"] == "ornith-ai/Ornith-1.5-35B-A3B:Q4_K_M"
    assert ident["resolved"] == "hf.co/ornith-ai/Ornith-1.5-35B-A3B-GGUF:Q4_K_M"
    assert ident["digest"] == "e418857d0565"


def test_normalisation_never_crosses_a_quantisation_or_an_org():
    """Only a registry prefix and a `-GGUF` repository suffix are dropped.
    Anything that could change the weights is not."""
    with pytest.raises(model_pin.PinError):
        model_pin.identity_from_ollama_tags(
            OLLAMA_TAGS, "ornith-ai/Ornith-1.5-35B-A3B:Q8_0")
    with pytest.raises(model_pin.PinError):
        model_pin.identity_from_ollama_tags(
            OLLAMA_TAGS, "someone-else/Ornith-1.5-35B-A3B:Q4_K_M")


def test_the_pin_records_both_quantisation_answers_when_they_disagree():
    ident = model_pin.identity_from_ollama_tags(
        OLLAMA_TAGS, "hf.co/ornith-ai/Ornith-1.5-35B-A3B-GGUF:Q4_K_M")
    assert ident["quantisation_reported_by_endpoint"] == "unknown"
    assert ident["quantisation_from_tag"] == "Q4_K_M"


def test_an_unreachable_endpoint_produces_an_unreachable_pin_not_a_number():
    pin = model_pin.build_pin("http://127.0.0.1:1", "whatever", "ollama",
                              samples=0, sampling=model_pin.DEFAULT_SAMPLING,
                              timeout=1, offline=True)
    assert pin["reachable"] is False
    assert pin["unreachable_reason"]
    assert pin["model"]["resolved"] is None
    assert "throughput" not in pin


def test_the_pin_carries_no_hostname_or_cpu_brand():
    """This file is published. A throughput number needs a machine string to be
    interpretable; it does not need a hostname or a CPU brand."""
    pin = model_pin.build_pin("http://127.0.0.1:11434", "m", "ollama", 0,
                              model_pin.DEFAULT_SAMPLING, 1, offline=True)
    import platform
    assert platform.node() not in json.dumps(pin)


def test_a_missing_pin_is_absent_from_the_report_rather_than_assumed(tmp_path):
    loaded = framework_bench.load_pin(tmp_path / "nothing.json")
    assert loaded["present"] is False
    assert "model_pin.py" in loaded["reason"]
