"""Per-emitter dispatcher conformance map (roadmap item 76a).

Every backend ships one or more *expression dispatchers* (python: component
vs fn-body renderers; wasm: three), and nothing used to mark which IR kinds
each must handle — the record-update run patched one of python's two paths and
shipped "unsupported expression kind 'record_update'" on the other, and two
findings files independently asked for the same fix. Each emit.py now carries
the map as *data* (`EXPR_DISPATCHERS` / `EXPR_REFUSED` /
`EXPR_REFUSED_DOCUMENT`); this file checks that data against the frontend's
schema (src/revl/lower.py: `EXPR_KINDS`, `EXPR_KINDS_FN`,
`EXPR_KINDS_COMPONENT`), so "did you patch both paths" is a red test:

1. **Coverage / position rule** — a kind the frontend can produce in fn-body
   positions must be handled or deliberately refused by every dispatcher that
   serves fn positions on the tier; the same for component positions. A new
   expression kind added to the frontend schema without a registration in some
   backend is a red test on every tier.
2. **Declared-handled kinds really render** — feeding a minimal node of a
   declared-handled kind through its dispatcher must not raise the
   "unsupported/unknown expression kind" fall-through (other, context errors
   are tolerated and reported: the fall-through is the bug class this test
   exists for).
3. **Declared refusals are named, not fall-throughs** — a deliberately refused
   kind must raise a tier-limit EmitError that names the limit, never the
   generic unknown-kind error.
4. **Document-level refusals** (`hole`) are rejected by `emit()` itself.
5. **The records failure mode is pinned** — the exact bug (a pure kind handled
   by python's component renderer but missing from the fn-body renderer) is
   simulated and asserted red, and asserted green without the simulation.

The wasm tier's deliberate absences are listed explicitly in its tables
(host builtins, the Map value type, record_update, arrow values, `?.`), each
with a named refusal in the emitter.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from _backend_import import backend_emitter  # noqa: E402
from revl.lower import EXPR_KINDS_COMPONENT, EXPR_KINDS_FN  # noqa: E402

TIERS = ("python", "typescript", "rust", "java", "go", "wasm")

# the dispatcher-name → position roles. A name starting with "fn" (or the
# single-renderer "renderer") serves fn positions; "component" serves
# component positions; a single renderer serves both.
_FN_ROLE = ("renderer",)
_COMPONENT_ROLE = ("renderer", "component")

# ── minimal nodes (self-sufficient: no context beyond the per-tier setup) ────
LIT = {"kind": "lit", "value": 1}
LIST = {"kind": "list", "items": [LIT]}
RECORD = {"kind": "record", "fields": [("a", LIT)]}

MINIMAL_NODES: dict[str, dict] = {
    "lit": LIT,
    "var": {"kind": "var", "name": "x"},
    "name": {"kind": "name", "id": "x"},
    "bin": {"kind": "bin", "op": "+", "left": LIT, "right": LIT},
    "un": {"kind": "un", "op": "-", "operand": LIT},
    "if": {"kind": "if", "cond": {"kind": "lit", "value": True},
           "then": LIT, "else": LIT},
    "list": LIST,
    "record": RECORD,
    "record_update": {"kind": "record_update", "base": RECORD,
                      "updates": [("a", LIT)]},
    "index": {"kind": "index", "target": LIST, "index": LIT},
    "field": {"kind": "field", "target": {"kind": "var", "name": "r"},
              "name": "a"},
    "builtin": {"kind": "builtin", "method": "push", "target": LIST,
                "args": [LIT]},
    "maplit": {"kind": "maplit", "entries": []},
    "match": {"kind": "match", "scrutinee": LIT, "arms": []},
    "adt": {"kind": "adt", "type": "Result", "case": "Ok", "args": [LIT]},
    "arrow": {"kind": "arrow", "params": ["p"], "captures": [],
              "body": {"kind": "var", "name": "p"}},
    "interp": {"kind": "interp", "parts": [("text", "hi"), ("expr", LIT)]},
    "optfield": {"kind": "optfield", "target": {"kind": "var", "name": "o"},
                 "name": "a"},
    "optcall": {"kind": "optcall", "target": {"kind": "var", "name": "o"},
                "method": "push", "args": [LIT]},
    "len": {"kind": "len", "target": LIST},
    "call": {"kind": "call", "callee": {"kind": "var", "name": "f"},
             "args": [LIT]},
    "req": {"kind": "req", "name": "s"},
    "config": {"kind": "config", "field": "url"},
    "host": {"kind": "host", "fn": "Map.new", "args": []},
    "format": {"kind": "format", "template": "hi", "args": []},
    "fn": {"kind": "fn", "name": "f", "args": [LIT]},
    "spawn": {"kind": "spawn", "component": "Child", "config": {},
              "realms": []},
    "instance-get": {"kind": "instance-get",
                     "target": {"kind": "var", "name": "h"},
                     "key": "k", "service": "Svc"},
    "hole": {"kind": "hole", "type": "Int", "file": "probe", "line": 1},
}

RECORD_TYPE = {"R": {"kind": "record", "fields": {"a": "Int"}}}

# per-tier node overrides: minimal nodes whose context-free shape trips a
# context check on a specific tier. Each override keeps the KIND identical and
# only supplies the context the tier's renderer needs (a declared type, a
# zero-arg call, a tagged scrutinee), so the rendered kind is the same one the
# table declares.
NODE_OVERRIDES: dict[str, dict[str, dict]] = {
    "wasm": {
        "adt": {"kind": "adt", "type": "Result[Int, Str]", "case": "Ok",
                "args": [LIT]},
        "call": {"kind": "call", "callee": {"kind": "var", "name": "f"},
                 "args": []},
        "fn": {"kind": "fn", "name": "f", "args": []},
        "match": {"kind": "match",
                  "scrutinee": {"kind": "var", "name": "o"},
                  "arms": [{"pattern": "Some", "bind": "v", "payload_type": "Int",
                            "body": {"kind": "var", "name": "v"}}]},
        "field": {"kind": "field", "target": {"kind": "var", "name": "r"},
                  "name": "a"},
    },
    "go": {
        # the stc-go component tier lowers `.length` only; the frontend reaches
        # `field` there only on sized values (records are refused first)
        "field": {"kind": "field", "target": {"kind": "var", "name": "xs"},
                  "name": "length"},
        # an empty Map is pinned by its expected type (roadmap item 76b)
        "maplit": {"kind": "maplit", "entries": []},
    },
}

# fall-through messages: the bug class the test exists to turn red.
_FALLTHROUGH = (
    "unsupported expression kind",
    "unknown expression kind",
    "unsupported v3 expression kind",
    "unsupported expr kind",
)


def _is_fallthrough(exc: BaseException) -> bool:
    return any(part in str(exc) for part in _FALLTHROUGH)


# ── per-tier render contexts ─────────────────────────────────────────────────

_COMPONENT = {
    "name": "C",
    "config": [{"name": "url", "type": "Str", "default": None}],
    "requires": {"s": "Svc"},
    "provides": {},
    "body": [],
}
_SERVICES = {"Svc": {"methods": {"m": {"params": [], "returns": "Int",
                                       "emission": False}}}}


def _python_contexts(module):
    comp = module._ComponentEmitter(_COMPONENT, _SERVICES)
    return {
        "component": (lambda node, expected: comp._expr(node, "conformance")),
        "fn": (lambda node, expected: module._expr(node)),
    }


def _typescript_contexts(module):
    scope = module._Scope(component=_COMPONENT)
    scope.locals.update({"x", "h", "o", "xs", "r"})
    ctx = module._Ctx({}, [{"name": "f", "params": [], "returns": "Int"}], [],
                      component_scope=scope)
    return {
        "renderer": (lambda node, expected: module._expr(node, ctx)),
    }


def _rust_contexts(module):
    env = module._Env(component=_COMPONENT, services=_SERVICES,
                      types=RECORD_TYPE, functions=[], externs=[],
                      components=[])
    return {
        "renderer": (lambda node, expected: module._expr(node, env)),
    }


def _java_contexts(module):
    ctx = module._V3Ctx(RECORD_TYPE, [], [],
                        components=[{"name": "Child", "config": [],
                                     "requires": {}, "provides": {},
                                     "body": []}])
    return {
        "renderer": (lambda node, expected: module._expr(node, ctx, None, None)),
    }


def _go_contexts(module):
    env = module._Env(binds={"x"}, reqs={"s"}, config_fields={"url"},
                      params={"x"})
    v3 = module._V3GoCtx(RECORD_TYPE, [], [])
    v3.var_types["o"] = "Opt[R]"
    v3.var_types["x"] = "Int"
    return {
        "component": (lambda node, expected: module._expr(node, env, expected)),
        "fn": (lambda node, expected: module._go_v3_expr(node, v3, expected)),
    }


def _wasm_contexts(module):
    # the v3 engine keeps per-function counters/pool state that the component
    # path initialises via `_open_function`; a bare render needs the same
    # priming (mirrors backends/wasm/emit.py `_open_function`).
    def prime_v3(v3):
        v3._tmp = "__revl_tmp"
        v3._tmp_stack = []
        v3._tmp_extra = set()
        v3._arrows = {}
        v3._arrow_counter = 0
        v3._match_counter = 0
        v3._cdiv_counter = 0
        v3._loop_counter = 0
        v3._for_temps = []
        v3._local_types = {}
        v3.literal_offsets = {"hi": 0, "": 0}
        return v3

    fn_decl = {"name": "f", "params": [], "returns": "Int",
               "body": [{"step": "return", "expr": {"kind": "lit", "value": 1}}]}
    comp = module._ComponentEmitter(
        {"name": "C", "requires": {"s": "Svc"}, "config": [], "provides": {},
         "body": []},
        _SERVICES,
        types=dict(RECORD_TYPE),
        functions=[fn_decl],
        is_template=False,
        spawn_targets={"Child": []},
    )
    # a spawn *target* template is the one place a `config` read is lowerable
    # (the instantiation-config channel); the render picks it for `config` nodes
    comp_template = module._ComponentEmitter(
        {"name": "T", "config": [{"name": "url", "type": "Int", "default": 1}],
         "requires": {}, "provides": {}, "body": []},
        _SERVICES,
        types=dict(RECORD_TYPE),
        is_template=True,
        spawn_targets={"Child": []},
    )
    prime_v3(comp.v3)
    prime_v3(comp_template.v3)
    slots = {"x": "l_x", "h": "l_h", "r": "l_r", "o": "l_o", "xs": "l_xs"}
    stypes = {"x": "Int", "h": "Int", "r": "R", "o": "Opt[Int]",
              "xs": "List[Int]"}

    def comp_render(node, expected):
        target = comp_template if node.get("kind") == "config" else comp
        return target._expr(node, dict(slots), "conformance", dict(stypes))

    v3 = prime_v3(module._V3Emitter(RECORD_TYPE, [fn_decl], [], []))
    v3_scope = module._Scope(dict(slots), dict(stypes))

    def v3_render(node, expected):
        return v3._expr(node, v3_scope, "conformance", expected)

    def v3_infer(node, expected):
        return v3._infer_type(node, v3_scope, expected)

    return {
        "component": comp_render,
        "fn": v3_render,
        "fn-infer": v3_infer,
    }


_CONTEXT_BUILDERS = {
    "python": _python_contexts,
    "typescript": _typescript_contexts,
    "rust": _rust_contexts,
    "java": _java_contexts,
    "go": _go_contexts,
    "wasm": _wasm_contexts,
}


def _tier_tables(tier: str):
    """(module, EXPR_DISPATCHERS, EXPR_REFUSED) with EXPR_REFUSED normalised to
    a per-dispatcher dict. python/typescript/rust/java declare their refusals
    as a tier-level frozenset (a single renderer, or refusals that apply to
    every dispatcher); go/wasm declare them per dispatcher."""
    module = backend_emitter(tier)
    handled = module.EXPR_DISPATCHERS
    refused = module.EXPR_REFUSED
    if isinstance(refused, (set, frozenset)):
        refused = {name: frozenset(refused) for name in handled}
    return (module, handled, refused)


def _refused_document(tier: str) -> frozenset:
    module = backend_emitter(tier)
    if hasattr(module, "EXPR_REFUSED_DOCUMENT"):
        return module.EXPR_REFUSED_DOCUMENT
    return frozenset(module.EXPR_REFUSED) if isinstance(module.EXPR_REFUSED, (set, frozenset)) else frozenset()


# ── 1. coverage and the position rule ────────────────────────────────────────

def _schema():
    """Read the schema from the module so a test can monkeypatch it."""
    import revl.lower as lower_mod
    return lower_mod.EXPR_KINDS


def _coverage_gap(tier: str) -> list[str]:
    """Schema kinds the tier neither handles nor deliberately refuses anywhere."""
    module, handled, refused = _tier_tables(tier)
    doc_refused = _refused_document(tier)
    all_refused = set().union(*(set(v) for v in refused.values())) | set(doc_refused)
    covered = set().union(*(set(v) for v in handled.values())) | all_refused
    return sorted(set(_schema()) - covered)


@pytest.mark.parametrize("tier", TIERS)
def test_tables_cover_the_schema(tier):
    missing = _coverage_gap(tier)
    assert not missing, f"{tier}: schema kinds with no home: {missing}"
    module, handled, refused = _tier_tables(tier)
    # a declared refusal must not also be declared handled in the same dispatcher
    for name, kinds in handled.items():
        overlap = set(kinds) & set(refused.get(name, ()))
        assert not overlap, f"{tier} {name}: kind in both handled and refused: {sorted(overlap)}"
    # every declared kind must be a real schema kind (no typos, no legacy kinds)
    for name, kinds in handled.items():
        unknown = set(kinds) - set(_schema())
        assert not unknown, f"{tier} {name}: declared kinds outside the schema: {sorted(unknown)}"
    for name, kinds in refused.items():
        unknown = set(kinds) - set(_schema())
        assert not unknown, f"{tier} {name}: refused kinds outside the schema: {sorted(unknown)}"


def test_new_kind_without_registration_is_a_red_test(monkeypatch):
    """The registration point: a new expression kind added to the frontend
    schema but not registered in any backend's tables must be a red test on
    every tier — that is the 'did you patch both paths' gate for brand-new
    kinds. (The kinds live in the schema sets in src/revl/lower.py; this test
    simulates adding one and asserts every tier's coverage check fails.)"""
    import revl.lower as lower_mod
    monkeypatch.setattr(lower_mod, "EXPR_KINDS",
                        lower_mod.EXPR_KINDS | {"frobnicate"})
    monkeypatch.setattr(lower_mod, "EXPR_KINDS_COMPONENT",
                        lower_mod.EXPR_KINDS_COMPONENT | {"frobnicate"})
    for tier in TIERS:
        assert _coverage_gap(tier) == ["frobnicate"], (
            f"{tier}: a schema kind nobody registered must be a coverage gap"
        )


@pytest.mark.parametrize("tier", TIERS)
def test_position_rule_every_dispatcher_covers_its_positions(tier):
    """A kind the frontend can produce in a position must be handled or
    deliberately refused by every dispatcher that serves that position."""
    module, handled, refused = _tier_tables(tier)
    doc_refused = _refused_document(tier)
    for name, kinds in handled.items():
        required = set(EXPR_KINDS_FN) if (name in _FN_ROLE or name.startswith("fn")) else set()
        if name in _COMPONENT_ROLE or name in ("component",):
            required |= set(EXPR_KINDS_COMPONENT)
        covered = set(kinds) | set(refused.get(name, ())) | set(doc_refused)
        missing = required - covered
        assert not missing, (
            f"{tier} {name}: frontend can produce these in its positions but the "
            f"dispatcher neither handles nor deliberately refuses them: "
            f"{sorted(missing)}"
        )


# ── 2/3. behavioral checks: declared handled kinds render, refusals are named ─

# kinds whose render needs an expected (pinning) surface type on a tier
EXPECTED_HINTS: dict[str, dict[str, dict[str, str]]] = {
    "go": {
        "component": {"maplit": "Map[Str, Int]"},
        "fn": {"maplit": "Map[Str, Int]"},
    },
}


def _render_one(tier: str, dispatcher: str, kind: str):
    module = backend_emitter(tier)
    render = _CONTEXT_BUILDERS[tier](module)[dispatcher]
    node = NODE_OVERRIDES.get(tier, {}).get(kind, MINIMAL_NODES[kind])
    expected = EXPECTED_HINTS.get(tier, {}).get(dispatcher, {}).get(kind)
    try:
        render(node, expected)
        return None
    except Exception as exc:  # noqa: BLE001
        return exc


@pytest.mark.parametrize("tier", TIERS)
def test_declared_handled_kinds_render(tier, capsys):
    """Every kind a dispatcher declares handled must not raise the
    unknown-kind fall-through when rendered. Context errors (a missing declared
    type, an unseeded engine table) are tolerated and reported: the fall-through
    is the failure mode this test exists to turn red."""
    module, handled, _ = _tier_tables(tier)
    failures = []
    for name, kinds in sorted(handled.items()):
        for kind in sorted(kinds):
            exc = _render_one(tier, name, kind)
            if exc is not None and _is_fallthrough(exc):
                failures.append(f"{tier}.{name}.{kind}: {type(exc).__name__}: {exc}")
            elif exc is not None:
                print(f"  note: {tier}.{name}.{kind}: non-fallthrough error: {type(exc).__name__}: {exc}")
    assert not failures, "declared-handled kinds that fall through to the unknown-kind error:\n" + "\n".join(failures)


@pytest.mark.parametrize("tier", TIERS)
def test_declared_refusals_are_named(tier):
    """A deliberately refused kind must raise a named tier-limit EmitError, not
    the generic unknown-kind fall-through. Document-level refusals (`hole`)
    never reach a dispatcher — emit() itself rejects the document, which
    `test_document_refused_kinds_are_rejected_by_emit` covers."""
    module, _, refused = _tier_tables(tier)
    doc_refused = _refused_document(tier)
    failures = []
    for name, kinds in sorted(refused.items()):
        for kind in sorted(kinds):
            if kind in doc_refused:
                continue
            exc = _render_one(tier, name, kind)
            if exc is None:
                failures.append(f"{tier}.{name}.{kind}: rendered OK but declared refused")
            elif _is_fallthrough(exc):
                failures.append(f"{tier}.{name}.{kind}: fell through instead of naming the limit: {exc}")
    assert not failures, "declared refusals that do not name the tier limit:\n" + "\n".join(failures)


# ── 4. document-level refusals (hole) ────────────────────────────────────────

@pytest.mark.parametrize("tier", TIERS)
def test_document_refused_kinds_are_rejected_by_emit(tier):
    """A kind refused at the document level (hole) must make emit() refuse the
    whole document with a named error — never emit a module containing it."""
    module = backend_emitter(tier)
    doc_refused = _refused_document(tier)
    if not doc_refused:
        pytest.skip(f"{tier} declares no document-level refusals")
    ir = {
        "ir_version": 3,
        "services": {},
        "types": {},
        "functions": [{
            "name": "f", "params": [], "returns": "Int",
            "body": [{"step": "return", "expr": MINIMAL_NODES["hole"]}],
        }],
        "externs": [], "tests": [], "components": [],
    }
    with pytest.raises(Exception) as excinfo:  # noqa: BLE001
        module.emit(ir)
    assert not _is_fallthrough(excinfo.value), (
        f"{tier}: document-level refusal must name the limit, got a fall-through: {excinfo.value}"
    )


# ── 5. the records failure mode, pinned ──────────────────────────────────────

def _records_conformance_pass() -> bool:
    """Run the handled-kind check for python's fn dispatcher in isolation:
    does `record_update` render without the fall-through?"""
    exc = _render_one("python", "fn", "record_update")
    return exc is None or not _is_fallthrough(exc)


def test_records_failure_mode_is_red_when_one_path_is_missing(monkeypatch):
    """The exact bug from findings-records: `record_update` handled by python's
    component renderer but missing from the fn-body renderer. Simulate the
    pre-fix state by making the fn renderer raise on record_update; the
    conformance check must go red. This is the pin that keeps 'did you patch
    both paths' a red test."""
    module = backend_emitter("python")
    original = module._expr

    def broken(node):
        if isinstance(node, dict) and node.get("kind") == "record_update":
            raise module.EmitError("unsupported expression kind 'record_update'")
        return original(node)

    monkeypatch.setattr(module, "_expr", broken)
    assert not _records_conformance_pass(), (
        "conformance must go red when record_update is missing from one of "
        "python's two dispatchers"
    )


def test_records_failure_mode_is_green_with_both_paths():
    """After the fix (both python dispatchers handle record_update), the same
    check is green — the red/green pair is the regression lock."""
    assert _records_conformance_pass(), (
        "record_update must render through python's fn-body dispatcher without "
        "the unknown-kind fall-through"
    )
