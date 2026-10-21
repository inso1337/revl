"""Issue #2730, slice 1: the attested composition's identity in the trace.

The runtime metrics and the OTel spans describe what happened but not which
composition or policy it happened under. `attest.canonical_hash` computes the
composition hash and `attest.ruleset_digest` the ruleset digest at admission,
but the run trace carried neither, nor the attestation key id.

This slice records the three values in the run trace's FIRST event and copies
them to every span as `revl.composition_hash`, `revl.attestation_key_id` and
`revl.ruleset_digest`. They are present only when the run was given an
attestation that binds THIS composition, and absent otherwise, with no
placeholder. A span attribute is a label, not proof: a consumer that wants
proof verifies the attestation itself.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from revl import RevlError, attest  # noqa: E402
from revl import metrics as mt  # noqa: E402
from revl import otel as ot  # noqa: E402
from revl import run  # noqa: E402
from revl import why_runtime as wr  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

KEY = b"2730-attestation-key-material"


def _attestation(tmp_path, *, source=None, paths=None, name="attestation.json"):
    """A real signed attestation over the given composition."""
    verdict = attest.run_gate(source=source, paths=paths)
    assert verdict.admitted, verdict.reason
    document = attest.make_attestation(
        verdict.ir, KEY, verdict=verdict,
        now="2026-10-20T00:00:00+00:00")
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return document, path


def _bare_driver(run_header=None):
    driver = run._Driver.__new__(run._Driver)
    driver.runtime = None
    driver.generation = 1
    driver._events = []
    driver._seq = 0
    driver._run_header = dict(run_header or {})
    return driver


HEADER = {"compositionHash": "a" * 64,
          "attestationKeyId": "0123456789abcdef",
          "rulesetDigest": "b" * 64}


def test_the_header_rides_only_the_first_event():
    """Recorded once, on the first event: a later event cannot claim a
    different admission, and the file has exactly one header."""
    driver = _bare_driver(HEADER)
    driver._record(wr.LOAD, "A", "PENDING -> ACTIVE", wr.cause_boot())
    driver._record_emit("A", "fs", "A.write", wr.cause_trigger("crossed"))
    first, second = driver._events
    assert first["run"] == HEADER
    assert "run" not in second


def test_every_span_carries_the_attested_run_attributes():
    """The acceptance shape: an attested composition with tracing carries the
    three attributes on every span, the synthetic run root included."""
    driver = _bare_driver(HEADER)
    driver._record(wr.LOAD, "A", "PENDING -> ACTIVE", wr.cause_boot())
    driver._record_emit("A", "fs", "A.write", wr.cause_trigger("crossed"))
    spans = ot.build_spans(driver._events)
    assert len(spans) == 3  # the run root, one load span, one emit span
    for span in spans:
        assert span.attributes["revl.composition_hash"] == HEADER["compositionHash"]
        assert span.attributes["revl.attestation_key_id"] == HEADER["attestationKeyId"]
        assert span.attributes["revl.ruleset_digest"] == HEADER["rulesetDigest"]


def test_an_unattested_run_has_no_header_and_no_attributes():
    driver = _bare_driver()
    driver._record(wr.LOAD, "A", "PENDING -> ACTIVE", wr.cause_boot())
    assert "run" not in driver._events[0]
    for span in ot.build_spans(driver._events):
        assert "revl.composition_hash" not in span.attributes
        assert "revl.attestation_key_id" not in span.attributes
        assert "revl.ruleset_digest" not in span.attributes


def test_the_header_omits_a_value_the_attestation_does_not_carry():
    """Older attestations have no `checker` identity; the header then has no
    ruleset digest rather than a placeholder."""
    header = wr.run_header(composition_hash="c" * 64, attestation_key_id=None,
                           ruleset_digest=None)
    assert header == {"compositionHash": "c" * 64}
    assert wr.run_header() == {}


def test_metrics_reports_the_composition_it_grouped_by():
    driver = _bare_driver(HEADER)
    driver._record(wr.LOAD, "A", "PENDING -> ACTIVE", wr.cause_boot())
    document = mt.compute_metrics(driver._events)
    assert document["run"] == HEADER
    rendered = mt.render(document)
    assert HEADER["compositionHash"] in rendered
    assert HEADER["attestationKeyId"] in rendered
    # and an unattested run's document has no run block at all
    bare = _bare_driver()
    bare._record(wr.LOAD, "A", "PENDING -> ACTIVE", wr.cause_boot())
    assert "run" not in mt.compute_metrics(bare._events)
    assert "run:" not in mt.render(mt.compute_metrics(bare._events))


def test_a_matching_attestation_yields_the_three_values(tmp_path):
    source = "service Empty { fn ping() -> Str }\n"
    document, path = _attestation(tmp_path, source=source)
    ir = compile_source(source, "<attest>")
    header = run._load_run_attestation(path, ir)
    assert header == {
        "compositionHash": document["composition_hash"],
        "attestationKeyId": document["key_id"],
        "rulesetDigest": document["checker"]["ruleset"],
    }


def test_an_attestation_for_another_composition_is_refused(tmp_path):
    """An attestation for a different composition would make the trace claim an
    admission that never covered this run; it is refused, not relabelled."""
    document, path = _attestation(
        tmp_path, source="service Other { fn ping() -> Str }\n")
    other = compile_source("service Mine { fn ping() -> Str }\n", "<attest>")
    with pytest.raises(RevlError) as exc:
        run._load_run_attestation(path, other)
    message = str(exc.value)
    assert "different composition" in message
    assert document["composition_hash"][:12] in message


def test_a_non_attestation_document_is_refused(tmp_path):
    path = tmp_path / "notes.json"
    path.write_text(json.dumps({"kind": "something-else"}), encoding="utf-8")
    with pytest.raises(RevlError) as exc:
        run._load_run_attestation(path, {})
    assert "not a revl attestation" in str(exc.value)


def test_run_command_checks_the_attestation_before_booting(tmp_path, capsys):
    """The CLI wiring: a matching attestation is accepted (this composition has
    no components, so the run returns before a runtime import), and a
    mismatched one refuses with its own diagnostic."""
    from types import SimpleNamespace

    attested = "service Attested { fn ping() -> Str }\n"
    _, good = _attestation(tmp_path, source=attested, name="good.json")
    _, wrong = _attestation(
        tmp_path, source="service Elsewhere { fn ping() -> Str }\n",
        name="wrong.json")
    source = tmp_path / "attested.rvl"
    source.write_text(attested, encoding="utf-8")

    base = dict(files=[str(source)], backend="py", placement=None)
    rc = run.run_command(SimpleNamespace(**base, attestation=str(good)))
    captured = capsys.readouterr()
    assert rc == 0
    assert "nothing to run" in captured.err
    # without --trace the header has nowhere to go, and the run says so
    assert "add --trace FILE" in captured.err

    rc = run.run_command(SimpleNamespace(**base, attestation=str(wrong)))
    assert rc == 1
    assert "different composition" in capsys.readouterr().err


def test_attestation_is_refused_with_placement(tmp_path, capsys):
    """A placement run has no single causal trace, so the header has nowhere to
    go; the pair is refused rather than accepting a flag that records nothing."""
    from types import SimpleNamespace

    source = tmp_path / "attested.rvl"
    source.write_text("service Attested { fn ping() -> Str }\n", encoding="utf-8")
    args = SimpleNamespace(files=[str(source)], backend="py",
                           placement="placement.toml",
                           attestation=str(tmp_path / "unused.json"))
    assert run.run_command(args) == 2
    assert "--placement" in capsys.readouterr().err
