"""`revl_resolve` over the git-backed seed index (roadmap item 49, phase 0).

These are the exit tests of docs/registry.md §6 for the read path:

* a need resolves to the §5-compatible providers, ranked, with an incompatible
  candidate filtered out — *by shape*, from a differently-named service
  declaration, proving the match is admission and not text;
* a fill spec (item 32) passed verbatim as the need resolves;
* every candidate carries its source and manifest inline, so need -> resolve ->
  admit is two round-trips and never a third fetch;
* a `manifest` that already provides the key withholds the candidate (G2);
* the committed index and manifests are current, and tampering with either
  turns the check red (the regenerate-or-red discipline).

The predicate under test is the *real* §5 gate: `test_resolve_agrees_with_the_
real_admission_gate` cross-checks resolve's verdict against
`compile_source(candidate, manifest=running)` — the same entry point the runtime
admits through — over the search-as-admission probe corpus.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from revl import RevlError, compile_files, compile_source  # noqa: E402
from revl import registry  # noqa: E402
from revl.mcp import fillspec  # noqa: E402

REGISTRY_DIR = os.path.join(os.path.dirname(__file__), "..", "registry")

# A Database need whose service is declared under a *different* name (`Store`),
# so a name-matching resolver would find nothing. Its full surface is what the
# providers are filtered against.
DATABASE_NEED = """
service Store {
  fn query(sql: Str) -> List[Row]
  emission fn execute(sql: Str) -> Int
}
"""

# A Cache need, likewise differently-named (`KV`).
CACHE_NEED = """
service KV {
  fn get(key: Str) -> Opt[Str]
  emission fn put(key: Str, value: Str)
}
"""


def _registry() -> registry.Registry:
    return registry.Registry.from_dir(REGISTRY_DIR)


def _names(result: dict) -> list[str]:
    return [c["name"] for c in result["candidates"]]


# --------------------------------------------------------------- resolve

def test_resolve_ranks_compatible_providers_and_filters_the_rest():
    result = _registry().resolve(DATABASE_NEED)
    names = _names(result)
    # every §5-compatible Database provider is returned ...
    assert "pg_database" in names
    assert "mysql_database" in names
    assert "audited_database" in names   # adds ping() — a compatible superset
    # ... and the one that removes `execute` (breaking the need's call site) is
    # not: the gate refuses it, so resolve never surfaces it.
    assert "readonly_database" not in names
    assert result["precision"] == "exact"
    assert result["query"] == "resolve"


def test_resolve_finds_user_cache_from_a_cache_shaped_need_by_shape():
    result = _registry().resolve(CACHE_NEED)
    # user_cache provides `cache: Cache`; the need declared `service KV`. A
    # match here can only be structural — there is no textual overlap.
    assert _names(result) == ["user_cache"]


def test_candidates_carry_source_and_manifest_inline():
    # the two-round-trip contract: resolve -> admit, source never re-fetched.
    top = _registry().resolve(DATABASE_NEED)["candidates"][0]
    assert "service Database" in top["source"]
    assert top["manifest"]["kind"] == "revl.interchange"
    # the inline source is admissible as-is: feed it straight to the gate.
    running = compile_files([os.path.join(os.path.dirname(__file__),
                                          "..", "examples", "user_cache.rvl")])
    compile_source(top["source"], "<imported>.rvl",
                   manifest=running, replacing=("PgDatabase",))


def test_ranking_is_least_authority_then_fit_then_evidence():
    names = _names(_registry().resolve(DATABASE_NEED))
    # the exact providers (empty capability set, identical interface) rank above
    # the superset provider (audited adds a method — a compatible widening).
    assert names.index("audited_database") == len(names) - 1
    # mysql and pg tie on authority and fit, and now on evidence too: the seed
    # registry ships no attestation, so mysql's gauntlet dossier is an
    # unverified self-report and buys it no rank (a dossier a publisher wrote
    # about itself is a claim, not a check). What separates them below is the
    # stable tiebreak - shorter source, then name - not an evidence verdict.
    assert names.index("mysql_database") < names.index("pg_database")
    by_name = {e.name: e for e in _registry().entries}
    assert len(by_name["mysql_database"].source) < len(by_name["pg_database"].source)


def test_manifest_withholds_a_key_the_composition_already_provides():
    # admissible-here beats compatible-somewhere: G2 forbids two providers of a
    # key, so a running composition that already provides `db` gets nothing back.
    manifest = {"manifest": {"components": [{"name": "X", "provides": ["db"]}]}}
    result = _registry().resolve(DATABASE_NEED, manifest=manifest)
    assert result["candidates"] == []
    assert any("G2" in a for a in result["assumptions"])


def test_a_fill_spec_from_a_real_hole_resolves():
    hole_src = """
    service Database {
      fn query(sql: Str) -> List[Row]
      emission fn execute(sql: Str) -> Int
    }
    service Report { fn rows() -> List[Row] }
    component Reporter requires db: Database provides report: Report {
      provide report {
        fn rows() = hole[List[Row]] "the rows to report"
      }
    }
    """
    ir = compile_source(hole_src, "hole.rvl")
    obligation = fillspec.enrich(ir)[0]
    reg = _registry()
    # the fill spec verbatim, and the whole obligation, both resolve to the
    # providers of the service the hole reaches (Database).
    for need in (obligation["fillSpec"], obligation):
        names = _names(reg.resolve(need))
        assert "pg_database" in names and "readonly_database" not in names


def test_limit_caps_the_candidate_count():
    result = _registry().resolve(DATABASE_NEED, limit=1)
    assert len(result["candidates"]) == 1


# --------------------------------------------------------------- index integrity

def test_committed_index_and_manifests_are_current():
    # the seed index in the repo must be a fresh regeneration of its sources,
    # and every manifest reproducible from its component.rvl.
    assert registry.verify(REGISTRY_DIR) == []


def test_hand_editing_the_index_turns_the_check_red():
    with tempfile.TemporaryDirectory() as tmp:
        dst = os.path.join(tmp, "registry")
        shutil.copytree(REGISTRY_DIR, dst)
        index_path = os.path.join(dst, "index.json")
        index = json.loads(open(index_path).read())
        # a hand-edit the generator would never produce
        index["components"]["pg_database"]["capabilities"] = ["forged"]
        open(index_path, "w").write(json.dumps(index, indent=2) + "\n")
        problems = registry.verify(dst)
        assert problems and any("index.json" in p for p in problems)


def test_tampering_with_a_manifest_turns_the_check_red():
    with tempfile.TemporaryDirectory() as tmp:
        dst = os.path.join(tmp, "registry")
        shutil.copytree(REGISTRY_DIR, dst)
        mpath = os.path.join(dst, "components", "pg_database", "manifest.json")
        manifest = json.loads(open(mpath).read())
        manifest["boundary"] = {"forged": {"emissions": ["evil.exfiltrate"]}}
        open(mpath, "w").write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        problems = registry.verify(dst)
        assert any("pg_database" in p for p in problems)


# --------------------------------------------------------------- the real gate

def test_resolve_agrees_with_the_real_admission_gate():
    """resolve's §5 verdict is the runtime's. For each seed Database provider,
    compare resolve's include/exclude decision against what the *actual*
    admission gate does when that provider is hot-swapped against a running
    UserCache — `compile_source(candidate, manifest=running, replacing=...)`,
    the same call docs/registry-probe.md proved and the runtime uses."""
    running = compile_files([os.path.join(os.path.dirname(__file__),
                                          "..", "examples", "user_cache.rvl")])
    resolved = set(_names(_registry().resolve(DATABASE_NEED)))
    for entry in _registry().entries:
        if "Database" not in entry.provides.values():
            continue
        try:
            compile_source(entry.source, "<candidate>.rvl",
                           manifest=running, replacing=("PgDatabase",))
            gate_admits = True
        except RevlError:
            gate_admits = False
        assert (entry.name in resolved) == gate_admits, entry.name
