"""Multi-realm `require` syntax (roadmap item 162) — frontend semantics.

    isolate <key> in realms("w1", "w2", "w3") strategy(round_robin)

The plural of the existing `isolate <key> in realm(<name>)`: one *required*
key is bound across N named realms, with an optional routing strategy the
runtime router (item 161) consumes. This is the CONSUMPTION side of the
load-balancer pattern — it records the binding + strategy in the IR and
verifies each named realm has a provider; it does NOT itself route.

Everything here is the executable spec: each acceptance pins the AST/IR shape,
each rejection is a refusal with a diagnostic. `realms`/`strategy` are ordinary
identifiers (NOT reserved words), so the reference KEYWORDS set — and the
selfhosted lexer that mirrors it — is untouched.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.lower import KNOWN_STRATEGIES  # noqa: E402

# Three providers, one per realm — G2 keeps exactly one provider per
# (key, realm), so `db` is provided once in each of w1/w2/w3.
PROVIDERS = """
service Database { fn get(k: Str) -> Str }
service Api { fn fetch(k: Str) -> Str }
component W1 provides db: Database { isolate db in realm("w1") provide db { fn get(k) = k } }
component W2 provides db: Database { isolate db in realm("w2") provide db { fn get(k) = k } }
component W3 provides db: Database { isolate db in realm("w3") provide db { fn get(k) = k } }
"""


def _consumer(bind: str) -> str:
    return PROVIDERS + f"""
component Consumer requires db: Database provides api: Api {{
  {bind}
  provide api {{ fn fetch(k) = db.get(k) }}
}}"""


def _by_name(ir, section="components"):
    if section == "manifest":
        return {c["name"]: c for c in ir["manifest"]["components"]}
    return {c["name"]: c for c in ir[section]}


# --------------------------------------------------------------- acceptance


def test_multi_realm_bind_parses_to_routes_ir():
    """`realms(...) strategy(...)` records the ordered realm list + strategy
    in the component IR, and mirrors it into the manifest entry."""
    ir = compile_source(
        _consumer('isolate db in realms("w1", "w2", "w3") strategy(round_robin)'),
        "route.rvl",
    )
    assert ir["ir_version"] == 2
    comp = _by_name(ir)["Consumer"]
    assert comp["routes"] == {
        "db": {"realms": ["w1", "w2", "w3"], "strategy": "round_robin"}
    }
    # the manifest entry carries the same binding for the runtime router
    entry = _by_name(ir, "manifest")["Consumer"]
    assert entry["routes"] == {
        "db": {"realms": ["w1", "w2", "w3"], "strategy": "round_robin"}
    }


def test_realm_order_is_preserved_as_written():
    """A router's rotation is defined over the list in declaration order, so
    the IR must keep the order the source wrote (not sort it)."""
    ir = compile_source(
        _consumer('isolate db in realms("w3", "w1", "w2")'),
        "order.rvl",
    )
    assert _by_name(ir)["Consumer"]["routes"]["db"]["realms"] == ["w3", "w1", "w2"]


def test_strategy_is_optional_and_records_none():
    """`strategy(...)` is optional; omitted, it records None ("router's
    default") rather than inventing one."""
    ir = compile_source(_consumer('isolate db in realms("w1", "w2")'), "nostrat.rvl")
    assert _by_name(ir)["Consumer"]["routes"]["db"] == {
        "realms": ["w1", "w2"],
        "strategy": None,
    }


@pytest.mark.parametrize("strategy", sorted(KNOWN_STRATEGIES))
def test_every_known_strategy_is_accepted(strategy):
    ir = compile_source(
        _consumer(f'isolate db in realms("w1", "w2") strategy({strategy})'),
        f"strat_{strategy}.rvl",
    )
    assert _by_name(ir)["Consumer"]["routes"]["db"]["strategy"] == strategy


def test_single_element_realms_is_the_degenerate_route():
    """`realms("w1")` (one target) is a valid degenerate route — the router
    just never rotates. It still verifies the one realm has a provider."""
    ir = compile_source(_consumer('isolate db in realms("w1")'), "one.rvl")
    assert _by_name(ir)["Consumer"]["routes"]["db"]["realms"] == ["w1"]


def test_providers_load_before_the_routing_consumer():
    """Every backend provider must be ACTIVE before the consumer that routes
    to it — one loadOrder edge per routed realm."""
    ir = compile_source(
        _consumer('isolate db in realms("w1", "w2", "w3") strategy(round_robin)'),
        "load.rvl",
    )
    order = ir["manifest"]["loadOrder"]
    assert order.index("Consumer") > order.index("W1")
    assert order.index("Consumer") > order.index("W2")
    assert order.index("Consumer") > order.index("W3")


# ------------------------------------------- per-realm provider verification


def test_missing_provider_in_one_named_realm_is_refused():
    """The core verification: a route names a realm with no provider → REFUSED
    with a diagnostic that names the key and the offending realm."""
    src = PROVIDERS + """
component Consumer requires db: Database provides api: Api {
  isolate db in realms("w1", "w2", "w3", "w4") strategy(round_robin)
  provide api { fn fetch(k) = db.get(k) }
}"""
    with pytest.raises(RevlError) as ei:
        compile_source(src, "missing.rvl")
    msg = str(ei.value)
    assert "realm `w4`" in msg
    assert "`db`" in msg
    # the other three realms, which DO have providers, are not blamed
    assert "realm `w1`" not in msg


def test_all_realms_provider_check_holds_g2_per_realm():
    """Verification is existence-per-realm; it does not relax G2. Two providers
    of `db` in the SAME realm is still the G2 conflict."""
    src = """
service Database { fn get(k: Str) -> Str }
component W1a provides db: Database { isolate db in realm("w1") provide db { fn get(k) = k } }
component W1b provides db: Database { isolate db in realm("w1") provide db { fn get(k) = k } }
"""
    with pytest.raises(RevlError, match="provision conflict"):
        compile_source(src, "g2.rvl")


# ------------------------------------------------------ syntactic rejections


def test_empty_realms_list_is_refused():
    with pytest.raises(RevlError, match="needs at least one realm label"):
        compile_source(_consumer("isolate db in realms()"), "empty.rvl")


def test_duplicate_realm_in_list_is_refused():
    with pytest.raises(RevlError, match="listed twice"):
        compile_source(_consumer('isolate db in realms("w1", "w1")'), "dup.rvl")


def test_dynamic_realm_label_is_refused():
    """A realm is a static string literal, exactly as for `realm(...)` — a
    config-derived realm would make G2 unsound."""
    with pytest.raises(RevlError, match="static string literal"):
        compile_source(_consumer("isolate db in realms(w1, w2)"), "dyn.rvl")


@pytest.mark.parametrize("label", ["bad\\\\label", "../escape", "é"])
def test_realm_label_namespace_syntax_is_refused(label):
    with pytest.raises(RevlError, match="invalid realm label"):
        compile_source(_consumer(f'isolate db in realm("{label}")'), "bad-label.rvl")


# ------------------------------------------------------- semantic rejections


def test_unknown_strategy_is_refused():
    """A strategy is validated against a closed set at compile time, so a typo
    is a refusal, not a silent runtime fallback."""
    with pytest.raises(RevlError) as ei:
        compile_source(
            _consumer('isolate db in realms("w1", "w2") strategy(round_robbin)'),
            "typo.rvl",
        )
    assert "unknown routing strategy `round_robbin`" in str(ei.value)


def test_routing_a_provision_is_refused():
    """Routing distributes a *consumer's* dependency; a provision has one
    installed instance in one realm (G2), so routing it is meaningless."""
    src = PROVIDERS + """
component Bad provides db: Database {
  isolate db in realms("w1", "w2")
  provide db { fn get(k) = k }
}"""
    with pytest.raises(RevlError, match=r"routes a \*required\* key"):
        compile_source(src, "prov.rvl")


def test_routing_and_providing_the_same_key_with_a_body_is_refused():
    """Regression for #449. A `routes`-carrying component (the router shape:
    requires + provides the SAME key, distributed across realms) is realized
    at load as a `_Router` proxy — the sole downstream provider (G2) — and is
    never plugged as a fiber. A hand-written `provide <routed key> { … }` body
    used to compile, admit, pass G4, then be SILENTLY DISCARDED at load
    (`audit.seen() == 0`): the author's body never ran and nothing said so.

    The header `provides <key>: <Service>` clause is all the route needs, so
    the discarded body is refused BY NAME at admission (the router keeps only
    the header clause — see `stdlib/router.rvl`)."""
    src = PROVIDERS + """
component RoundRobin requires db: Database provides db: Database {
  isolate db in realms("w1", "w2", "w3") strategy(round_robin)
  provide db { fn get(k) = db.get(k) }
}"""
    with pytest.raises(RevlError, match=r"is silently discarded"):
        compile_source(src, "discard.rvl")


def test_router_with_only_the_header_provides_clause_is_admitted():
    """The corrected router shape — routes the key and declares it in the
    `provides` header, but carries NO `provide` body — compiles and records
    the `routes` IR (the `_Router` proxy provides it at load)."""
    src = PROVIDERS + """
component RoundRobin requires db: Database provides db: Database {
  isolate db in realms("w1", "w2", "w3") strategy(round_robin)
}"""
    ir = compile_source(src, "header_only.rvl")
    rr = _by_name(ir)["RoundRobin"]
    assert rr["routes"] == {"db": {"realms": ["w1", "w2", "w3"],
                                   "strategy": "round_robin"}}
    assert rr["provides"] == {"db": "Database"}
    assert rr["body"] == []


def test_routing_an_undeclared_key_is_refused():
    with pytest.raises(RevlError, match="not a declared requirement"):
        compile_source(_consumer('isolate zzz in realms("w1", "w2")'), "undecl.rvl")


def test_route_after_an_action_is_refused_prelude_rule():
    src = PROVIDERS + """
component Consumer requires db: Database provides api: Api {
  provide api { fn fetch(k) = db.get(k) }
  isolate db in realms("w1", "w2")
}"""
    with pytest.raises(RevlError, match="must precede every effect"):
        compile_source(src, "prelude.rvl")


def test_a_key_cannot_be_both_isolated_and_routed():
    src = PROVIDERS + """
component Consumer requires db: Database provides api: Api {
  isolate db in realm("w1")
  isolate db in realms("w1", "w2")
  provide api { fn fetch(k) = db.get(k) }
}"""
    with pytest.raises(RevlError, match="already isolated to a single realm"):
        compile_source(src, "both.rvl")


def test_a_key_cannot_be_routed_twice():
    src = PROVIDERS + """
component Consumer requires db: Database provides api: Api {
  isolate db in realms("w1", "w2")
  isolate db in realms("w1", "w2")
  provide api { fn fetch(k) = db.get(k) }
}"""
    with pytest.raises(RevlError, match="routed twice"):
        compile_source(src, "twice.rvl")


# ---------------------------------------------- additivity / byte-identity


def test_single_realm_isolate_is_unchanged():
    """The existing singular `realm(...)` form still lowers to the flat
    `isolate` map and carries NO `routes` key."""
    src = """
service Kv { fn get(k: Str) -> Str }
component Store provides kv: Kv {
  isolate kv in realm("tenant_a")
  provide kv { fn get(k) = k }
}"""
    ir = compile_source(src, "single.rvl")
    comp = _by_name(ir)["Store"]
    assert comp["isolate"] == {"kv": "tenant_a"}
    assert "routes" not in comp
    entry = _by_name(ir, "manifest")["Store"]
    assert entry["isolate"] == {"kv": "tenant_a"}
    assert "routes" not in entry


def test_plain_program_stays_v1_with_no_route_fields():
    """A program with no realm feature at all is byte-identical to before —
    v1, and neither `isolate` nor `routes` appears anywhere."""
    src = """
service Kv { fn get(k: Str) -> Str }
component Store provides kv: Kv { provide kv { fn get(k) = k } }
component App requires kv: Kv provides api: Kv {
  provide api { fn get(k) = kv.get(k) }
}"""
    ir = compile_source(src, "plain.rvl")
    assert ir["ir_version"] == 1
    for comp in ir["components"]:
        assert "isolate" not in comp and "routes" not in comp
    for entry in ir["manifest"]["components"]:
        assert "routes" not in entry


def test_realms_and_strategy_remain_ordinary_identifiers():
    """`realms`/`strategy` are NOT reserved — a program using either as a
    field or parameter name stays valid (keeps the KEYWORDS set untouched)."""
    src = """
service S { fn f(realms: Str, strategy: Str) -> Str }
component C provides s: S {
  config { realms: Str = "x", strategy: Int = 3 }
  provide s { fn f(realms, strategy) = realms }
}"""
    ir = compile_source(src, "names.rvl")
    assert ir["ir_version"] == 1
