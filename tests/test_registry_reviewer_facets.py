"""The three reviewer-facing evidence facets a bundle now carries: the
declared ceilings, the secret sinks a component can reach, and the compensation
class of each crossing (issue #2697, slice 3).

Each is assembled from a producer the audit already emits (`cardinality`,
`_secrets_table`, `_recovery_surface`), written beside the boundary, and bound by
a signed attestation like every other dossier.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from revl import registry, registry_evidence  # noqa: E402
from revl.compiler import compile_files  # noqa: E402

KEY = b"revl-registry-2697-facets-fixture-key"
NOW = "2026-01-01T00:00:00+00:00"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEDIATOR = os.path.join(ROOT, "examples", "mediator.rvl")
DURABLE = os.path.join(ROOT, "examples", "durable_log.rvl")

#: A composition that binds a secret to the one sink it may reach (the honest
#: host-body shape of `tests/test_secret_bound.py`), plus a component whose
#: boundary is unnameable (`*`): it could reach any declared sink.
SECRET_SOURCE = """
secret api_key for net.send

extern emission[net.send] fn send(m: Str) -> Int
= @py { return len(m) + len(api_key) }

service Ops { emission fn go(u: Str) -> Int }
service Relay { emission fn relay() -> Int }
service Ops2 { emission fn run() -> Int }

component A provides ops: Ops {
  provide ops {
    fn go(u) {
      let n = emit send(u)
      return 0
    }
  }
}

component Wrapper requires relay: Relay provides ops2: Ops2 {
  provide ops2 {
    fn run() -> Int {
      emit relay.relay()
      return 0
    }
  }
}
"""


def _build(tmp_path, name: str, source: str) -> tuple[str, str]:
    """Write one entry, regenerate the index, assemble its signed bundle."""
    reg = os.path.join(str(tmp_path), "registry")
    entry_dir = os.path.join(reg, "components", name)
    os.makedirs(entry_dir)
    with open(os.path.join(entry_dir, "component.rvl"), "w",
              encoding="utf-8") as fh:
        fh.write(source)
    registry.build_index(reg)
    registry_evidence.build_evidence(reg, key=KEY, now=NOW, signer="revl-ci")
    return reg, os.path.join(reg, "components", name)


def _ir(entry_dir: str) -> dict:
    return registry._normalize_ir_for_attest(
        compile_files([os.path.join(entry_dir, "component.rvl")]))


def test_the_three_reviewer_facets_are_written_and_bound(tmp_path):
    _reg, entry_dir = _build(tmp_path, "fs_mediator",
                             open(MEDIATOR, encoding="utf-8").read())
    ev = os.path.join(entry_dir, registry.EVIDENCE_DIRNAME)
    for filename in (registry.EVIDENCE_CEILINGS,
                     registry.EVIDENCE_SECRET_SINKS,
                     registry.EVIDENCE_COMPENSATION):
        assert os.path.exists(os.path.join(ev, filename)), filename

    bundle = registry.load_evidence_bundle(entry_dir)
    assert bundle.present()[-3:] == ("ceilings", "secret-sinks", "compensation")
    assert bundle.ceilings["kind"] == "revl.ceilings"
    ceilings = bundle.ceilings["cardinality"]["FsMediator"]["per_capability"]
    assert ceilings["fs"]["kind"] == "bounded"
    assert ceilings["fs"]["bound"] == 1

    # no secrets, no reversible crossing: the facets are present and honestly
    # empty, which is different from a facet that was never produced.
    assert bundle.secret_sinks == {"kind": "revl.secret-sinks",
                                   "secrets": [], "reach": {}}
    assert bundle.compensation == {"kind": "revl.compensation", "recovery": []}

    # a signed entry vouches for the reviewer facets too.
    bindings = bundle.attestation["evidence_bindings"]
    assert {"ceilings", "secret-sinks", "compensation"} <= set(bindings)
    assessment = registry.assess_evidence(bundle, key=KEY, ir=_ir(entry_dir))
    assert assessment.facets["ceilings"] == "present"
    assert assessment.facets["secret-sinks"] == "present"
    assert assessment.facets["compensation"] == "present"
    assert assessment.verified["ceilings"] is True


def test_a_bound_secret_reports_its_sink_and_the_component_that_reaches_it(tmp_path):
    _reg, entry_dir = _build(tmp_path, "net_agent", SECRET_SOURCE)
    bundle = registry.load_evidence_bundle(entry_dir)
    assert bundle.secret_sinks["secrets"] == [
        {"name": "api_key", "capability": "net.send"}]
    # A names the sink directly; Wrapper's `*` reach could be any declared sink,
    # so it is listed against this one too.
    assert bundle.secret_sinks["reach"] == {"A": ["net.send"],
                                           "Wrapper": ["net.send"]}


def test_a_tampered_reviewer_facet_breaks_the_attestation(tmp_path):
    """The facets are bound dossiers, not commentary: changing one after the
    signature makes the attestation `invalid`, so a ceiling or a sink cannot be
    edited under an honest signature."""
    _reg, entry_dir = _build(tmp_path, "fs_mediator",
                             open(MEDIATOR, encoding="utf-8").read())
    path = os.path.join(entry_dir, registry.EVIDENCE_DIRNAME,
                        registry.EVIDENCE_CEILINGS)
    doc = json.loads(open(path, encoding="utf-8").read())
    doc["cardinality"]["FsMediator"]["per_capability"]["fs"]["bound"] = 99
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    bundle = registry.load_evidence_bundle(entry_dir)
    assessment = registry.assess_evidence(bundle, key=KEY, ir=_ir(entry_dir))
    assert assessment.facets["attestation"] == "invalid"
    assert assessment.verified["ceilings"] is False


def test_a_recorded_inverse_reports_its_compensation_class(tmp_path):
    _reg, entry_dir = _build(tmp_path, "durable_log",
                             open(DURABLE, encoding="utf-8").read())
    bundle = registry.load_evidence_bundle(entry_dir)
    kinds = {row["kind"] for row in bundle.compensation["recovery"]}
    assert "inverse" in kinds
    # every row names the crossing and its declaration register (or None).
    for row in bundle.compensation["recovery"]:
        assert set(row) == {"name", "kind", "register"}
