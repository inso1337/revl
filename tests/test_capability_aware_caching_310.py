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
def test_a_seam_hit_writes_a_cache_hit_wal_record(tmp_path):
    """Design 310 laundering point 5 / design 462 finding 1: every hit is on the
    record. Before the `record_cache_hit` writer existed, `_record_cache_hit`
    resolved it with `getattr(..., None)` and silently wrote nothing — the
    `cacheHits` counter moved but the durable audit line the seam slice claimed
    did not exist. The hit's record names the seam it re-delivers and the
    recorded grant its liveness is bound to, so an audit joins the hit to the
    same authority a miss would have consumed."""
    from revl.wal import read_wal
    ir = compile_source(_CAP_SRC, "c.rvl")
    s = _session("auto")
    s.load(copy.deepcopy(ir), record=True)
    assert s.recorder.wal is not None, "the policy session opened no WAL"
    grant = s.mint_standing_grant(capability="read_db", uses=5)
    sink = str(tmp_path / "hitrec.log")
    s.call("users", "get", [sink, "1"])                       # miss
    assert s.call("users", "get", [sink, "1"]).get("cacheHit") is True  # hit
    records = read_wal(s.recorder.wal.path)["records"]
    hits = [r for r in records if r.get("record") == "cache-hit"]
    assert len(hits) == 1, records
    assert hits[0]["key"] == "users" and hits[0]["method"] == "get"
    # the record binds the hit to the entry's authority: the recorded grant id
    # the miss consumed, so a miss (spend) and a hit (no spend) read against the
    # same grant. A hit that named no authority would be exactly the laundering
    # the record exists to make auditable.
    assert hits[0]["grantIds"], hits[0]
    consumed = [r for r in records if r.get("record") == "approval-consumed"]
    assert len(consumed) == 1, "the hit consumed nothing; only the miss spent"


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


# --------------------------------------------------- surface H: applicability
# The applicability fold ("is this callee's reach cacheable?") is a 414 SURFACE
# in its own right, so it is a worst-over-reach fold over the SAME provider
# closure the class map folds — not a second reach walk that could disagree with
# it — and it runs at LOAD, because the compile-time checks see only the
# DECLARING method's reach shape while the clause is an interface contract every
# provider inherits.


def _surface_h(src: str) -> str | None:
    """The fold's verdict for `src`, driven exactly as `Session.load` drives it
    (the same index builder, the same class map). No cordis: `ClassMap` is
    derived from the IR alone."""
    from revl.mcp.approval import ClassMap, cache_applicability_refusal
    from revl.mcp.session import Session
    ir = compile_source(src, "h.rvl")
    index = Session._build_cache_index(Session.__new__(Session), ir)
    return cache_applicability_refusal(ClassMap(ir), index)


_H_WITNESSED = (
    "type W = { path: Str }\n"
    "type E = { msg: Str }\n"
    "extern pure fn restore(w: W) -> Unit = @py { pass }\n"
    "extern witnessed[fs] fn rm(path: Str) -> Result[W, E] "
    "undo restore(result) = @py { pass }\n")


def test_surface_h_admits_a_plain_emission_read():
    """The feature itself: a seam method whose closure crosses one ordinary
    emission is exactly what `cache capability` is for."""
    assert _surface_h(_CAP_SRC) is None


def test_surface_h_admits_a_crossing_free_pure_method():
    assert _surface_h(
        "fn shade(n: Int) -> Int { return n * 2 }\n"
        "service S { fn get(n: Int) -> Int cache pure }\n"
        "component C provides s: S { provide s { fn get(n) = shade(n) } }\n"
    ) is None


def test_surface_h_is_inert_without_a_cache_clause():
    assert _surface_h(_CAP_SRC.replace(" cache capability", "")) is None


def test_surface_h_follows_the_transitive_service_closure():
    """414 crossing kind 4. The escrow-shaped crossing is two seams away, in a
    component the declaring one only REQUIRES — invisible to any check that
    reads the declaration alone."""
    problem = _surface_h(_H_WITNESSED + (
        "service Inner { emission fn go(x: Str) -> Result[W, E] }\n"
        "service Outer { emission fn get(x: Str) -> Result[W, E] cache capability }\n"
        "component I provides inner: Inner { provide inner { fn go(x) = rm(x) } }\n"
        "component O provides outer: Outer requires inner: Inner {\n"
        "  provide outer { fn get(x) = emit inner.go(x) }\n"
        "}\n"))
    assert problem is not None
    assert "`witnessed` extern `rm`" in problem and "I.inner.go" in problem
    assert "outer.get" in problem and "surface H" in problem


def test_surface_h_follows_the_spawn_seam():
    """414 crossing kind 2: the crossing is reached through a spawn handle
    (`w.inner.go`), which carries no `req` target — the seam a reach walk that
    only follows `requires` edges misses entirely."""
    problem = _surface_h(
        "extern emission[mail] deferred fn send(msg: Str) = @py { return }\n"
        "service Inner { emission fn go(msg: Str) -> Int }\n"
        "service Svc { emission fn serve(msg: Str) -> Int cache capability }\n"
        "component Worker provides inner: Inner {\n"
        "  provide inner { fn go(msg) { emit send(msg) return 1 } }\n"
        "}\n"
        "component C provides svc: Svc {\n"
        "  let w = effect spawn Worker with { } undo w.dispose()\n"
        "  provide svc { fn serve(msg) { emit w.inner.go(msg) return 1 } }\n"
        "}\n")
    assert problem is not None
    assert "`deferred` emission extern `send`" in problem
    assert "Worker.inner.go" in problem and "svc.serve" in problem


def test_surface_h_refuses_the_star_widening():
    """414 crossing kind 8. An emitting callable handed on as a VALUE reaches a
    boundary no `emission[...]` list can name, so an entry cannot be scoped to
    the authority that covered its miss."""
    problem = _surface_h(
        "extern emission fn ship(x: Str) -> Str = @py { return x }\n"
        "fn indirect(f: (Str) -> Str, x: Str) -> Str { return f(x) }\n"
        "service S { emission fn loud(a: Str) -> Str cache capability }\n"
        "component C provides s: S { provide s { fn loud(a) = indirect(ship, a) } }\n")
    assert problem is not None
    assert "as a VALUE" in problem and "(`*`)" in problem


def test_surface_h_refuses_an_acquire_in_the_closure():
    problem = _surface_h(
        "type Sock = { fd: Int }\n"
        "extern pure fn shut(s: Sock) -> Unit = @py { pass }\n"
        "extern acquire fn open() -> Sock undo shut(result) = @py { pass }\n"
        "extern emission fn ping(s: Sock) -> Str = @py { return 'ok' }\n"
        "service S { emission fn get() -> Str cache capability }\n"
        "component C provides s: S { provide s { fn get() { "
        "let s = open() return emit ping(s) } } }\n")
    assert problem is not None
    assert "`acquire` extern `open`" in problem and "teardown would leak" in problem


def test_surface_h_refuses_a_compensating_emission_in_the_closure():
    problem = _surface_h(
        "extern emission[pay] fn cleanup() -> Unit = @py { pass }\n"
        "extern emission[pay] fn charge(x: Str) -> Str "
        "compensate cleanup() = @py { return x }\n"
        "service S { emission fn get(x: Str) -> Str cache capability }\n"
        "component C provides s: S { provide s { fn get(x) = emit charge(x) } }\n")
    assert problem is not None
    assert "`compensate`-declaring emission extern `charge`" in problem


def test_surface_h_refuses_cache_pure_over_component_state():
    """The hole the fold closes. `cache pure` on a non-emission method passes
    every compile-time check (it crosses nothing), but a provider whose body
    reads its own effect-created state is not a function of its arguments: an
    entry would serve a value the state has since moved past. The provider is
    only known once the composition is linked, which is why this is surface H's
    to catch and not the checker's."""
    src = ("service Store {\n"
           "  fn get(k: Str) -> Opt[Str] cache pure\n"
           "  fn put(k: Str, v: Str)\n"
           "}\n"
           "component Cache provides cache: Store {\n"
           "  let m = effect Map.new() undo m.drop()\n"
           "  provide cache {\n"
           "    fn get(k) = m.get(k)\n"
           "    fn put(k, v) { effect m.insert(k, v) undo m.remove(k) }\n"
           "  }\n"
           "}\n")
    problem = _surface_h(src)
    assert problem is not None
    assert "reads the component state `m`" in problem
    assert "function of the arguments alone" in problem
    # and the same method without the clause loads exactly as it always did
    assert _surface_h(src.replace(" cache pure", "")) is None


def test_surface_h_admits_an_immutable_component_binding():
    """Only STATE refuses: a per-instance constant fixed at activation leaves the
    method a function of its arguments, so it must not be swept up."""
    assert _surface_h(
        "service S { fn get(n: Int) -> Int cache pure }\n"
        "component C provides s: S {\n"
        "  config { base: Int = 10 }\n"
        "  provide s { fn get(n) = n + config.base }\n"
        "}\n") is None


@needs_cordis
def test_load_refuses_an_uncacheable_provider_closure():
    """The fold is wired into the pre-boot gates, so an uncacheable composition
    never reaches a runtime."""
    from revl.mcp.session import Session, SessionError
    ir = compile_source(
        "service Store { fn get(k: Str) -> Opt[Str] cache pure\n"
        "                fn put(k: Str, v: Str) }\n"
        "component Cache provides cache: Store {\n"
        "  let m = effect Map.new() undo m.drop()\n"
        "  provide cache { fn get(k) = m.get(k)\n"
        "                  fn put(k, v) { effect m.insert(k, v) undo m.remove(k) } }\n"
        "}\n", "h.rvl")
    s = Session()
    with pytest.raises(SessionError, match="reads the component state"):
        s.load(copy.deepcopy(ir))


# ------------------------------------------------ body-level pure memoization
# The seam gate memoizes a `cache pure` SERVICE METHOD at the call. A `cache
# pure` plain `fn` is reached from inside a body, where there is no seam — so
# its memo is emitted into the module, which is where the call happens.


def _emitted(src: str):
    """Compile `src`, emit the cordis-py module, and exec it."""
    import types

    import emit as py_emit
    source = py_emit.emit(compile_source(src, "m.rvl"))
    module = types.ModuleType("memo_probe")
    exec(compile(source, "memo_probe.py", "exec"), module.__dict__)  # noqa: S102
    return source, module


_MEMO_SRC = (
    'extern pure fn tick(sink: Str, n: Int) -> Int = @py {\n'
    '    with open(sink, "a") as _f:\n'
    '        _f.write("t\\n")\n'
    '    return n * 2\n'
    '}\n'
    'fn double(sink: Str, n: Int) -> Int cache pure { return tick(sink, n) }\n')


def test_cache_pure_fn_memoizes_in_the_emitted_body(tmp_path):
    _, module = _emitted(_MEMO_SRC)
    sink = str(tmp_path / "memo.log")
    assert module.double(sink, 3) == 6
    assert module.double(sink, 3) == 6          # served from the table
    assert module.double(sink, 4) == 8
    lines = Path(sink).read_text().splitlines()
    assert len(lines) == 2                      # one host call per distinct args


def test_a_first_class_reference_reaches_the_memo(tmp_path):
    """The memo wraps the PUBLIC name, so there is no spelling — call site or
    value reference — that reaches the un-memoized body by accident."""
    _, module = _emitted(
        _MEMO_SRC
        + "fn apply(f: (Str, Int) -> Int, s: Str, n: Int) -> Int "
          "{ return f(s, n) }\n")
    sink = str(tmp_path / "fc.log")
    assert module.apply(module.double, sink, 5) == 10
    assert module.apply(module.double, sink, 5) == 10
    assert len(Path(sink).read_text().splitlines()) == 1


def test_an_unmemoizable_argument_shape_falls_back_to_the_call(tmp_path):
    """The key is a whitelist over the shapes this backend emits; anything else
    answers `_REVL_NOMEMO` and the call is simply not memoized. A miss always
    recomputes a pure function, so an unknown shape costs speed, never
    correctness."""
    source, module = _emitted(_MEMO_SRC)
    assert module._revl_memo_key((lambda: 1,)) is module._REVL_NOMEMO
    assert module._revl_memo_key(object()) is module._REVL_NOMEMO
    # structural, not identity: two equal records key the same
    assert module._revl_memo_key({"a": 1, "b": [2, 3]}) \
        == module._revl_memo_key({"b": [2, 3], "a": 1})
    # and the types stay apart, so `1` and `True` are not one entry
    assert module._revl_memo_key(1) != module._revl_memo_key(True)
    assert "_revl_uncached_double" in source


def test_a_cache_pure_fn_is_not_inlined():
    """The py tier folds small pure fns into their call sites (item 231a). A
    `cache pure` fn is excluded: inlining copies the body to call sites no memo
    can see, which would make the declaration silently do nothing."""
    src = ("fn twice(n: Int) -> Int cache pure { return n * 2 }\n"
           "fn outer(n: Int) -> Int { return twice(n) + 1 }\n")
    def outer_body(source: str) -> str:
        return source.split("def outer(n):", 1)[1].split("\n\n", 1)[0]

    assert "twice(n)" in outer_body(_emitted(src)[0])   # the call site survives
    # …while the same fn without the clause still inlines
    assert "twice(n)" not in outer_body(
        _emitted(src.replace(" cache pure", ""))[0])


def test_a_program_with_no_cache_pure_fn_emits_no_memo_table():
    source, _ = _emitted("fn twice(n: Int) -> Int { return n * 2 }\n")
    assert "_REVL_MEMO" not in source and "_revl_memo_key" not in source
