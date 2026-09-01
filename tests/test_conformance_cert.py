"""Smoke test for the cross-runtime conformance certificate (roadmap item 306).

The tool (`tools/conformance_cert.py`) generates a signed JSON cert over a fixed
corpus of self-checking probes and verifies it WITHOUT re-running any tier. This
proves the three things a cert must do:

  1. It has the shape item 306 specifies (source_hash, ir_hash, tiers[], cases,
     passed, semantic_differences[], runtime_versions{}, + a signature).
  2. `verify` accepts a freshly generated cert with the right key.
  3. `verify` rejects a tampered cert (a flipped source_hash) — the signature
     covers the whole body, so no field can be altered undetected.

Every case runs on the `py` tier only: it needs no external toolchain, so the
test is deterministic on any box and never waits on cargo/javac/node. The
numbers in the cert are real — the corpus actually runs — never fabricated.
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "conformance_cert_under_test", ROOT / "tools" / "conformance_cert.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CERT = _load_tool()
KEY = b"conformance-cert-smoke-key"

# A tiny fixed corpus, independent of the tool's shipped default, so the test
# pins the contract rather than whatever the default corpus happens to be.
CORPUS = {
    "int arithmetic": (
        "pub fn add(a: Int, b: Int) -> Int { return a + b }\n"
        'test "adds" { assert add(2, 2) == 4 }\n'
    ),
    "list length": (
        'test "length" { assert [1, 2, 3].length() == 3 }\n'
        'test "value equality" { assert [1, 2] == [1, 2] }\n'
    ),
}


def _generate():
    return CERT.generate(CORPUS, ["py"], corpus_name="smoke", key=KEY,
                         now="2026-01-01T00:00:00+00:00")


def test_cert_has_the_item_306_shape():
    cert = _generate()
    for field in ("source_hash", "ir_hash", "tiers", "cases", "passed",
                  "semantic_differences", "runtime_versions", "signature",
                  "corpus_sources", "per_tier"):
        assert field in cert, f"cert is missing required member {field!r}"

    assert cert["kind"] == CERT.CERT_KIND
    assert cert["tiers"] == ["py"]                    # the one tier that ran
    assert isinstance(cert["semantic_differences"], list)
    assert isinstance(cert["runtime_versions"], dict)
    # `cases`/`passed` count case×tier EXECUTIONS: the two corpus cases each ran
    # once on py (a case's own `test` blocks are the RUNNER's business), both
    # passing — a real count, never invented.
    assert cert["case_count"] == 2
    assert cert["cases"] == 2 and cert["passed"] == 2
    assert cert["per_tier"]["py"] == {
        "ran": 2, "passed": 2, "skipped": 0, "refused": 0, "differences": 0}
    # a conforming corpus on a conforming tier has no divergence.
    assert cert["semantic_differences"] == []
    # the reference runtime's version is recorded (doctor-style probing).
    assert cert["runtime_versions"]["py"]["available"] is True


def test_verify_accepts_a_fresh_cert():
    ok, reason = CERT.verify(_generate(), KEY)
    assert ok, reason


def test_verify_rejects_a_wrong_key():
    ok, _ = CERT.verify(_generate(), b"not-the-signing-key")
    assert not ok


def test_verify_rejects_a_tampered_source_hash():
    cert = _generate()
    cert["source_hash"] = "0" * 64          # claim a different corpus identity
    ok, reason = CERT.verify(cert, KEY)
    assert not ok
    assert "signature" in reason or "source hash" in reason


def test_verify_rejects_a_tampered_pass_count():
    cert = _generate()
    cert["passed"] = 999                    # fabricate a pass count
    ok, _ = CERT.verify(cert, KEY)
    assert not ok, "the signature must cover the recorded counts"


def test_hashes_are_deterministic_and_bind_the_corpus():
    a, b = _generate(), _generate()
    assert a["source_hash"] == b["source_hash"]
    assert a["ir_hash"] == b["ir_hash"]
    # a different corpus hashes differently.
    other = dict(CORPUS)
    other["extra"] = 'test "t" { assert 1 == 1 }\n'
    assert CERT.source_hash(other) != a["source_hash"]
    assert CERT.ir_hash(other) != a["ir_hash"]
