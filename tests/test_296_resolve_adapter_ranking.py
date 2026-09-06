"""Item 296, slice 3: `compatible-with-adapter` on the RESOLVE side.

Slice 1 shipped the predicate and `revl adapt`. The resolver knew nothing about
it: a candidate the §5 filter refused simply vanished, and an agent had to guess
which of the entries it never saw might be one hand-written wrapper away. These
are the design's section 3 / 6.1 / 6.4 exit tests for the read path:

* a candidate the direct filter refuses is reported `compatible-with-adapter`,
  carrying the bridge plan, the rendered section-4 artifact, the derivation
  hash, and the wiring the author must commit - PROPOSED, never wired (E1);
* without the author's `adapt` opt-in the same pair is a NAMED near miss - the
  clause, the position, and the item-274 `navigate` record - so "fix it by
  hand" starts from the exact position (design section 5);
* the rendered artifact re-admits through the ordinary, unmodified gate;
* ranking: a directly compatible candidate outranks an adapted one at equal
  authority fit (the bridge is a cost, not a tie, section 6.1), and a CHAIN -
  a bridge in front of a committed adapter - ranks below a fresh single bridge
  onto the underlying candidate, read off the machine-readable derivation
  marking (E12, section 6.4);
* a plan that merges outcomes discounts the candidate's error-semantics
  evidence class and flags the inversion, because a fault sweep attesting
  "errors surface as `Err`" describes exactly what the consumer stops seeing
  behind the merge (E13, section 6.1).

Frontend-only: everything here compiles and resolves in-process, no runtime.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from revl import attest  # noqa: E402
from revl import registry  # noqa: E402
from revl.adapt import adapter_marking, chain_depth_for  # noqa: E402
from revl.compiler import compile_source  # noqa: E402

# A fixture signing key - obviously a test key. It only signs the throwaway
# attestations these tests build in tmp registries.
KEY = b"revl-registry-296-adapter-fixture-key"
NOW = "2026-01-01T00:00:00+00:00"

# The item's own pair, in the shape the design's E1 states it: the consumer
# wants `get(Key) -> Opt[Value]`, the candidate provides
# `get(Key, Options) -> Result[Value, Error]`.
NEED = """
service KV {
  fn get(key: Str) -> Opt[Str]
}
"""

_TYPES = """
type Options = { seed: Opt[Str] }
type Error = { code: Str }
"""

# The candidate the §5 filter refuses: extra parameter, Result return.
VENDOR = _TYPES + """
service VendorCache {
  fn get(key: Str, options: Options) -> Result[Str, Error]
}

component VendorCacheImpl provides cache: VendorCache {
  provide cache { fn get(key, options) = Ok(key) }
}
"""

# The same arity gap with NO outcome merge: only a B1 absence-shaped default.
PLAIN_VENDOR = _TYPES + """
service PlainVendor {
  fn get(key: Str, options: Options) -> Opt[Str]
}

component PlainVendorImpl provides cache: PlainVendor {
  provide cache { fn get(key, options) = None }
}
"""

# A DIRECTLY compatible provider, and deliberately a compatible SUPERSET (it
# adds `ping`) rather than an exact match: that way it ties the adapted
# candidates on the interface-fit term, and the direct-over-adapted bit is the
# only thing left that can decide the order.
DIRECT_CACHE = """
service Cache {
  fn get(key: Str) -> Opt[Str]
  fn ping() -> Str
}

component DirectCacheImpl provides cache: Cache {
  provide cache {
    fn get(key) = None
    fn ping()   = "ok"
  }
}
"""

# A COMMITTED adapter: ordinary source that happens to carry the section-4
# derivation marking. A bridge in front of it is a chain of depth 2.
_FAKE_DERIVATION = "d" * 64
COMMITTED_ADAPTER = _TYPES + f"""
type Scope = {{ name: Opt[Str] }}

service PlainVendor {{
  fn get(key: Str, options: Options) -> Opt[Str]
}}

service ScopedCache {{
  fn get(key: Str, scope: Scope) -> Opt[Str]
}}

// generated: revl adapt cache from backing
// derivation: sha256:{_FAKE_DERIVATION} catalogue-v1 depth=1
component ScopedCacheAdapter requires backing: PlainVendor
    provides cache: ScopedCache {{
  provide cache {{ fn get(key, scope) = backing.get(key, {{seed: None}}) }}
}}
"""

# The author's `D`: the total waiver the design's E1 opts into, keyed by method
# name exactly as `revl adapt --adapt` takes it.
TOTAL_WAIVER = {"get": {"return": {"merge": "total"}}}


# --------------------------------------------------------------- fixtures

def _write(comp_dir: str, source: str) -> None:
    os.makedirs(comp_dir, exist_ok=True)
    with open(os.path.join(comp_dir, "component.rvl"), "w",
              encoding="utf-8") as handle:
        handle.write(source)


def _attestation_for(comp_dir: str) -> dict:
    """A real `revl.attest` attestation over the rebuilt IR, BINDING whatever
    evidence is already on disk. The binding is the whole point: a dossier
    nothing signed vouches for nothing (item 290 §6.2), so without it a fault
    sweep is worth no rank at all and the discount would have nothing to bite
    on."""
    verdict = attest.run_gate(
        paths=[os.path.join(comp_dir, "component.rvl")],
        normalize=registry._normalize_ir_for_attest)
    ir = registry._normalize_ir_for_attest(verdict.ir)
    bundle = registry.load_evidence_bundle(comp_dir)
    return attest.make_attestation(
        ir, KEY, verdict=verdict, now=NOW, signer="revl-ci",
        evidence_bindings=registry.evidence_bindings(bundle))


def _sweep(passed: int, steps: int) -> dict:
    """A fault-sweep dossier in `fault.sweep_dossier`'s real shape."""
    return {
        "kind": "tested", "status": "passed" if passed == steps else "failed",
        "roadmapItem": 30, "title": "fault sweep at every step", "tier": "py",
        "counts": {"components": 1, "steps": steps, "passed": passed,
                   "failed": steps - passed, "unreachable": 0},
        "components": [], "unreachable": [],
    }


def _build(tmp_path, components: dict) -> str:
    """`components` maps name -> (source, evidence dict). `"ATTEST"` as an
    evidence value is resolved to a real attestation over the built entry."""
    reg = os.path.join(str(tmp_path), "registry")
    comps = os.path.join(reg, "components")
    os.makedirs(comps, exist_ok=True)
    for name, (source, _evidence) in components.items():
        _write(os.path.join(comps, name), source)
    registry.build_index(reg)
    for name, (_source, evidence) in components.items():
        if not evidence:
            continue
        comp_dir = os.path.join(comps, name)
        ev_dir = os.path.join(comp_dir, registry.EVIDENCE_DIRNAME)
        os.makedirs(ev_dir, exist_ok=True)

        def _put(filename: str, doc: dict) -> None:
            with open(os.path.join(ev_dir, filename), "w",
                      encoding="utf-8") as handle:
                handle.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")

        # the dossiers first, so the attestation can BIND them.
        for filename, doc in evidence.items():
            if doc != "ATTEST":
                _put(filename, doc)
        if evidence.get(registry.EVIDENCE_ATTESTATION) == "ATTEST":
            _put(registry.EVIDENCE_ATTESTATION, _attestation_for(comp_dir))
    return reg


def _names(result: dict) -> list:
    return [c["name"] for c in result["candidates"]]


def _by_name(result: dict, name: str) -> dict:
    return next(c for c in result["candidates"] if c["name"] == name)


# ------------------------------------------------- E1: compatible-with-adapter

def test_a_refused_candidate_is_reported_compatible_with_adapter(tmp_path):
    """The item's own pair. The §5 filter refuses it outright (different arity,
    different return); with the author's total waiver, resolve reports it as
    `compatible-with-adapter` with the plan and the artifact to commit."""
    reg = _build(tmp_path, {"vendor_cache": (VENDOR, None)})
    result = registry.Registry.from_dir(reg).resolve(
        NEED, adapt_opt_ins=TOTAL_WAIVER)

    assert _names(result) == ["vendor_cache"]
    adapter = _by_name(result, "vendor_cache")["adapter"]
    assert adapter["verdict"] == "compatible-with-adapter"
    assert adapter["chainDepth"] == 1
    assert adapter["merges"] == ["merge-total"]
    # the plan is per-position and auditable: the extra parameter is defaulted,
    # the Result is merged under the waiver.
    steps = {s["position"]: s for s in adapter["methods"][0]["steps"]}
    assert steps["options"]["transformation"] == "B1"
    assert steps["return"]["merge_shape"] == "merge-total"
    # ... and the rendered artifact shows the merge rather than hiding it.
    assert "Err(_) => None" in adapter["source"]


def test_the_proposal_is_never_silently_applied(tmp_path):
    """Design section 3: option (b), explicit and proposed. Nothing in the
    response claims the bridge exists - it is source for the author to commit,
    and the answer says so, including the wiring rename G2 requires."""
    reg = _build(tmp_path, {"vendor_cache": (VENDOR, None)})
    result = registry.Registry.from_dir(reg).resolve(
        NEED, adapt_opt_ins=TOTAL_WAIVER)
    adapter = _by_name(result, "vendor_cache")["adapter"]

    assert adapter["applied"] is False
    assert "PROPOSED" in adapter["note"]
    assert adapter["wiring"]["renameCandidateKey"] == {"from": "cache",
                                                       "to": "backing"}
    # the resolve says out loud that the composition is no longer derivable
    # from the registry source alone until the declaration is committed.
    assert any("NOT derivable from the registry source alone" in a
               for a in result["assumptions"])


def test_the_rendered_artifact_readmits_through_the_ordinary_gate(tmp_path):
    """The whole safety story: the proposal is ordinary `.rvl` that the
    UNMODIFIED compiler admits. If it did not, the resolver would be proposing
    something the gate refuses."""
    reg = _build(tmp_path, {"vendor_cache": (VENDOR, None)})
    result = registry.Registry.from_dir(reg).resolve(
        NEED, adapt_opt_ins=TOTAL_WAIVER)
    source = _by_name(result, "vendor_cache")["adapter"]["source"]

    # the one edit the response tells the author to make: the candidate's
    # provision is renamed to the alias, so G2 sees exactly one provider of
    # `cache` - the adapter (design section 4, "Wiring").
    wiring = _by_name(result, "vendor_cache")["adapter"]["wiring"]
    rebound = VENDOR.replace(
        f"provides {wiring['renameCandidateKey']['from']}: VendorCache",
        f"provides {wiring['renameCandidateKey']['to']}: VendorCache").replace(
        "provide cache {", f"provide {wiring['requireAlias']} {{")
    ir = compile_source(NEED + rebound + source, "committed.rvl")
    assert "KVAdapter" in {c["name"] for c in ir["components"]}


# ----------------------------------------------------- named near misses (§5)

def test_without_the_opt_in_the_pair_is_a_named_near_miss(tmp_path):
    """The merge is an author's semantic decision, never the resolver's. With
    no `D`, the same pair refuses - and the refusal names the method, the
    position, and the closed clause, so the fix starts from the position."""
    reg = _build(tmp_path, {"vendor_cache": (VENDOR, None)})
    result = registry.Registry.from_dir(reg).resolve(NEED)

    assert _names(result) == []
    near = result["nearMisses"]
    assert [n["name"] for n in near] == ["vendor_cache"]
    refusal = near[0]["refusals"][0]
    assert (refusal["method"], refusal["position"]) == ("get", "return")
    assert refusal["clause"] == "outcome-merge"
    assert refusal["hint"]                       # the repair, spelled out
    # item 274: the same list projected into the shared navigable record.
    assert near[0]["navigate"]["family"] == "adapter"
    assert near[0]["navigate"]["blocked"] is False
    # and the answer says WHY nothing bridged, rather than looking like an
    # empty registry.
    assert any("no `adapt` opt-in map was supplied" in a
               for a in result["assumptions"])


def test_a_method_the_candidate_lacks_is_not_reported_as_a_near_miss(tmp_path):
    """v1 matches methods by NAME (design §2.4, §8): a candidate missing a
    required method is never bridgeable. Reporting it would make every
    unrelated entry in the registry a near miss, which is noise, not honesty."""
    unrelated = """
service Mailer {
  fn send(to: Str) -> Opt[Str]
}

component MailerImpl provides mailer: Mailer {
  provide mailer { fn send(to) = None }
}
"""
    reg = _build(tmp_path, {"mailer": (unrelated, None)})
    result = registry.Registry.from_dir(reg).resolve(
        NEED, adapt_opt_ins=TOTAL_WAIVER)
    assert result["candidates"] == []
    assert result["nearMisses"] == []


# --------------------------------------------------------------- §6.1 ranking

def test_direct_compatible_outranks_adapted_at_equal_authority(tmp_path):
    """Section 6.1: "the bridge is a cost, not a tie". Both candidates here are
    compatible SUPERSETS rather than exact matches and declare the same (empty)
    authority, so the interface-fit and least-authority terms tie and the new
    direct-over-adapted bit is the only thing that can decide."""
    reg = _build(tmp_path, {"direct_cache": (DIRECT_CACHE, None),
                            "vendor_cache": (VENDOR, None)})
    result = registry.Registry.from_dir(reg).resolve(
        NEED, adapt_opt_ins=TOTAL_WAIVER)

    assert _names(result) == ["direct_cache", "vendor_cache"]
    assert "adapter" not in _by_name(result, "direct_cache")
    assert _by_name(result, "vendor_cache")["adapter"]["chainDepth"] == 1


# ------------------------------------------------------------ E12: §6.4 chains

def test_the_marking_is_read_off_a_committed_adapter():
    """The machine-readable half of the section-4 header, and the honest
    default: an unmarked component is ordinary code, so a bridge onto it is
    depth 1."""
    mark = adapter_marking(COMMITTED_ADAPTER)
    assert mark == {"derivation": _FAKE_DERIVATION,
                    "catalogue": "catalogue-v1", "depth": 1}
    assert chain_depth_for(COMMITTED_ADAPTER) == 2
    assert adapter_marking(PLAIN_VENDOR) is None
    assert chain_depth_for(PLAIN_VENDOR) == 1


def test_a_chain_ranks_below_a_fresh_single_bridge(tmp_path):
    """E12. Both candidates need the same B1 default and nothing else, declare
    the same authority, and neither merges - so they tie on every term but
    depth. The chain (a bridge in front of an already-committed adapter) ranks
    second, and its `why` says depth 2 out loud: one reviewed plan beats two
    stacked ones, and depth only ever ranks down."""
    reg = _build(tmp_path, {"plain_vendor": (PLAIN_VENDOR, None),
                            "scoped_adapter": (COMMITTED_ADAPTER, None)})
    result = registry.Registry.from_dir(reg).resolve(NEED)

    assert _names(result) == ["plain_vendor", "scoped_adapter"]
    assert _by_name(result, "plain_vendor")["adapter"]["chainDepth"] == 1
    chained = _by_name(result, "scoped_adapter")
    assert chained["adapter"]["chainDepth"] == 2
    assert "chain depth 2" in chained["why"]
    assert "ranks below a fresh single bridge" in chained["why"]


def _run_check(tmp_path, need_src, cand_src, *extra):
    """`revl adapt --check` (no --emit), returning the parsed JSON plan."""
    import contextlib

    from revl.__main__ import main
    need_file = tmp_path / "need.rvl"
    need_file.write_text(need_src)
    cand_file = tmp_path / "cand.rvl"
    cand_file.write_text(cand_src)
    out_file = tmp_path / "out.json"
    with open(out_file, "w", encoding="utf-8") as handle:
        with contextlib.redirect_stdout(handle):
            code = main(["adapt", str(need_file), str(cand_file), *extra])
    return code, json.loads(out_file.read_text())


def test_check_flattens_a_committed_chain_end_to_end(tmp_path):
    """E12, the `revl adapt --check` half of section 6.4. A bridge proposed in
    front of the committed `ScopedCacheAdapter` re-displays the COMPOSITE plan:
    the proposed hop (KV <- ScopedCache, `scope` defaulted) and the committed
    hop (ScopedCache <- PlainVendor, `scope` DROPPED, `options` defaulted), so
    the composed loss across both hops is in one listing, not just the last
    hop's slice of it."""
    code, plan = _run_check(tmp_path, NEED, COMMITTED_ADAPTER,
                            "--candidate-service", "ScopedCache")
    assert code == 0
    assert plan["verdict"] == "compatible-with-adapter"
    assert plan["chainDepth"] == 2

    hops = {h["kind"]: h for h in plan["chain"]}
    proposed, committed = hops["proposed"], hops["committed"]
    assert proposed["hop"] == 2 and committed["hop"] == 1
    assert (proposed["from"], proposed["to"]) == ("KV", "ScopedCache")
    assert (committed["from"], committed["to"]) == ("ScopedCache", "PlainVendor")
    assert committed["requireKey"] == "backing"
    assert "opaque" not in committed

    def _positions(hop):
        return {s["position"]: s["transformation"]
                for s in hop["methods"][0]["steps"]}
    # the proposed hop defaults `scope`; the committed hop DROPS `scope` (B2)
    # and defaults the vendor's `options` (B1) - the loss the flattening exists
    # to surface in one place.
    assert _positions(proposed)["scope"] == "B1"
    assert _positions(committed)["scope"] == "B2"
    assert _positions(committed)["options"] == "B1"


def test_check_on_ordinary_code_is_depth_one_with_no_chain(tmp_path):
    """The honest default: an unmarked candidate is ordinary code, so `--check`
    reports `chainDepth` 1 and emits no `chain` - there is no committed hop to
    flatten."""
    code, plan = _run_check(tmp_path, NEED, PLAIN_VENDOR)
    assert code == 0
    assert plan["chainDepth"] == 1
    assert "chain" not in plan


def _committed_adapter(merge, backing_service_src, backing_name, provided_src,
                       provided_name):
    """A committed adapter over `backing_name`, rendered by the real emitter so
    its body has the exact shape the flattener reconstructs, then prefixed with
    both service declarations so the candidate file compiles standalone."""
    from revl.adapt import render_adapter, service_surface, derivation_hash
    from revl.admission import _service_from_ir
    src_ir = compile_source(
        _TYPES + backing_service_src + provided_src
        + f"\ncomponent _Impl provides k: {backing_name} {{ provide k {{"
        + " fn get(key, options) = Ok(key) } }\n", "impl.rvl")
    req = _service_from_ir(provided_name, src_ir["services"][provided_name])
    prov = _service_from_ir(backing_name, src_ir["services"][backing_name])
    derivation = derivation_hash(service_surface(req), service_surface(prov),
                                 "0" * 64, json.dumps({}))
    body = render_adapter(
        "ChainAdapter", req, prov, {"get": {"return": {"merge": merge}}},
        provide_key="cache", require_key="backing",
        prov_types=src_ir["types"], derivation=derivation, chain_depth=1)
    return _TYPES + backing_service_src + provided_src + body


def test_check_flattens_a_committed_total_waiver_merge(tmp_path):
    """The merge a committed adapter performs is recovered from its body so the
    flattened committed hop names it (`merge-total`), not just the proposed
    hop's identity return: the composed loss includes the error-to-absence
    merge the committed adapter carried."""
    cand = _committed_adapter(
        "total",
        "service VendorCache { fn get(key: Str, options: Options)"
        " -> Result[Str, Error] }\n", "VendorCache",
        "service Cache { fn get(key: Str) -> Opt[Str] }\n", "Cache")
    code, plan = _run_check(tmp_path, NEED, cand, "--candidate-service", "Cache")
    assert code == 0
    assert plan["chainDepth"] == 2
    committed = [h for h in plan["chain"] if h["kind"] == "committed"][0]
    assert "opaque" not in committed
    assert committed["merges"] == ["merge-total"]
    ret = [s for s in committed["methods"][0]["steps"]
           if s["position"] == "return"][0]
    assert ret["merge_shape"] == "merge-total"


def test_check_marks_an_unreconstructable_committed_hop_opaque(tmp_path):
    """Honest degradation: a committed adapter whose body merges a CLOSED error
    per variant is outside the flagship reconstruction, so the flattened hop is
    reported `opaque` (pointing at the committed adapter's own attestation)
    rather than inventing a plan it never ran."""
    cand = _committed_adapter(
        {"NotFound": "None", "Unavailable": "None"},
        "type VErr = NotFound | Unavailable\n"
        "service VendorCache { fn get(key: Str, options: Options)"
        " -> Result[Str, VErr] }\n", "VendorCache",
        "service Cache { fn get(key: Str) -> Opt[Str] }\n", "Cache")
    code, plan = _run_check(tmp_path, NEED, cand, "--candidate-service", "Cache")
    assert code == 0
    assert plan["chainDepth"] == 2
    committed = [h for h in plan["chain"] if h["kind"] == "committed"][0]
    assert "opaque" in committed
    assert "attestation" in committed["opaque"]
    assert "methods" not in committed


# ------------------------------------------------- E13: §6.1 evidence discount

def test_an_outcome_merge_discounts_the_error_semantics_evidence(tmp_path):
    """E13. `vendor_cache` carries the STRONGER fault sweep (12/12 vs 8/12),
    both vouched by a valid attestation - so on evidence alone it wins. But its
    plan merges `Err` into `None`, which inverts exactly the conclusion that
    sweep attests, so the class is discounted and the merge-free candidate
    takes the top slot. The reported facets stay honest: the sweep is still
    `full`, it just stops buying rank it no longer earns."""
    reg = _build(tmp_path, {
        "vendor_cache": (VENDOR, {registry.EVIDENCE_FAULT_SWEEP: _sweep(12, 12),
                                  registry.EVIDENCE_ATTESTATION: "ATTEST"}),
        "plain_vendor": (PLAIN_VENDOR,
                         {registry.EVIDENCE_FAULT_SWEEP: _sweep(8, 12),
                          registry.EVIDENCE_ATTESTATION: "ATTEST"}),
    })
    result = registry.Registry.from_dir(reg).resolve(
        NEED, adapt_opt_ins=TOTAL_WAIVER, key=KEY)

    assert _names(result) == ["plain_vendor", "vendor_cache"]
    merging = _by_name(result, "vendor_cache")
    assert merging["evidence"]["discounted"] == ["fault-sweep"]
    assert "merge-total" in merging["evidence"]["discountReason"]
    # the honest grade is untouched - only the RANK was discounted.
    assert merging["evidence"]["facets"]["fault-sweep"] == "full"
    assert "INVERTED" in merging["why"]
    # the merge-free plan keeps its evidence at full weight, unflagged.
    assert "discounted" not in _by_name(result, "plain_vendor")["evidence"]


def test_a_merge_free_plan_ranks_its_evidence_at_full_weight(tmp_path):
    """The other half of E13: the same candidate surface WITHOUT a merge in the
    plan keeps its fault sweep, and the stronger sweep wins as it always did.
    Otherwise the discount would just be a penalty on being adapted at all."""
    other = PLAIN_VENDOR.replace("PlainVendor", "OtherVendor")
    reg = _build(tmp_path, {
        "plain_vendor": (PLAIN_VENDOR,
                         {registry.EVIDENCE_FAULT_SWEEP: _sweep(12, 12),
                          registry.EVIDENCE_ATTESTATION: "ATTEST"}),
        "other_vendor": (other, {registry.EVIDENCE_FAULT_SWEEP: _sweep(8, 12),
                                 registry.EVIDENCE_ATTESTATION: "ATTEST"}),
    })
    result = registry.Registry.from_dir(reg).resolve(NEED, key=KEY)

    assert _names(result) == ["plain_vendor", "other_vendor"]
    for name in ("plain_vendor", "other_vendor"):
        assert "discounted" not in _by_name(result, name)["evidence"]


# ------------------------------------------------------------ the opt-out path

def test_adapt_false_restores_the_direct_only_read_path(tmp_path):
    """The probe is additive and switchable off: with `adapt=False` the answer
    is exactly the pre-296 one, and it says which read path produced it."""
    reg = _build(tmp_path, {"vendor_cache": (VENDOR, None)})
    result = registry.Registry.from_dir(reg).resolve(
        NEED, adapt_opt_ins=TOTAL_WAIVER, adapt=False)

    assert result["candidates"] == []
    assert "nearMisses" not in result
    assert any("adapter probing was disabled" in a for a in result["assumptions"])


def test_the_seed_registry_answer_is_unchanged_by_the_probe():
    """No regression on the shipped registry: the item-49 exit answers are the
    same, and nothing there needs an adapter."""
    seed = os.path.join(os.path.dirname(__file__), "..", "registry")
    result = registry.Registry.from_dir(seed).resolve("""
service KVSeed {
  fn get(key: Str) -> Opt[Str]
  emission fn put(key: Str, value: Str)
}
""")
    assert _names(result) == ["user_cache"]
    assert result["nearMisses"] == []


# ------------------------------------------------------ one adapter identity

def test_resolve_and_revl_adapt_derive_the_same_adapter_identity(tmp_path):
    """The derivation hash is the adapter's identity, for evidence (§6.1) and
    for staleness. Two surfaces naming the same pair differently would defeat
    both, so `revl adapt --emit` and `resolve` must agree byte for byte."""
    from revl.__main__ import main

    reg = _build(tmp_path, {"vendor_cache": (VENDOR, None)})
    resolved = registry.Registry.from_dir(reg).resolve(
        NEED, adapt_opt_ins=TOTAL_WAIVER)
    from_resolve = _by_name(resolved, "vendor_cache")["adapter"]["derivation"]

    need_file = tmp_path / "need.rvl"
    need_file.write_text(NEED)
    cand_file = tmp_path / "cand.rvl"
    cand_file.write_text(VENDOR)
    d_file = tmp_path / "d.json"
    d_file.write_text(json.dumps(TOTAL_WAIVER))

    out_file = tmp_path / "out.json"
    import contextlib
    with open(out_file, "w", encoding="utf-8") as handle:
        with contextlib.redirect_stdout(handle):
            code = main(["adapt", str(need_file), str(cand_file),
                         "--adapt", str(d_file), "--emit", "--name",
                         "KVAdapter"])
    assert code == 0
    printed = json.loads(out_file.read_text())
    assert printed["derivation"] == from_resolve
    assert printed["chainDepth"] == 1
