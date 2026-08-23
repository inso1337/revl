"""The versioned composition-interchange format (roadmap item 28).

Proves the three things the format promises:

  * `revl audit --json` emits a versioned document (`schema_version` + `kind`),
  * that document validates against the published JSON Schema, and
  * an external harness can read provides/requires/reaches from it, and carry
    a gauntlet dossier in the same envelope, WITHOUT running revl a second
    time.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.audit_diff import audit_report  # noqa: E402
from revl.interchange import (  # noqa: E402
    INTERCHANGE_KIND,
    INTERCHANGE_VERSION,
    SCHEMA_PATH,
    load_schema,
    stamp,
    validate,
)

EXAMPLES = ROOT / "examples"


def _audit_json(name: str, capsys) -> dict:
    code = main(["audit", str(EXAMPLES / name), "--json"])
    assert code == 0
    return json.loads(capsys.readouterr().out)


# ----------------------------------------------------- version is stamped

def test_audit_json_carries_version_header(capsys):
    doc = _audit_json("user_cache.rvl", capsys)
    assert doc["schema_version"] == INTERCHANGE_VERSION
    assert doc["kind"] == INTERCHANGE_KIND
    # major.minor
    major, minor = doc["schema_version"].split(".")
    assert major.isdigit() and minor.isdigit()


def test_version_is_additive_body_unchanged(capsys):
    # the header is the ONLY addition — the existing body earlier consumers
    # read (manifest/boundary/externs/distributability) is byte-for-byte the
    # unstamped audit report.
    doc = _audit_json("uxprobe2_jobs.rvl", capsys)
    body = {k: v for k, v in doc.items()
            if k not in ("schema_version", "kind")}
    ir = compile_files([str(EXAMPLES / "uxprobe2_jobs.rvl")])
    assert body == audit_report(ir)


# ------------------------------------------------- validates against schema

@pytest.mark.parametrize("name", [
    "user_cache.rvl",     # emissions + capabilities, no externs
    "uxprobe2_jobs.rvl",  # declared externs, pure/acquire/emission classes
    "tenants.rvl",        # isolate + intercept on components
    "migrator.rvl",       # compensated + awaits
])
def test_audit_json_validates(name, capsys):
    doc = _audit_json(name, capsys)
    assert validate(doc) == []


def test_schema_file_is_published_and_parseable():
    assert SCHEMA_PATH.exists()
    schema = load_schema()
    assert schema["$id"].endswith("revl-interchange-v1.schema.json")
    assert "schema_version" in schema["required"]


def test_validator_rejects_a_broken_document(capsys):
    doc = _audit_json("user_cache.rvl", capsys)
    del doc["manifest"]                       # required member removed
    doc["schema_version"] = "not-a-version"   # violates the pattern
    errors = validate(doc)
    assert errors
    assert any("manifest" in e for e in errors)
    assert any("schema_version" in e for e in errors)


# --------------------------------------- consumer reads without running revl

def test_external_harness_reads_provides_requires_reaches(capsys):
    """The worked example from docs/interchange-format.md: a consumer answers
    provides/requires/reaches from the JSON alone."""
    doc = _audit_json("user_cache.rvl", capsys)

    # gate on MAJOR only, per the compatibility promise
    assert doc["kind"] == "revl.interchange"
    assert doc["schema_version"].split(".")[0] == "1"

    provider = next(c for c in doc["manifest"]["components"]
                    if "cache" in c["provides"])
    assert provider["name"] == "UserCache"
    assert provider["inject"] == ["db"]                       # requires
    reaches = doc["boundary"]["UserCache"]
    assert reaches["emissions"] == ["db.execute"]             # reaches
    assert [e["name"] for e in reaches["externs"]] == []


# ------------------------------------ composes with the gauntlet dossier (31)

def test_envelope_carries_a_gauntlet_dossier(capsys):
    ir = compile_files([str(EXAMPLES / "user_cache.rvl")])
    report = audit_report(ir)
    dossier = {"ok": True, "verdict": "admissible",
               "claimed": {"boundary": {"status": "ok"}}}
    doc = stamp(report, gauntlet=dossier)
    assert doc["schema_version"] == INTERCHANGE_VERSION
    assert doc["gauntlet"] == dossier
    # the combined envelope still validates: `gauntlet` is a reserved slot
    assert validate(doc) == []


def test_stamp_does_not_mutate_input():
    report = {"manifest": {"components": [], "loadOrder": []}, "boundary": {}}
    stamp(report)
    assert "schema_version" not in report
