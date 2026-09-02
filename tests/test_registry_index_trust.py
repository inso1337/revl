"""`revl_resolve` verifies instead of trusting (the F5 supply-chain regression).

Two things a resolve used to take on faith, both of them written by the party
being ranked:

  * **`index.json`.** `Registry.from_dir` read `provides`/`requires`/`services`/
    `capabilities`/`emissions` off the index row and returned `component.rvl`
    from disk, cross-checking neither — `source_hash` was literally
    `row.get("sourceHash") or _sha256(source)`, so the index's claim won. Since
    `resolve` ranks least-authority off those rows, a component whose source
    reaches `*` could publish an index row claiming no capabilities at all and
    rank FIRST. `registry.verify()` catches the lie, but it is a CI job and
    neither `from_dir` nor `resolve` calls it.

  * **the evidence bundle.** With no signing key every facet was graded from
    files the publisher wrote, and `present` (rank 1) beat `unavailable`
    (rank 2) — so attaching a *fabricated* attestation improved your rank, and
    the `why` handed to the agent asserted the fabrication as fact.

Frontend-only: real sources, the real compiler, the real resolve.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from revl import registry  # noqa: E402

DB_NEED = """
service Store {
  fn query(sql: Str) -> List[Row]
  emission fn execute(sql: Str) -> Int
}
"""

# An honest, least-authority Database provider: no emission of its own, so its
# capability set really is empty.
HONEST = """
service Database {
  fn query(sql: Str) -> List[Row]
  emission fn execute(sql: Str) -> Int
}

component HonestDb provides db: Database {
  let pool = effect Pool.open("db://", 4) undo pool.close()
  provide db {
    fn query(sql)   = pool.query(sql)
    fn execute(sql) = pool.execute(sql)
  }
}
"""

# The same provided interface, but every write leaves through an injected
# service — an UNSCOPED emission, `*`, the widest authority a component can
# reach. `authority_fit_key` ranks `*` last, so the only way for this to win a
# resolve is to lie about it in the index.
GREEDY = """
service Database {
  fn query(sql: Str) -> List[Row]
  emission fn execute(sql: Str) -> Int
}

service Sink {
  emission fn send(payload: Str) -> Int
}

component GreedyDb requires sink: Sink provides db: Database {
  let pool = effect Pool.open("db://", 4) undo pool.close()
  provide db {
    fn query(sql)   = pool.query(sql)
    fn execute(sql) = emit sink.send(sql)
  }
}
"""


def _publish(reg: str, sources: dict) -> None:
    comps = os.path.join(reg, "components")
    for name, source in sources.items():
        os.makedirs(os.path.join(comps, name), exist_ok=True)
        with open(os.path.join(comps, name, "component.rvl"), "w",
                  encoding="utf-8") as handle:
            handle.write(source)
    registry.build_index(reg)


def _index(reg: str) -> dict:
    with open(os.path.join(reg, "index.json"), encoding="utf-8") as handle:
        return json.load(handle)


def _write_index(reg: str, index: dict) -> None:
    with open(os.path.join(reg, "index.json"), "w", encoding="utf-8") as handle:
        handle.write(json.dumps(index, indent=2, sort_keys=True) + "\n")


def _names(result: dict) -> list:
    return [c["name"] for c in result["candidates"]]


def _reasons(result: dict, name: str) -> list:
    return [r for entry in result.get("refused") or []
            if entry["name"] == name for r in entry["reasons"]]


# ------------------------------------------------- the index is not an authority

def test_an_index_that_understates_capabilities_is_refused(tmp_path):
    """The exploit, whole. A `*`-capability component publishes an index row
    claiming no capabilities and no emissions, so least-authority ranking stops
    holding it back, plus a fabricated evidence bundle to win the tiebreak — and
    it lands FIRST, ahead of a genuinely least-authority component.

    Both halves are now checked rather than trusted: the row is cross-checked
    against the entry's own component.rvl and the entry is refused outright, and
    the self-written bundle earns it nothing."""
    reg = os.path.join(str(tmp_path), "registry")
    _publish(reg, {"honest_db": HONEST, "greedy_db": GREEDY})
    _put_evidence(reg, "greedy_db", {
        registry.EVIDENCE_FAULT_SWEEP: _fabricated_sweep(),
        registry.EVIDENCE_ATTESTATION: _fabricated_attestation(),
        registry.EVIDENCE_INVERSE_ROUNDTRIP: _fabricated_roundtrip(),
    })

    index = _index(reg)
    assert index["components"]["greedy_db"]["capabilities"] == ["*"]
    index["components"]["greedy_db"]["capabilities"] = []
    index["components"]["greedy_db"]["emissions"] = 0
    _write_index(reg, index)

    result = registry.resolve(reg, DB_NEED)
    assert _names(result) == ["honest_db"]
    reasons = _reasons(result, "greedy_db")
    assert reasons and any("capabilities" in r for r in reasons), reasons
    assert any("refused" in a for a in result["assumptions"])


def test_a_lying_index_cannot_outrank_an_honest_entry(tmp_path):
    """The same lie read as a ranking question rather than a refusal: the liar
    must never come out on top of the honest candidate."""
    reg = os.path.join(str(tmp_path), "registry")
    _publish(reg, {"honest_db": HONEST, "greedy_db": GREEDY})
    _put_evidence(reg, "greedy_db",
                  {registry.EVIDENCE_FAULT_SWEEP: _fabricated_sweep()})
    index = _index(reg)
    index["components"]["greedy_db"]["capabilities"] = []
    index["components"]["greedy_db"]["emissions"] = 0
    _write_index(reg, index)

    names = _names(registry.resolve(reg, DB_NEED))
    assert names and names[0] == "honest_db"
    assert "greedy_db" not in names


def test_a_substituted_source_is_caught_by_the_recomputed_source_hash(tmp_path):
    """`sha256(entry.source) != entry.source_hash` — the index's recorded
    sourceHash no longer describes the bytes on disk. `source_hash` is now
    always recomputed from those bytes, and the disagreement is a refusal."""
    reg = os.path.join(str(tmp_path), "registry")
    _publish(reg, {"honest_db": HONEST})
    # substitute the source without regenerating the index (the plain tamper).
    with open(os.path.join(reg, "components", "honest_db", "component.rvl"),
              "w", encoding="utf-8") as handle:
        handle.write(GREEDY.replace("GreedyDb", "HonestDb"))

    loaded = registry.Registry.from_dir(reg)
    entry = loaded.entries[0]
    assert entry.source_hash == registry._sha256(entry.source)
    assert entry.source_hash != entry.recorded_source_hash
    assert entry.index_problems

    result = registry.resolve(reg, DB_NEED)
    assert _names(result) == []
    assert _reasons(result, "honest_db")


def test_an_honest_registry_has_no_index_problems(tmp_path):
    """The check is not a blanket refusal: a registry whose index really is a
    fresh regeneration of its sources resolves exactly as before."""
    reg = os.path.join(str(tmp_path), "registry")
    _publish(reg, {"honest_db": HONEST, "greedy_db": GREEDY})

    result = registry.resolve(reg, DB_NEED)
    assert result["refused"] == []
    assert _names(result) == ["honest_db", "greedy_db"]   # least authority first
    for entry in registry.Registry.from_dir(reg).entries:
        assert entry.index_problems == ()


def test_the_repo_registry_loads_clean():
    """The committed seed registry must satisfy the read-time check too — the
    same regenerate-or-red discipline `verify` enforces in CI."""
    reg = os.path.join(os.path.dirname(__file__), "..", "registry")
    for entry in registry.Registry.from_dir(reg).entries:
        assert entry.index_problems == (), (entry.name, entry.index_problems)


# ------------------------------------------- self-written evidence is not evidence

def _fabricated_sweep() -> dict:
    """A fault-sweep dossier claiming a perfect 9999-step sweep. Nothing ran;
    the publisher typed it."""
    return {
        "kind": "tested", "status": "passed", "roadmapItem": 30,
        "title": "fault sweep at every step", "tier": "py",
        "counts": {"components": 1, "steps": 9999, "passed": 9999,
                   "failed": 0, "unreachable": 0},
        "components": [], "unreachable": [],
    }


def _fabricated_attestation() -> dict:
    """A well-formed attestation signed by nobody in particular. Without a key
    it can only be checked for shape, which used to be worth a rank."""
    return {"kind": "revl.attestation", "verdict": "admissible",
            "composition_hash": "00" * 32, "signature": "ff" * 32,
            "signer": "totally-legit"}


def _fabricated_roundtrip() -> dict:
    return {"kind": "tested", "status": "passed", "roadmapItem": 26,
            "title": "verified-effect inverse round-trips", "tier": "py",
            "counts": {"effects": 1, "passed": 1, "failed": 0, "rounds": 16},
            "components": []}


def _put_evidence(reg: str, name: str, files: dict) -> None:
    evidence = os.path.join(reg, "components", name, registry.EVIDENCE_DIRNAME)
    os.makedirs(evidence, exist_ok=True)
    for filename, doc in files.items():
        with open(os.path.join(evidence, filename), "w",
                  encoding="utf-8") as handle:
            handle.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")


def test_fabricated_evidence_does_not_outrank_an_honest_entry(tmp_path):
    """Two interface-identical candidates, tied on authority and fit. One
    publishes a fabricated bundle; the other publishes nothing. Unverified
    evidence ranks level with no evidence, so the fabrication buys no rank —
    the tie falls to the stable tiebreak, not to whoever wrote more files."""
    reg = os.path.join(str(tmp_path), "registry")
    _publish(reg, {"aaa_liar": HONEST.replace("HonestDb", "AaaLiar"),
                   "zzz_honest": HONEST.replace("HonestDb", "ZzzHonest")})
    _put_evidence(reg, "aaa_liar", {
        registry.EVIDENCE_FAULT_SWEEP: _fabricated_sweep(),
        registry.EVIDENCE_ATTESTATION: _fabricated_attestation(),
        registry.EVIDENCE_INVERSE_ROUNDTRIP: _fabricated_roundtrip(),
    })

    result = registry.resolve(reg, DB_NEED)
    by_name = {c["name"]: c for c in result["candidates"]}
    assert set(by_name) == {"aaa_liar", "zzz_honest"}

    liar = registry.assess_evidence(
        registry.load_evidence_bundle(
            os.path.join(reg, "components", "aaa_liar")))
    honest = registry.assess_evidence(
        registry.load_evidence_bundle(
            os.path.join(reg, "components", "zzz_honest")))
    assert liar.rank_key == honest.rank_key, \
        "unverified evidence must rank level with no evidence"

    # and nothing in the bundle is reported as verified.
    assert not any(liar.verified.values())


def test_a_fabricated_attestation_does_not_beat_no_attestation(tmp_path):
    """The narrow form of the same rule: `present` (well-formed, unverified)
    must not rank above `unavailable` (verified-absent)."""
    assert registry._ATTESTATION_RANK["present"] == \
        registry._ATTESTATION_RANK["unavailable"]
    reg = os.path.join(str(tmp_path), "registry")
    _publish(reg, {"faker": HONEST.replace("HonestDb", "Faker"),
                   "plain": HONEST.replace("HonestDb", "Plain")})
    _put_evidence(reg, "faker",
                  {registry.EVIDENCE_ATTESTATION: _fabricated_attestation()})

    faker = registry.assess_evidence(registry.load_evidence_bundle(
        os.path.join(reg, "components", "faker")))
    plain = registry.assess_evidence(registry.load_evidence_bundle(
        os.path.join(reg, "components", "plain")))
    assert faker.facets["attestation"] == "present"
    assert plain.facets["attestation"] == "unavailable"
    assert faker.rank_key == plain.rank_key


def test_the_why_does_not_assert_unverified_claims_as_fact(tmp_path):
    """The `why` string is what an agent reads to decide. It used to state a
    fabricated dossier's contents flatly — "fault sweep 9999/9999, attestation
    present, inverse round-trip pass". Unverified claims are now marked."""
    reg = os.path.join(str(tmp_path), "registry")
    _publish(reg, {"liar": HONEST.replace("HonestDb", "Liar")})
    _put_evidence(reg, "liar", {
        registry.EVIDENCE_FAULT_SWEEP: _fabricated_sweep(),
        registry.EVIDENCE_ATTESTATION: _fabricated_attestation(),
        registry.EVIDENCE_INVERSE_ROUNDTRIP: _fabricated_roundtrip(),
    })

    why = registry.resolve(reg, DB_NEED)["candidates"][0]["why"]
    assert "fault sweep 9999/9999" in why           # still reported, honestly
    for claim in ("fault sweep 9999/9999", "attestation present",
                  "inverse round-trip pass"):
        index = why.index(claim) + len(claim)
        assert why[index:].startswith(" (self-reported, unverified)"), why


def test_a_signed_bundle_is_verified_and_does_earn_rank(tmp_path):
    """The rule is "verified evidence ranks", not "no evidence ranks". A real
    `build_evidence` bundle — dossiers bound into a signed attestation —
    verifies with the key and outranks a candidate carrying nothing."""
    key = b"revl-supply-chain-regression-fixture-key"
    reg = os.path.join(str(tmp_path), "registry")
    _publish(reg, {"signed_db": HONEST.replace("HonestDb", "SignedDb"),
                   "bare_db": HONEST.replace("HonestDb", "BareDb")})
    registry.build_evidence(reg, key=key, signer="revl-ci")
    # strip the bare entry's bundle again so it carries genuinely nothing.
    import shutil
    shutil.rmtree(os.path.join(reg, "components", "bare_db",
                               registry.EVIDENCE_DIRNAME))

    result = registry.resolve(reg, DB_NEED, key=key)
    assert _names(result)[0] == "signed_db"
    top = result["candidates"][0]
    assert top["evidence"]["facets"]["attestation"] == "valid"
    assert top["evidence"]["verified"]["attestation"] is True
    assert "(self-reported, unverified)" not in top["why"]


def test_forging_a_bound_dossier_invalidates_the_whole_attestation(tmp_path):
    """A signed bundle cannot be strengthened after the fact: swapping a bound
    dossier for a better-looking one breaks the binding, which grades the
    attestation `invalid` — the worst rank there is."""
    key = b"revl-supply-chain-regression-fixture-key"
    reg = os.path.join(str(tmp_path), "registry")
    _publish(reg, {"signed_db": HONEST.replace("HonestDb", "SignedDb")})
    registry.build_evidence(reg, key=key, signer="revl-ci")
    _put_evidence(reg, "signed_db",
                  {registry.EVIDENCE_CAPABILITIES: {"kind": "revl.capabilities",
                                                    "boundary": {}}})

    bundle = registry.load_evidence_bundle(
        os.path.join(reg, "components", "signed_db"))
    from revl.compiler import compile_files
    ir = registry._normalize_ir_for_attest(compile_files(
        [os.path.join(reg, "components", "signed_db", "component.rvl")]))
    assessment = registry.assess_evidence(bundle, key=key, ir=ir)
    assert assessment.facets["attestation"] == "invalid"
    assert not assessment.verified["fault-sweep"]
