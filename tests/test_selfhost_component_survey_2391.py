"""Issue #2391: a differential survey of the rust and java component surface.

For every `.rvl` document under `tests/fixtures` that declares a component, the
survey compiles it with the reference, emits it on the reference rust and java
backends and on the compiled self-host emitters, and classifies the pair:

  byte_agreement        the self-host output is the reference's bytes
  port_refusal          the self-host output carries a named `<<UNSUPPORTED...>>`
  reference_rejection   the reference refuses the document (nothing to compare)
  compile_rejection     the reference frontend refuses the document
  byte_divergence       the bytes differ and NO marker names the difference
  port_error            the port raised an unexpected error

`byte_divergence` is the silent difference the issue is about: a port that
answers with a module the reference would not, and says nothing. This test is
the ratchet: the only silent divergence allowed is the one on file, and the
day it is fixed the stale entry fails instead of waiving a document nobody
rereads.

Named refusals are loud and are tracked by their own issues (#2353, #2367,
#2383 and the per-tier ledgers); they are expected survey data here, not a
failure.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_survey():
    spec = importlib.util.spec_from_file_location(
        "component_survey_2391", ROOT / "tools" / "selfhost_differential_survey.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: The silent divergences the survey still finds, each on file. Emptying this
#: table is the goal; adding to it needs an issue number in the comment.
KNOWN_SILENT = {
    # a routed require on rust has no router struct and no marker (issue #2796)
    "tests/fixtures/emit_py_placement.rvl",
}


def _component_documents() -> list[str]:
    found = []
    for path in sorted((ROOT / "tests" / "fixtures").rglob("*.rvl")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "component " in text:
            found.append(path.relative_to(ROOT).as_posix())
    return found


@pytest.fixture(scope="module")
def surveyed():
    survey = _load_survey()
    report = survey.survey(["rust", "java"], _component_documents())
    counts = {}
    for row in report["results"]:
        counts.setdefault(row["tier"], {})
        counts[row["tier"]][row["status"]] = counts[row["tier"]].get(row["status"], 0) + 1
    return report, counts


def test_no_component_document_diverges_silently(surveyed):
    """Every difference between the self-host rust/java component output and
    the reference is either byte agreement, a reference refusal, or a NAMED
    port refusal, except the one on file."""
    report, counts = surveyed
    silent = sorted((row["document"], row["tier"]) for row in report["results"]
                    if row["status"] == "byte_divergence")
    errors = sorted((row["document"], row["tier"]) for row in report["results"]
                    if row["status"] == "port_error")
    known = sorted((document, tier) for document in KNOWN_SILENT
                   for tier in ("rust", "java"))
    assert len(report["results"]) >= 100, counts
    assert not errors, errors
    assert silent == known, (
        "silent component divergences (document, tier): %r; counts %r"
        % (silent, counts))


def test_every_port_refusal_names_the_difference(surveyed):
    """A `port_refusal` classification is only assigned when the self-host
    output carries a `<<UNSUPPORTED...>>` marker: the survey reads it off the
    output, so a marker-free divergence can never be filed here as a refusal."""
    report, _counts = surveyed
    for row in report["results"]:
        if row["status"] == "port_refusal":
            assert any(m.startswith("<<UNSUPPORTED") for m in row["markers"]), row["document"]
