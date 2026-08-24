"""Verified canary — progressive delivery with a derived rollback
(src/revl/mcp/canary.py, docs/verified-canary.md, roadmap item 59).

`revl swap` (item 23) is an all-or-nothing cutover; a canary is the gradual
form. It reuses landed machinery rather than reinventing it:

  * the slice is a REALM (item 10), because G2 forbids a second provider of a
    key in one realm — so the canary provider can only live in another realm;
  * divergence is a REPLAY COMPARISON of two recorded worlds (docs/replay.md),
    attributed to the exact (component, realm) that produced the first
    differing step — not a metric;
  * revert is the DERIVED LIFO teardown of the slice, reusing
    `erase_report.build_report` for the residue + `survivors` proof;
  * promote is item 23's SWAP for the remainder (the swap admission gate).

THE EXIT TEST (`test_exit_one_of_n_diverges_and_reverts_clean`): a canary
serves 1 of N tenants, diverges under the replay comparison, and reverts with a
residue proof — the other N-1 tenants provably untouched (`survivors`). The
runtime R4 no-residue proof needs cordis-py; the EXACT survivors proof is
static and always runs, so the exit test's untouched-claim holds without the
runtime, and the R4 leg is asserted only when the runtime is present.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from revl.compiler import compile_files  # noqa: E402
from revl.mcp import canary  # noqa: E402
from revl.placement import slice_partition, slice_realms  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures")
BASELINE = os.path.join(FIX, "canary_tenants.rvl")
CANDIDATE_DIVERGE = os.path.join(FIX, "canary_candidate_diverge.rvl")
CANDIDATE_SAME = os.path.join(FIX, "canary_candidate_same.rvl")

TENANTS = ("tenant_a", "tenant_b", "tenant_c")
OTHER_TENANT_COMPONENTS = {
    "TenantBStore", "TenantBApp", "TenantCStore", "TenantCApp",
}


def _has_runtime() -> bool:
    try:
        import cordis  # noqa: F401,PLC0415
        return True
    except ModuleNotFoundError:
        return False


@pytest.fixture(scope="module")
def baseline_ir():
    return compile_files([BASELINE])


# --------------------------------------------------------------- slice model

def test_slice_is_a_realm_and_g2_keeps_it_disjoint(baseline_ir):
    """The composition declares N tenant realms; each is a designated slice, and
    the same key `kv` is provided once per realm (G2 disjointness per (key,
    realm)) — which is exactly why a canary provider can serve one slice."""
    assert slice_realms(baseline_ir) == list(TENANTS)
    part = slice_partition(baseline_ir, "tenant_a")
    assert part["providers"] == {"kv": "TenantAStore"}
    assert part["members"] == ["TenantAStore", "TenantAApp"]
    # promote would swap the SAME key in the remainder realms
    assert part["remainderRealms"] == ["tenant_b", "tenant_c"]
    assert part["remainderProviders"] == {
        "tenant_b": {"kv": "TenantBStore"},
        "tenant_c": {"kv": "TenantCStore"},
    }


def test_select_slice_names_known_realms_on_miss(baseline_ir):
    from revl.errors import RevlError

    with pytest.raises(RevlError, match="no realm 'tenant_zzz'"):
        canary.select_slice(baseline_ir, "tenant_zzz")


# --------------------------------------------------- divergence (replay)

def test_divergence_is_attributed_to_the_slice(baseline_ir):
    """The candidate for tenant_a records an extra effect the baseline never
    did; the replay comparison finds the first differing step and attributes it
    to the exact (component, realm) — not a threshold on a metric."""
    report = canary.run_canary(baseline_ir, candidate_files=[CANDIDATE_DIVERGE],
                               realm="tenant_a", prove_residue=False)
    assert report["ok"] and report["admitted"]
    div = report["divergence"]
    assert div["diverged"] is True
    assert div["attribution"] == {"component": "TenantAStore", "realm": "tenant_a"}
    # the differing step is the candidate's extra `seed` acquisition
    assert "seed" in div["candidate"]["label"]


def test_no_divergence_recommends_promote(baseline_ir):
    """An identical candidate produces an identical recorded world: no
    divergence, so the recommendation is to promote (swap the remainder)."""
    report = canary.run_canary(baseline_ir, candidate_files=[CANDIDATE_SAME],
                               realm="tenant_a", prove_residue=False)
    assert report["divergence"]["diverged"] is False
    assert report["recommendation"] == "promote"


def test_divergence_uses_the_replay_step_vocabulary(baseline_ir):
    """A recorded world is a `replay.Timeline` of `replay.Step`s — the same
    vocabulary the backwards-replay engine records, so a divergence reads in the
    terms a step-back would."""
    tl = canary.slice_timeline(baseline_ir, "TenantAStore")
    kinds = {s.kind for s in tl.steps}
    # activation `let` effect + the provision, at least
    assert "effect" in kinds and "provision" in kinds


# ---------------------------------------------- revert (derived, survivors)

def test_revert_leaves_the_other_tenants_untouched(baseline_ir):
    """Revert = the derived LIFO teardown of the slice. The EXACT `survivors`
    set (from query.withdrawal via erase_report) proves every component outside
    the realm keeps every provision — G2 makes cross-realm orphaning
    impossible."""
    revert = canary.revert(baseline_ir, "tenant_a", prove_residue=False)
    assert revert["ok"]
    assert revert["untouched"] is True
    assert revert["breached"] == []
    assert set(revert["survivors"]) == OTHER_TENANT_COMPONENTS
    assert set(revert["withdrawnComponents"]) == {"TenantAStore", "TenantAApp"}


# --------------------------------------------------------- THE EXIT TEST

def test_exit_one_of_n_diverges_and_reverts_clean(baseline_ir):
    """Roadmap item 59's exit test, pinned exactly:

    a canary serves 1 of N tenants (a designated slice), DIVERGES under the
    replay comparison, and REVERTS with residue proof — the other N-1 tenants
    provably untouched (`survivors`).
    """
    report = canary.run_canary(baseline_ir, candidate_files=[CANDIDATE_DIVERGE],
                               realm="tenant_a", prove_residue=True)

    # 1 of N: the slice is one tenant realm; N-1 others are the remainder
    assert report["ok"] and report["admitted"]
    assert report["realm"] == "tenant_a"
    assert report["slice"]["members"] == ["TenantAStore", "TenantAApp"]
    assert report["slice"]["remainderRealms"] == ["tenant_b", "tenant_c"]

    # DIVERGES under the replay comparison, attributed to the slice
    assert report["divergence"]["diverged"] is True
    assert report["divergence"]["attribution"] == {
        "component": "TenantAStore", "realm": "tenant_a"}
    assert report["recommendation"] == "revert"

    # REVERTS with the other N-1 tenants provably untouched
    revert = report["revert"]
    assert revert["ok"]
    assert revert["untouched"] is True
    assert revert["breached"] == []
    assert set(revert["survivors"]) == OTHER_TENANT_COMPONENTS

    # residue proof: the EXACT survivors proof is always present; the runtime
    # R4 no-residue proof rides along only when cordis-py is installed.
    residue = revert["residueProof"]
    if _has_runtime():
        assert residue["available"] is True
        assert residue["proven"] is True
    else:
        assert residue["available"] is False


def test_exit_test_over_the_mcp_verb():
    """The same exit test, driven through the `revl_canary` MCP verb, proving
    the server surface returns the identical verdict."""
    from revl.mcp import server

    with open(BASELINE, encoding="utf-8") as handle:
        baseline_src = handle.read()
    with open(CANDIDATE_DIVERGE, encoding="utf-8") as handle:
        candidate_src = handle.read()

    response = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "revl_canary", "arguments": {
            "baseline": baseline_src,
            "candidate": candidate_src,
            "realm": "tenant_a",
            "proveResidue": False,
        }},
    })
    payload = _tool_payload(response)
    assert payload["ok"] and payload["divergence"]["diverged"] is True
    assert payload["revert"]["untouched"] is True
    assert set(payload["revert"]["survivors"]) == OTHER_TENANT_COMPONENTS


def test_candidate_refused_by_admission_gate(baseline_ir):
    """A candidate whose interface is incompatible with the running composition
    is reported refused — the same admission gate a swap runs re-checks every
    consumer's call site — never silently canaried. Here `get`'s return type
    changes, which breaks the running consumers."""
    bad = """
    service Kv { fn get(k: Str) -> Str  fn set(k: Str, v: Str) }
    component TenantAStore provides kv: Kv {
      isolate kv in realm("tenant_a")
      let store = effect Map.new() undo store.drop()
      provide kv {
        fn get(k) = "x"
        fn set(k, v) { effect store.insert(k, v) undo store.remove(k) }
      }
    }
    """
    report = canary.run_canary(baseline_ir, candidate_source=bad,
                               realm="tenant_a", prove_residue=False)
    assert report["ok"] is False
    assert report["admitted"] is False
    assert "refused" in report["error"]


def _tool_payload(response: dict) -> dict:
    """Unwrap an MCP tools/call response to the handler's structured result."""
    import json

    result = response["result"]
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    return json.loads(result["content"][0]["text"])
