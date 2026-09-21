"""What a bare `emission` names in the attenuation fold (issue #1265, item 561).

A service method may declare an effect and decline to say what it reaches:

    service Store { emission fn put(k: Str, v: Str) }

Issue #1265 asks what the checker should make of that. The composition half
(PR #1292) made the shipped compositions declare their tokens. This is the
checker half, and the answer it records is not one of the three the issue
listed: the fold element is the SERVICE the method is declared on, because that
is the only name the boundary itself owns.

What it used instead was the CONSUMER's local `requires` key. Item 294 already
moved the DECLARED case off the key for a stated reason - two components wire
the same boundary under whatever key each likes, so comparing keys compares two
identifiers that name nothing in common - and pinned it with
`tests/formal_corpus/g4_spawn_widens_capability_same_key.rvl`. A method that
declares no token had none to move onto, so it stayed on the key, and the same
laundering stayed open one declaration weaker. This module measures that, and
measures the two arms that were not taken.

`docs/design/561-undeclared-emission-boundary.md` carries the argument.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl import cap_order, lower  # noqa: E402


# --------------------------------------------------------------- the programs
#
# One key, two services. `Boss` holds `Net`; `Worker` reaches `Kv`. Both spell
# the wiring key `net`, so a fold that names a crossing by the key it was
# reached through sees `net` on both sides and derives no widening.

_BODY = """
service Net {{ emission{net} fn call(u: Str) -> Int }}
service Kv  {{ emission{kv} fn put(k: Str) -> Int }}
service Task {{ emission fn go() -> Int }}
component Worker requires net: Kv provides task: Task {{
  provide task {{ fn go() {{ emit net.put("x") return 0 }} }}
}}
component Boss requires net: Net {{
  let w = effect spawn Worker with {{ }} undo w.dispose()
}}
"""

#: the two declarations of the SAME program. The only difference is whether the
#: two services name the boundaries they reach.
UNDECLARED = _BODY.format(net="", kv="")
DECLARED = _BODY.format(net="[netcap]", kv="[kvcap]")

#: one service, two keys: `Boss` wires `Kv` as `kv`, `Worker` wires it as
#: `store`. The same boundary reached through two spellings, which is the case
#: the declared namespace has always admitted.
_TWO_KEYS = """
service Kv {{ emission{kv} fn put(k: Str) -> Int }}
service Task {{ emission fn go() -> Int }}
component Worker requires store: Kv provides task: Task {{
  provide task {{ fn go() {{ emit store.put("x") return 0 }} }}
}}
component Boss requires kv: Kv {{
  let w = effect spawn Worker with {{ }} undo w.dispose()
}}
"""
TWO_KEYS_UNDECLARED = _TWO_KEYS.format(kv="")
TWO_KEYS_DECLARED = _TWO_KEYS.format(kv="[kvcap]")

#: the same `_BODY` program a third time, with ONE token declared on both
#: services. Parent and child then name one boundary, so the spawn narrows
#: nothing and the program is admitted. Held against `UNDECLARED` it is the
#: non-vacuity of the whole arm: the two sources differ only in the capability
#: list, and they get opposite verdicts.
ONE_SHARED_TOKEN = _BODY.format(net="[shared]", kv="[shared]")

#: the ordinary case: one service, one key, on both sides.
ONE_KEY_UNDECLARED = (TWO_KEYS_UNDECLARED
                      .replace("requires store: Kv", "requires kv: Kv")
                      .replace("emit store.put", "emit kv.put"))


def _verdict(src: str) -> str:
    try:
        compile_source(src, "t.rvl")
    except RevlError as exc:
        return f"REFUSE {exc}"
    return "ADMIT"


# ------------------------------------------------------------ the differential


def test_the_two_declarations_differ_only_in_the_capability_list():
    """Non-vacuity for the differential itself: the two sources are one program
    under two declarations, not two programs."""
    assert UNDECLARED != DECLARED
    assert DECLARED.replace("[netcap]", "").replace("[kvcap]", "") == UNDECLARED


def test_the_declared_spelling_refuses_the_widening():
    """The reference verdict, and the one this module measures against. It is
    `g4_spawn_widens_capability_same_key.rvl`'s rule: the boundary is the
    declared token, so parent and child name nothing in common."""
    verdict = _verdict(DECLARED)
    assert verdict.startswith("REFUSE")
    assert "granting it `kvcap`" in verdict
    assert "holds only `netcap`" in verdict


def test_the_undeclared_spelling_refuses_it_too():
    """The fix. Declining to name the boundary no longer decides the verdict;
    the element is the service, so the two sides still name nothing in common.
    """
    verdict = _verdict(UNDECLARED)
    assert verdict.startswith("REFUSE")
    assert "granting it `Kv`" in verdict
    assert "holds only `Net`" in verdict


def test_one_source_two_declarations_opposite_verdicts():
    """The non-vacuity of the arm, on one program rather than across two.

    `UNDECLARED` and `ONE_SHARED_TOKEN` are the same source modulo the
    capability list: bare on both services, and one token on both. The first is
    refused and the second is admitted, so the element this change introduces
    decides a verdict rather than sitting inert beside one, and it is not
    refusing every undeclared program either - the `svc:` element is compared,
    not merely present.
    """
    assert (ONE_SHARED_TOKEN.replace("[shared]", "") == UNDECLARED)
    assert _verdict(UNDECLARED).startswith("REFUSE")
    assert _verdict(ONE_SHARED_TOKEN) == "ADMIT"


def test_the_refusal_carries_the_capability_attenuation_code():
    """Same guarantee, same category as the declared corner: no new refusal
    code is registered by this change."""
    from revl.diagnostics import classify  # noqa: PLC0415

    with pytest.raises(RevlError) as excinfo:
        compile_source(UNDECLARED, "t.rvl")
    assert classify(excinfo.value)["code"] == "G4"


# ------------------------------------------------- the two arms that were not taken


def test_the_unnameable_star_arm_admits_the_widening(monkeypatch):
    """Issue #1265's own exit names `*` for this case, and on this tree it is a
    REGRESSION rather than a tightening.

    `cap_order.covers` gives `*` one clause - top of the order, covered only by
    `*` - so a held `*` covers a reached `*`. Resolving both sides of an
    undeclared emission to the unnameable boundary therefore admits the
    widening. `*` is the fail-closed element for a DISJOINTNESS question, which
    is a different question from the coverage one the fold asks.
    """
    assert cap_order.covers(cap_order.Cap("*", ()), cap_order.Cap("*", ()))
    monkeypatch.setattr(lower, "_undeclared_cap",
                        lambda service: cap_order.Cap("*", ()))
    assert _verdict(UNDECLARED) == "ADMIT"


def test_the_wiring_key_arm_admits_the_widening(monkeypatch):
    """And so does any namer that hands parent and child the same element for
    two different boundaries, which is what the local `requires` key did here:
    both components spell the key `net`.
    """
    monkeypatch.setattr(lower, "_undeclared_cap",
                        lambda service: cap_order.Cap("key:net", ()))
    assert _verdict(UNDECLARED) == "ADMIT"


def test_the_two_arms_do_not_move_the_declared_verdict(monkeypatch):
    """...and neither arm touches the declared corner, so the difference above
    is attributable to the undeclared element and to nothing else."""
    for element in (cap_order.Cap("*", ()), cap_order.Cap("key:net", ())):
        monkeypatch.setattr(lower, "_undeclared_cap", lambda service, e=element: e)
        assert _verdict(DECLARED).startswith("REFUSE")


# ----------------------------------------------------- what must stay admitted


def test_one_service_under_two_keys_is_admitted():
    """The half the fix must not cost, and the half it repairs: the same
    service reached through two different keys is ONE boundary. The key
    namespace refused this; the declared namespace never did."""
    assert _verdict(TWO_KEYS_UNDECLARED) == "ADMIT"


def test_the_declared_spelling_of_that_program_is_admitted_too():
    """...which is the reference verdict it now matches."""
    assert _verdict(TWO_KEYS_DECLARED) == "ADMIT"


def test_the_ordinary_one_key_case_is_admitted():
    """The spelling 459 of this corpus's 556 emission methods use. Refusing it
    is the arm the census below rules out."""
    assert _verdict(ONE_KEY_UNDECLARED) == "ADMIT"


def test_a_parent_holding_a_non_emitting_service_grants_nothing():
    """The same conflation one step further out: `Boss` requires a service that
    cannot emit at all, under the key the child emits through. Holding a
    non-emitting wiring is not holding an emission boundary."""
    src = """
service Plain { fn ping() -> Int }
service Kv { emission fn put(k: Str) -> Int }
service Task { emission fn go() -> Int }
component Worker requires net: Kv provides task: Task {
  provide task { fn go() { emit net.put("x") return 0 } }
}
component Boss requires net: Plain {
  let w = effect spawn Worker with { } undo w.dispose()
}
"""
    verdict = _verdict(src)
    assert verdict.startswith("REFUSE")
    assert "granting it `Kv`" in verdict


# ------------------------------------------------------------- the namespaces


def test_the_derived_namespace_is_unspellable_in_source():
    """The reason the element is namespaced at all: a capability token is a
    dotted identifier, so a `:` cannot be written, and a derived element can
    never be mistaken by `covers` for a declared boundary."""
    with pytest.raises(RevlError):
        compile_source(
            "service Kv { emission[%sKv] fn put(k: Str) -> Int }\n"
            % lower._UNDECLARED_NS, "t.rvl")


def test_the_declared_element_never_lands_in_the_derived_namespace():
    """...from the other side: `_cap_keyed` hands a declared token back
    untouched, so the two namespaces cannot collide."""
    assert not lower._cap_keyed("k", "netcap").to_str().startswith(
        lower._UNDECLARED_NS)
    assert lower._undeclared_cap("Net").to_str().startswith(
        lower._UNDECLARED_NS)


def test_an_unresolvable_service_degrades_to_the_unnameable_boundary():
    """Impossible on a validated IR, and fail-closed on both sides if it ever
    happened: nothing covers `*` as a reach, and `*` covers nothing but `*` as
    a held element."""
    assert lower._undeclared_cap(None).to_str() == "*"
    assert lower._undeclared_cap("").to_str() == "*"


def test_the_refusal_renders_the_service_rather_than_the_namespace():
    """`_cap_render` strips the namespace, so an operator reads a source-level
    name and never the internal spelling."""
    assert lower._cap_render(lower._undeclared_cap("Net")) == "Net"
    assert lower._UNDECLARED_NS not in _verdict(UNDECLARED)


# ----------------------------------------------------------------- the corpus


def _emission_census() -> tuple[list[str], list[str]]:
    """Every service method in the tree's `.rvl` files that declares an
    emission, split by whether it names a capability token."""
    from revl import parser  # noqa: PLC0415

    skip = {".git", "node_modules", "target", "__pycache__", ".lake"}
    bare: list[str] = []
    declared: list[str] = []
    for path in sorted(ROOT.rglob("*.rvl")):
        # any `.venv*` is an INSTALLED copy of the tree (the cordis job builds
        # one under `backends/python`, a lane may build its own): walking it
        # counts the same corpus twice under a different path.
        if skip & set(path.parts) or any(p.startswith(".venv")
                                         for p in path.parts):
            continue
        try:
            program = parser.Parser(path.read_text(encoding="utf-8"),
                                    str(path)).parse()
        except Exception:                                  # noqa: BLE001
            continue     # a fragment or a deliberately-rejected corpus member
        rel = str(path.relative_to(ROOT))
        for service in program.services:
            for method in service.methods.values():
                if not method.emission:
                    continue
                where = f"{rel}:{service.name}.{method.name}"
                (bare if method.capabilities is None else declared).append(where)
    return bare, declared


def test_refusing_a_bare_emission_outright_is_not_an_available_arm():
    """The third arm issue #1265 lists, costed on the corpus rather than
    argued. Bare is not a legacy spelling being phased out; it is the majority
    one, and most of it is in `bench/results`, which holds RECORDED MODEL
    OUTPUTS. A recorded output is evidence about what a model wrote against the
    language as it was: refusing to compile one destroys the thing it records.
    """
    bare, declared = _emission_census()
    assert len(bare) > len(declared), (len(bare), len(declared))
    recorded = [b for b in bare if b.startswith("bench/results/")]
    assert len(recorded) > len(bare) // 2, (len(recorded), len(bare))


def test_the_formal_corpus_carries_the_undeclared_corner():
    """The fixture family the differential harness and the Lean derived layer
    both read. `g4_spawn_widens_capability_same_key.rvl` is the declared
    corner; this is the undeclared one."""
    from revl import compile_files  # noqa: PLC0415

    fixture = (ROOT / "tests" / "formal_corpus"
               / "g4_spawn_widens_undeclared_emission_same_key.rvl")
    assert fixture.is_file()
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(fixture)])
    assert "granting it `Kv`" in str(excinfo.value)
