"""Item 310: capability-aware caching, the SEAM-METHOD slice.

Design: docs/design/310-capability-aware-caching.md (revised — the implementable
scope is `cache` on SEAM SERVICE METHODS, where the call IS the crossing so the
consent gate can decide the hit/miss transaction pre-execution). Covers:

  * the SURFACE + IR: `cache pure|capability|external` as a contextual trailing
    clause on a fn / extern / service method, lowered to the roadmap vocabulary
    (`pure_fn`/`capability_result`/`external_effect`), inert to the emission fold;
  * the COMPILE REFUSALS: the class-vs-reach mismatches, the per-class freshness
    rules (external requires a bound, pure forbids one, capability forbids
    `invalidated_by`), the structural resource-in-entry walk, the escrow-shaped
    reach refusals, the interior-crossing extern "later slice" refusal, and the
    `invalidated_by` token resolution;
  * the SEAM GATE (live cordis-py session): `cache pure` memoizes; a `cache
    capability` hit skips consumption and does NOT launder authority (revocation
    kills the entry, an ungranted access is refused); `cache external` ttl expiry
    and `invalidated_by` re-fetch; the no-policy inert rule; the `state()` hit
    counter;
  * the DISTRIBUTED-placement refusal;
  * ADDITIVITY: a non-cache program's IR is byte-identical.

`examples/handoff_cache.rvl` — which uses `cache` as a provided KEY name — still
compiles, proving the contextual keyword does not reserve the word.
"""

from __future__ import annotations

import copy
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
BACKEND = ROOT / "backends" / "python"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from revl.compiler import compile_files, compile_source  # noqa: E402
from revl.lower import RevlError, check_and_lower  # noqa: E402
from revl.parser import Parser  # noqa: E402
from revl.placement import cache_crossing_refusal  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the seam gate is proven against a live cordis-py composition — "
           "install it with `sh backends/python/setup.sh` and run under its venv",
)


def _lower(src: str) -> dict:
    return check_and_lower(Parser(src, "t.rvl").parse())


# ------------------------------------------------------------ surface + IR

def test_cache_pure_on_a_fn_lowers_to_pure_fn():
    ir = _lower("fn shade(base: Str, light: Str) -> Str cache pure { return base }")
    assert ir["functions"][0]["cache"] == {"class": "pure_fn"}


def test_cache_capability_on_a_method_lowers_to_capability_result():
    ir = _lower("service S { emission[reg] fn resolve(name: Str) -> Str "
                "cache capability }")
    assert ir["services"]["S"]["methods"]["resolve"]["cache"] == {
        "class": "capability_result"}


def test_cache_external_lowers_with_its_freshness_bound():
    ir = _lower("service S { emission[user.updated] fn get(k: Str) -> Str "
                "cache external invalidated_by user.updated ttl 5m }")
    assert ir["services"]["S"]["methods"]["get"]["cache"] == {
        "class": "external_effect",
        "invalidated_by": ["user.updated"],
        "ttl_ms": 300000}


def test_ttl_reuses_the_policy_duration_forms():
    for spelling, ms in (("500ms", 500), ("30", 30000), ("2m", 120000),
                         ("1h", 3600000)):
        ir = _lower(f"service S {{ emission[t] fn g(k: Str) -> Str "
                    f"cache external ttl {spelling} }}")
        assert ir["services"]["S"]["methods"]["g"]["cache"]["ttl_ms"] == ms


def test_invalidated_by_ttl_token_and_ttl_clause_disambiguate():
    # `invalidated_by ttl ttl 5m` is the token `ttl` then a ttl clause (the
    # namespaces overlap — design §Surface, one parser test required).
    ir = _lower("service S { emission[ttl] fn g(k: Str) -> Str "
                "cache external invalidated_by ttl ttl 5m }")
    cache = ir["services"]["S"]["methods"]["g"]["cache"]
    assert cache["invalidated_by"] == ["ttl"]
    assert cache["ttl_ms"] == 300000


def test_cache_is_inert_to_the_emission_fold():
    # the emission fixed point sees the crossing identically with cache on/off:
    # the reach report is byte-identical.
    base = ("extern emission fn read_db(id: Str) -> Str = @py {{ return id }}\n"
            "service S {{ emission fn get(id: Str) -> Str{clause} }}\n"
            "component C provides s: S {{ provide s {{ "
            "fn get(id) = emit read_db(id) }} }}\n")
    plain = compile_source(base.format(clause=""), "a.rvl")
    cached = compile_source(base.format(clause=" cache capability"), "a.rvl")
    # the only IR difference is the additive cache descriptor on the method
    del cached["services"]["S"]["methods"]["get"]["cache"]
    assert plain == cached


def test_handoff_cache_example_still_compiles():
    # `cache` is used as a provided KEY name there; the contextual keyword must
    # not reserve the word (design §Surface, grammar-edge exit test).
    compile_files([str(ROOT / "examples" / "handoff_cache.rvl")])


def test_a_program_using_no_cache_carries_no_new_ir_keys():
    ir = _lower("service S { emission fn get(k: Str) -> Str }")
    assert "cache" not in ir["services"]["S"]["methods"]["get"]


# ------------------------------------------------------------ compile refusals

def test_cache_external_without_a_freshness_bound_is_refused():
    with pytest.raises(RevlError, match="requires a freshness bound"):
        _lower("service S { emission fn get(k: Str) -> Str cache external }")


def test_cache_external_with_either_clause_admits():
    _lower("service S { emission[e] fn g(k: Str) -> Str cache external ttl 5m }")
    _lower("service S { emission[e] fn g(k: Str) -> Str "
           "cache external invalidated_by e }")


def test_ttl_on_pure_is_a_category_error():
    with pytest.raises(RevlError, match="cannot declare a freshness bound"):
        _lower("service S { fn g(k: Str) -> Str cache pure ttl 5m }")


def test_invalidated_by_on_capability_is_refused():
    with pytest.raises(RevlError, match="cannot declare `invalidated_by`"):
        _lower("service S { emission[e] fn g(k: Str) -> Str "
               "cache capability invalidated_by e }")


def test_cache_pure_on_an_emission_method_is_a_mismatch():
    with pytest.raises(RevlError, match="crosses a boundary"):
        _lower("service S { emission fn g(k: Str) -> Str cache pure }")


def test_cache_capability_on_a_plain_method_is_a_mismatch():
    with pytest.raises(RevlError, match="crosses nothing"):
        _lower("service S { fn g(k: Str) -> Str cache capability }")


def test_cache_capability_on_a_plain_fn_is_refused():
    with pytest.raises(RevlError, match="not allowed on a plain"):
        _lower("fn f(x: Str) -> Str cache capability { return x }")


def test_invalidated_by_token_nothing_can_cross_is_refused():
    with pytest.raises(RevlError, match="no crossing in this composition can fire"):
        _lower("service S { emission[user.updated] fn g(k: Str) -> Str "
               "cache external invalidated_by no.such.token }")


# ------------------------------------------------------------ escrow refusals

_PRE = (
    "type W = { path: Str }\n"
    "type E = { msg: Str }\n"
    "extern pure fn restore(w: W) -> Unit = @py { pass }\n"
)


def test_cache_on_a_witnessed_extern_is_refused():
    with pytest.raises(RevlError, match="skip the escrow"):
        _lower(_PRE + (
            "extern witnessed[fs] fn rm(path: Str) -> Result[W, E] "
            "cache external ttl 5m undo restore(result) = @py { pass }\n"))


def test_cache_on_an_acquire_extern_is_refused():
    with pytest.raises(RevlError, match="skip the escrow"):
        _lower("extern pure fn shut(s: Sock) -> Unit = @py { pass }\n"
               "extern acquire fn open() -> Sock cache pure "
               "undo shut(result) = @py { pass }\n")


def test_cache_on_a_deferred_emission_extern_is_refused():
    with pytest.raises(RevlError, match="QUEUED write"):
        _lower("extern emission[mail] deferred fn send(to: Str) -> Unit "
               "cache capability = @py { pass }\n")


def test_cache_on_a_compensate_emission_extern_is_refused():
    with pytest.raises(RevlError, match="compensation escrow"):
        _lower("extern emission[pay] fn cleanup() -> Unit = @py { pass }\n"
               "extern emission[pay] fn charge(x: Str) -> Unit "
               "cache capability compensate cleanup() = @py { pass }\n")


def test_cache_on_an_interior_emission_extern_is_the_later_slice_refusal():
    with pytest.raises(RevlError, match="not yet enforceable"):
        _lower("extern emission[reg] fn resolve(name: Str) -> Str "
               "cache capability = @py { pass }\n")


# ------------------------------------------------------------ resource walk

# a resource handle stored in an entry would be stored authority; the walk is
# STRUCTURAL over the taint closure (nested record, variant arm, generic).
_RES = (
    "extern pure fn shut(s: Sock) -> Unit = @py { pass }\n"
    "extern acquire fn open() -> Sock undo shut(result) = @py { pass }\n"
    "type Sess = { conn: Sock }\n"                 # nested in a record
    "type Res = Live(Sock) | Dead\n"               # nested in a variant arm
    "type Box[T] = { it: T }\n"                     # generic instantiation
)


@pytest.mark.parametrize("ret", ["Sock", "Sess", "Opt[Sock]", "Res", "Box[Sock]"])
def test_cache_on_a_resource_carrying_result_is_refused_structurally(ret):
    with pytest.raises(RevlError, match="would store a resource handle"):
        _lower(_RES + (
            f"service S {{ emission[net] fn get(k: Str) -> {ret} "
            f"cache capability }}\n"))


def test_cache_on_a_resource_carrying_parameter_is_refused():
    with pytest.raises(RevlError, match="would store a resource handle"):
        _lower(_RES + ("service S { emission[net] fn get(s: Sock) -> Str "
                       "cache capability }\n"))


# ------------------------------------------------------------ the seam gate

_CAP_SRC = (
    "extern emission fn read_db(sink: Str, id: Str) -> Str = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('r:' + id + '\\n')\n"
    "    return 'P:' + id\n"
    "}\n"
    "service Users {\n"
    "  emission fn get(sink: Str, id: Str) -> Str cache capability\n"
    "}\n"
    "component U provides users: Users {\n"
    "  provide users { fn get(sink, id) = emit read_db(sink, id) }\n"
    "}\n"
)


def _session(policy="auto"):
    from revl.mcp.session import Session
    s = Session()
    s.approval_policy = policy
    return s


def _reads(sink: str) -> int:
    if not os.path.exists(sink):
        return 0
    return sum(1 for line in Path(sink).read_text().splitlines()
               if line.startswith("r:"))


@needs_cordis
def test_cache_pure_memoizes_on_and_off_policy(tmp_path):
    src = (
        "extern pure fn compute(sink: Str, id: Str) -> Str = @py {\n"
        "    with open(sink, 'a') as _f:\n"
        "        _f.write('r:' + id + '\\n')\n"
        "    return 'P:' + id\n"
        "}\n"
        "service S { fn get(sink: Str, id: Str) -> Str cache pure }\n"
        "component C provides s: S { provide s { "
        "fn get(sink, id) = compute(sink, id) } }\n")
    ir = compile_source(src, "p.rvl")
    for policy in (None, "auto"):
        s = _session(policy)
        s.load(copy.deepcopy(ir), record=policy is not None)
        sink = str(tmp_path / f"pure-{policy}.log")
        r1 = s.call("s", "get", [sink, "1"])
        r2 = s.call("s", "get", [sink, "1"])
        s.call("s", "get", [sink, "2"])
        assert r1["result"] == "P:1"
        assert r2.get("cacheHit") is True
        assert _reads(sink) == 2           # id1 once (memoized) + id2 once


@needs_cordis
def test_cache_capability_hit_skips_consumption(tmp_path):
    ir = compile_source(_CAP_SRC, "c.rvl")
    s = _session("auto")
    s.load(copy.deepcopy(ir), record=True)
    s.mint_standing_grant(capability="read_db", uses=5)
    sink = str(tmp_path / "cap.log")
    r1 = s.call("users", "get", [sink, "1"])
    r2 = s.call("users", "get", [sink, "1"])
    s.call("users", "get", [sink, "2"])
    assert r1.get("cacheHit") is None      # miss
    assert r2.get("cacheHit") is True      # hit
    assert _reads(sink) == 2               # only two misses touched the host
    m = s.approval_metrics()
    assert m["grantsConsumed"] == 2        # the hit consumed nothing
    assert m["cacheHits"] == 1
    assert m["standingGrants"][0]["remainingUses"] == 3


@needs_cordis
def test_a_hit_does_not_launder_authority(tmp_path):
    from revl.mcp.approval import ApprovalRequired
    ir = compile_source(_CAP_SRC, "c.rvl")
    s = _session("auto")
    s.load(copy.deepcopy(ir), record=True)
    sink = str(tmp_path / "launder.log")
    # ungranted: the very first access is refused (no entry, no authority)
    with pytest.raises(ApprovalRequired):
        s.call("users", "get", [sink, "1"])
    assert _reads(sink) == 0
    # grant, miss+hit, then revoke: the entry dies with the grant, and the next
    # access takes the miss path and is refused exactly as an uncached call.
    s.mint_standing_grant(capability="read_db", uses=5)
    s.call("users", "get", [sink, "1"])
    assert s.call("users", "get", [sink, "1"]).get("cacheHit") is True
    s.revoke_standing_grant(capability="read_db")
    with pytest.raises(ApprovalRequired):
        s.call("users", "get", [sink, "1"])
    assert _reads(sink) == 1               # the refused access crossed nothing


@needs_cordis
def test_a_one_use_grant_yields_one_miss_and_zero_hits(tmp_path):
    from revl.mcp.approval import ApprovalRequired
    ir = compile_source(_CAP_SRC, "c.rvl")
    s = _session("auto")
    s.load(copy.deepcopy(ir), record=True)
    s.mint_standing_grant(capability="read_db", uses=1)
    sink = str(tmp_path / "onceuse.log")
    s.call("users", "get", [sink, "1"])    # the one miss exhausts the grant
    with pytest.raises(ApprovalRequired):  # entry died with the exhausted grant
        s.call("users", "get", [sink, "1"])
    assert s.approval_metrics()["cacheHits"] == 0


@needs_cordis
def test_cache_external_ttl_expiry_refetches(tmp_path):
    src = _CAP_SRC.replace("cache capability", "cache external ttl 5m")
    ir = compile_source(src, "e.rvl")
    s = _session("auto")
    box = {"now": 0}
    s._clock_ms = lambda: box["now"]
    s.load(copy.deepcopy(ir), record=True)
    s.mint_standing_grant(capability="read_db", uses=10)
    sink = str(tmp_path / "ttl.log")
    s.call("users", "get", [sink, "1"])
    box["now"] = 1000
    assert s.call("users", "get", [sink, "1"]).get("cacheHit") is True
    assert _reads(sink) == 1
    box["now"] = 300001                    # past the 5m ttl
    assert s.call("users", "get", [sink, "1"]).get("cacheHit") is None
    assert _reads(sink) == 2


_INV_SRC = (
    "extern emission fn read_db(sink: Str, id: Str) -> Str = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('r:' + id + '\\n')\n"
    "    return 'P:' + id\n"
    "}\n"
    "extern emission[user.updated] fn touch_db(sink: Str) -> Unit = @py {\n"
    "    with open(sink, 'a') as _f:\n"
    "        _f.write('t\\n')\n"
    "    return\n"
    "}\n"
    "service Users {\n"
    "  emission fn get(sink: Str, id: Str) -> Str "
    "cache external invalidated_by user.updated\n"
    "  emission fn touch(sink: Str)\n"
    "}\n"
    "component U provides users: Users {\n"
    "  provide users {\n"
    "    fn get(sink, id) = emit read_db(sink, id)\n"
    "    fn touch(sink) { emit touch_db(sink) }\n"
    "  }\n"
    "}\n"
)


@needs_cordis
def test_invalidated_by_refetches_after_the_named_crossing(tmp_path):
    ir = compile_source(_INV_SRC, "i.rvl")
    s = _session("auto")
    s.load(copy.deepcopy(ir), record=True)
    s.mint_standing_grant(capability="read_db", uses=10)
    s.mint_standing_grant(capability="user.updated", uses=10)
    sink = str(tmp_path / "inv.log")
    s.call("users", "get", [sink, "1"])
    assert s.call("users", "get", [sink, "1"]).get("cacheHit") is True
    assert _reads(sink) == 1
    s.call("users", "touch", [sink])       # crosses user.updated -> invalidate
    assert s.call("users", "get", [sink, "1"]).get("cacheHit") is None
    assert _reads(sink) == 2
    # a fresh entry is then live again
    assert s.call("users", "get", [sink, "1"]).get("cacheHit") is True


@needs_cordis
def test_no_policy_session_is_inert_for_capability(tmp_path):
    ir = compile_source(_CAP_SRC, "c.rvl")
    s = _session(None)                     # no policy -> no ledger -> no entry store
    s.load(copy.deepcopy(ir), record=False)
    sink = str(tmp_path / "inert.log")
    s.call("users", "get", [sink, "1"])
    s.call("users", "get", [sink, "1"])    # every access is a miss
    assert _reads(sink) == 2


# ------------------------------------------------------------ distribution

def test_a_distributed_placement_refuses_cache():
    src = (
        "extern emission fn read_db(id: Str) -> Str = @py { return id }\n"
        "service Users { emission fn get(id: Str) -> Str cache external ttl 5m }\n"
        "component U provides users: Users { provide users { "
        "fn get(id) = emit read_db(id) } }\n")
    ir = compile_source(src, "d.rvl")
    # `c` requires `users`, provided by a different process `w`
    split = cache_crossing_refusal(
        ir,
        requires={"c": {"users": "Users"}},
        provides={"c": {}, "w": {"users": "Users"}},
        owner={"users": "w"})
    assert split is not None
    assert "Users" in split and "get" in split and "process seam" in split
    # the same composition in ONE process admits
    colocated = cache_crossing_refusal(
        ir,
        requires={"c": {"users": "Users"}},
        provides={"c": {"users": "Users"}},
        owner={"users": "c"})
    assert colocated is None
