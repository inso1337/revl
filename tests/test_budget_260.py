"""Emission budgets, Slice 3 (item 260 §3).

docs/design/260-emission-cardinality-bounds.md. A budget on an emission is a
ceiling parameter on its capability token: `budget.requests`/`calls` reconciles
onto the SAME quantity the cardinality analysis proves (so the static check is
exactly "proved-max <= declared calls"), `budget.bytes`/`size` and `budget.time`
are runtime-enforced and carried verbatim into the attestation surface.

THE HIGH FIX (the load-bearing one, §3.3): budget attenuation does NOT ride the
crossing-coverage fold `covers_set`, which is CEILING-BLIND by construction (the
spawn surface strips ceilings). A child budget WIDER than its parent's - or a
DROPPED clause that silently widens to unbounded - is refused by a DEDICATED
ceiling-attenuation check over the unstripped `(T, P)` pairs. Every escape path
is tested here.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.audit_diff import audit_report  # noqa: E402
from revl.cardinality import cardinality  # noqa: E402


# --------------------------------------------------------------------------
# §3.1 grammar - a budget parses onto the ceiling params (requests -> calls,
# bytes -> size, time -> ms), and canonicalizes.
# --------------------------------------------------------------------------

def test_budget_sugar_canonicalizes_onto_ceiling_params():
    from revl import cap_order as co

    cap = co.parse_cap('network.call(host="api.x", requests=100, '
                       'bytes="10MB", time="2s")')
    # requests -> calls, bytes -> size (10MB = 10*1024*1024), time -> ms
    assert cap.param_map() == {"host": "api.x", "calls": 100,
                               "size": 10485760, "time": 2000}
    # requests and calls are ONE parameter: binding both is a duplicate
    with pytest.raises(co.CapError, match="duplicate"):
        co.make_cap("n.c", [("calls", 1), ("requests", 2)])


# --------------------------------------------------------------------------
# §3.2 the STATIC requests/calls check - proved-max <= declared calls
# --------------------------------------------------------------------------

def _prog(requests: int, emits: int) -> str:
    body = " ".join(f'emit net.call("{i}");' for i in range(emits))
    return f"""
service Net {{ emission[net(requests={requests})] fn call(u: Str) -> Int }}
service Svc {{ emission[net] fn run() -> Int }}
component C requires net: Net provides svc: Svc {{
  provide svc {{ fn run() -> Int {{ {body} return 0 }} }}
}}
"""


def test_static_over_budget_is_refused():
    """A `budget.requests=1` on an emission whose proved cardinality max is 2 is
    a red compile: proved-max (2) exceeds declared (1)."""
    with pytest.raises(RevlError) as exc:
        compile_source(_prog(requests=1, emits=2), "t.rvl")
    msg = str(exc.value)
    assert "may cross `net` up to 2 times per activation" in msg
    assert "calls=1" in msg


def test_within_budget_passes_and_shows_on_audit_surface():
    """A within-budget composition (2 <= 5) compiles, and the budget shows on the
    audit surface next to the cardinality table (verbatim, ceiling and all)."""
    ir = compile_source(_prog(requests=5, emits=2), "t.rvl")
    rep = audit_report(ir)
    # the proved count is reported against the budgeted token
    assert rep["cardinality"]["C"]["per_capability"] == {
        "net(calls=5)": {"bound": 2, "kind": "bounded"}}
    # the budget is carried verbatim on the boundary/attestation surface
    assert rep["boundary"]["C"]["capabilities"] == {
        "net.call": ["net(calls=5)"]}


def test_finite_budget_over_unbounded_body_is_refused():
    """A declared finite `calls` ceiling over a body whose crossing count is
    `unbounded` (here an arrow-mediated recursion with a non-decreasing fuel) is
    UNPROVABLE and refused - the feature turning a runaway loop into a red
    compile (§3.2)."""
    src = """
service Model { emission[model(requests=3)] fn complete(m: List[Str]) -> Str }
service Loop { emission[model] fn run(s: Str) -> Str }
fn r(msgs: List[Str], step: (List[Str]) -> Str, n: Int) -> Str {
  if (n <= 0) { return "s" }
  let x = step(msgs)
  return r(msgs, step, n)
}
component Agent requires model: Model provides agent: Loop {
  provide agent { fn run(s) -> Str {
    let msgs = [s]
    return r(msgs, msgs2 => emit model.complete(msgs2), 5)
  } }
}
"""
    with pytest.raises(RevlError) as exc:
        compile_source(src, "t.rvl")
    msg = str(exc.value)
    assert "not statically provable" in msg
    assert "calls=3" in msg


def test_budget_shows_on_text_audit(capsys):
    src = _prog(requests=5, emits=2)
    path = ROOT / "examples" / "_budget_tmp.rvl"
    try:
        path.write_text(src)
        code = main(["audit", str(path)])
        assert code == 0
        out = capsys.readouterr().out
        assert "cardinality: net(calls=5) <= 2 per activation" in out
    finally:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# §3.3 THE HIGH FIX - the DEDICATED ceiling-attenuation check, over the
# UNSTRIPPED pairs, NOT the ceiling-blind crossing-coverage fold `covers_set`.
# --------------------------------------------------------------------------

def _spawn_prog(child_req: str) -> str:
    return f"""
service NetTight {{ emission[net(requests=100)] fn call(u: Str) -> Int }}
service NetWide  {{ emission[net(requests=1000)] fn call(u: Str) -> Int }}
service NetNarrow{{ emission[net(requests=50)] fn call(u: Str) -> Int }}
service NetPlain {{ emission[net] fn call(u: Str) -> Int }}
service Worker {{ emission[net] fn go() -> Int }}
component Child requires net: {child_req} provides worker: Worker {{
  provide worker {{ fn go() -> Int {{ emit net.call("x"); return 0 }} }}
}}
component Parent requires net: NetTight {{
  let c = effect spawn Child with {{ }} undo c.dispose()
}}
"""


def test_wider_child_budget_is_refused_by_the_dedicated_check():
    """A child budget WIDER than the parent's (requests=1000 under requests=100)
    is refused. The message is the DEDICATED ceiling check's, not the
    crossing-coverage fold's - the fold is ceiling-blind and would admit it."""
    with pytest.raises(RevlError) as exc:
        compile_source(_spawn_prog("NetWide"), "t.rvl")
    msg = str(exc.value)
    assert "wider resource budget" in msg          # the dedicated check
    assert "widens `calls` to 1000 over the parent's 100" in msg
    # NOT the crossing-coverage refusal (which never sees the ceiling)
    assert "holds only" not in msg


def test_dropped_child_budget_clause_does_not_silently_widen():
    """A child that DROPS the `requests` clause does not silently widen to
    unbounded: a missing child ceiling reads as +inf (wider) and is refused by
    the dedicated check (§3.3, §5.3 - the escape the HIGH finding named)."""
    with pytest.raises(RevlError) as exc:
        compile_source(_spawn_prog("NetPlain"), "t.rvl")
    msg = str(exc.value)
    assert "wider resource budget" in msg
    assert "drops the `calls` budget" in msg
    assert "unbounded, hence wider" in msg


def test_narrower_child_budget_is_admitted():
    """A child budget within the parent's (requests=50 <= 100) is admitted -
    monotone narrowing is the sound direction."""
    ir = compile_source(_spawn_prog("NetNarrow"), "t.rvl")
    assert ir["manifest"]["instances"]  # spawned, admitted


def test_grandchild_budget_widening_is_caught_transitively():
    """Attenuation is checked at EVERY spawn edge over the transitively-closed
    reach, so a grandchild re-minting a wider budget than an ancestor holds is
    refused (a re-mint escape path)."""
    src = """
service NetTight {  emission[net(requests=100)]  fn call(u: Str) -> Int }
service NetWide  {  emission[net(requests=1000)] fn call(u: Str) -> Int }
service Worker { emission[net] fn go() -> Int }
component Grand requires net: NetWide provides worker: Worker {
  provide worker { fn go() -> Int { emit net.call("x"); return 0 } }
}
component Mid requires net: NetTight {
  let g = effect spawn Grand with { } undo g.dispose()
}
component Top requires net: NetTight {
  let m = effect spawn Mid with { } undo m.dispose()
}
"""
    with pytest.raises(RevlError) as exc:
        compile_source(src, "t.rvl")
    assert "wider resource budget" in str(exc.value)


def test_it_is_the_dedicated_check_not_the_ceiling_blind_fold():
    """Nail the HIGH fix directly: over the wider-child surface, the
    crossing-coverage fold `covers_set` (run on the STRIPPED projection) returns
    EMPTY - it is ceiling-blind and would admit the widening. Only the dedicated
    ceiling check flags it. This is the reconciliation §3.3 demands."""
    from revl import cap_order as co
    from revl.lower import _ceiling_attenuation_check, _strip_ceilings

    parent_held = {co.parse_cap("net(calls=100)")}
    child_reach = {co.parse_cap("net(calls=1000)")}
    # the ceiling-blind fold: strip, then covers - NOTHING uncovered
    assert co.covers_set(_strip_ceilings(parent_held),
                         _strip_ceilings(child_reach)) == []
    # the dedicated check: the widening IS flagged
    bad = _ceiling_attenuation_check(parent_held, child_reach)
    assert len(bad) == 1
    assert bad[0]["param"] == "calls"
    assert bad[0]["child"] == 1000 and bad[0]["parent"] == 100
    # and a dropped clause reads as +inf, hence wider
    dropped = _ceiling_attenuation_check(parent_held, {co.parse_cap("net")})
    assert dropped and dropped[0]["child"] is None


# --------------------------------------------------------------------------
# runtime-only budgets ride into the attestation surface
# --------------------------------------------------------------------------

def test_time_budget_is_carried_into_the_attestation():
    """`budget.time` is runtime-only (no static analogue) but is carried verbatim
    into the attestation surface (the audit `--json` boundary capabilities), as
    the capability's valuation (`time=2000` ms)."""
    src = """
service Net { emission[net(requests=5, time="2s")] fn call(u: Str) -> Int }
service Svc { emission[net] fn run() -> Int }
component C requires net: Net provides svc: Svc {
  provide svc { fn run() -> Int { emit net.call("a"); return 0 } }
}
"""
    ir = compile_source(src, "t.rvl")
    caps = audit_report(ir)["boundary"]["C"]["capabilities"]
    assert caps == {"net.call": ["net(calls=5,time=2000)"]}
    # and cardinality keys on the same budgeted token
    assert "net(calls=5,time=2000)" in cardinality(ir)["C"]["per_capability"]


# --------------------------------------------------------------------------
# additivity - a budget-free composition is byte-identical
# --------------------------------------------------------------------------

def test_budget_free_composition_is_byte_identical():
    """A composition with no budget clause is unaffected: no ceiling params
    anywhere on the audit/interchange surface, and a bare token round-trips
    exactly as before (the feature is inert unless a program opts in)."""
    from revl import cap_order as co

    # a bare token is unchanged by the new registry rows / aliases
    assert co.parse_cap("db.write").to_str() == "db.write"

    src = """
service DB { emission[db] fn exec(s: Str) -> Int }
service Svc { emission[db] fn run() -> Int }
component C requires db: DB provides svc: Svc {
  provide svc { fn run() -> Int { emit db.exec("a"); return 0 } }
}
"""
    rep = audit_report(compile_source(src, "t.rvl"))
    # no ceiling parameter leaks onto a budget-free surface
    blob = json.dumps(rep)
    assert "calls=" not in blob and "time=" not in blob and "size=" not in blob
    assert rep["cardinality"]["C"]["per_capability"] == {
        "db": {"bound": 1, "kind": "bounded"}}


# ---------------------------------------------------------------------------
# F2: a budget is scoped to a RESOURCE CONE, never to a bare token.
#
# Aggregating the parent's ceilings per token with `max` let a generous budget
# on one cone license a crossing on a sibling cone the parent barely holds.
# ---------------------------------------------------------------------------

def test_a_budget_on_a_sibling_cone_cannot_license_another_cone():
    """One service, two emission methods under one wiring key: `/a` capped at
    `calls=1`, `/b` at `calls=100`. The child takes 100 calls on `/a` — a 100x
    budget on the cone the parent holds one call of. Per-token `max` admits it;
    per-cone refuses it."""
    src = """
service Fs {
  emission[fs(calls=1,path="/a")] fn wa(row: Str) -> Int
  emission[fs(calls=100,path="/b")] fn wb(row: Str) -> Int
}
service ChildFs { emission[fs(calls=100,path="/a")] fn wa(row: Str) -> Int }
service Task { emission fn go() -> Int }
component Child requires store: ChildFs provides task: Task {
  provide task { fn go() { emit store.wa("x") return 0 } }
}
component Parent requires store: Fs {
  let c = effect spawn Child with { } undo c.dispose()
}
"""
    with pytest.raises(RevlError) as exc:
        compile_source(src, "b.rvl")
    msg = str(exc.value)
    assert "wider resource budget" in msg
    assert '`fs(calls=100,path="/a")` widens `calls` to 100 over the parent\'s 1' in msg


def test_the_cone_rule_is_existential_not_a_per_parameter_max():
    """A parent holding `(calls=1,size=100)` and `(calls=100,size=1)` on the SAME
    cone holds neither `calls=100` AND `size=100` together. One held cap must
    license the WHOLE crossing, so a per-parameter `max` over the covering caps
    would be unsound too."""
    from revl import cap_order as co
    from revl.lower import _ceiling_attenuation_check

    held = {co.parse_cap('fs(path="/a",calls=1,size=100)'),
            co.parse_cap('fs(path="/a",calls=100,size=1)')}
    bad = _ceiling_attenuation_check(
        held, {co.parse_cap('fs(path="/a",calls=100,size=100)')})
    assert bad, "no single held cap licenses calls=100 AND size=100"
    # either held cap fails exactly one parameter; the closest one is reported
    assert len(bad) == 1
    assert bad[0]["param"] in ("calls", "size")


def test_a_ceiling_free_covering_cap_still_licenses_the_crossing():
    """The no-false-alarm direction of the same rule: a parent holding an
    UNBOUNDED `fs` (no ceiling at all) genuinely holds every budget under it, so
    a capped sibling cone must not make the crossing look like a widening."""
    from revl import cap_order as co
    from revl.lower import _ceiling_attenuation_check

    held = {co.parse_cap("fs"), co.parse_cap('fs(path="/a",calls=1)')}
    assert _ceiling_attenuation_check(
        held, {co.parse_cap('fs(path="/a",calls=100)')}) == []
    # and a crossing on a cone the parent caps nowhere is likewise licensed
    held2 = {co.parse_cap('fs(path="/a",calls=1)'), co.parse_cap('fs(path="/b")')}
    assert _ceiling_attenuation_check(
        held2, {co.parse_cap('fs(path="/b",calls=100)')}) == []


def test_a_dropped_ceiling_on_the_covering_cone_is_still_refused():
    """Cone-scoping must not weaken the dropped-clause rule: a child that binds
    no ceiling on a cone the parent caps reads as `+inf`, hence wider."""
    from revl import cap_order as co
    from revl.lower import _ceiling_attenuation_check

    held = {co.parse_cap('fs(path="/a",calls=1)'), co.parse_cap('fs(path="/b",calls=100)')}
    bad = _ceiling_attenuation_check(held, {co.parse_cap('fs(path="/a")')})
    assert len(bad) == 1
    assert bad[0]["child"] is None and bad[0]["parent"] == 1
