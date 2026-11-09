"""An untrusted edit may not drop a requirer's `realms(...)` route (issue #3121).

`check_no_realm_removal` compares only the `isolate` map: a single-realm
placement. The item-162 plural, `isolate <key> in realms("w1", "w2")
strategy(...)`, is lowered into a different IR field, `routes`, so an edit,
swap or undo that deletes the route from a surviving requirer, or shrinks it
from `realms("w1", "w2")` to `realms("w1")` or to a single `realm(...)`, passed
the check with no refusal. The requirer then binds a shared-realm (or
single-realm) provider of the key, which the same untrusted author may add.

The check now reads each `routes[key].realms` entry as a placement of that
key, so a surviving requirer must keep every realm the admitted generation
routed it across. The policy's `component_realms` reads the same field: a
realm-scoped rule selects a routed requirer by every realm it reaches, so the
realm-set walk counts route realms too and refuses a route renamed onto a
shared-realm key.

Controls: a trusted author may drop the route, an edit that keeps the route
(or widens it) is admitted, and a withdrawn component keeps its exemption.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.admit_profile import (  # noqa: E402
    AdmissionProfile,
    check_no_realm_removal,
)
from revl.errors import RevlError  # noqa: E402
from revl.policy import component_realms  # noqa: E402

REFUSAL = "cannot remove the realm placement"
SHRINK = "cannot shrink the realm set"

PROVIDERS = """
service Ops { fn get(k: Str) -> Str }
component W1 provides ops: Ops {
  isolate ops in realm("w1")
  provide ops { fn get(k) = k }
}
component W2 provides ops: Ops {
  isolate ops in realm("w2")
  provide ops { fn get(k) = k }
}
"""

SHARED_PROVIDER = """
component SharedOps provides ops: Ops {
  provide ops { fn get(k) = k }
}
"""


def _source(route: str, header: str = "component Router requires ops: Ops",
            with_shared: bool = False) -> str:
    body = f"  {route}\n" if route else ""
    return (PROVIDERS + (SHARED_PROVIDER if with_shared else "")
            + f"{header} {{\n{body}}}\n")


def _ir(route: str, **kwargs) -> dict:
    return compile_source(_source(route, **kwargs), "router.rvl")


def _untrusted() -> AdmissionProfile:
    return AdmissionProfile.untrusted_author(())


def _refused(admitted: dict, candidate: dict,
             profile: AdmissionProfile | None = None) -> RevlError:
    with pytest.raises(RevlError) as refused:
        check_no_realm_removal(admitted, candidate,
                               _untrusted() if profile is None else profile)
    return refused.value


ADMITTED_ROUTE = 'isolate ops in realms("w1", "w2") strategy(round_robin)'


def _comp(name: str, isolate: dict | None = None,
          provides: dict | None = None, requires: dict | None = None,
          routes: dict | None = None) -> dict:
    """One component of a hand-built IR, for placements the compiler will not
    write (a route that keeps the component's realm set while shrinking one
    key's, a mixed provision and route loss)."""
    comp = {"name": name, "source": f"{name}.rvl",
            "isolate": dict(isolate or {}), "provides": dict(provides or {}),
            "requires": dict(requires or {})}
    if routes:
        comp["routes"] = routes
    return comp


# --------------------------------------------------------------- the refusal


def test_dropped_route_is_refused_by_name():
    """The issue's reproduction: the same composition without the route line.
    The admitted generation routes `ops` across w1 and w2; the candidate leaves
    the requirer admitted and binds the key in the shared realm."""
    admitted = _ir(ADMITTED_ROUTE)
    candidate = _ir("")
    refused = _refused(admitted, candidate)
    assert REFUSAL in refused.message, refused.message
    assert "`Router`" in refused.message, refused.message
    assert 'realms("w1", "w2")' in refused.message, refused.message
    assert "the shared realm" in refused.message, refused.message
    assert "`ops` requirement" in refused.message, refused.message
    assert refused.code == "G9", refused.code
    # the navigable refusal names both realms the requirer left
    realms = refused.navigate["refused"]["realms"]
    assert realms == ["w1", "w2"], realms


def test_dropped_route_with_a_shared_provider_added_is_refused():
    """The exploit shape: the candidate drops the route and adds a shared-realm
    provider of the same key, which is where the requirer would bind."""
    admitted = _ir(ADMITTED_ROUTE)
    candidate = _ir("", with_shared=True)
    refused = _refused(admitted, candidate)
    assert REFUSAL in refused.message, refused.message
    assert "`Router`" in refused.message, refused.message


def test_route_shrunk_to_one_realm_is_refused():
    """`realms("w1", "w2")` -> `realms("w1")` loses w2 while staying a route."""
    admitted = _ir(ADMITTED_ROUTE)
    candidate = _ir('isolate ops in realms("w1")')
    refused = _refused(admitted, candidate)
    assert REFUSAL in refused.message, refused.message
    assert 'realms("w1", "w2")' in refused.message, refused.message
    assert 'realms("w1")' in refused.message, refused.message
    assert refused.navigate["refused"]["realms"] == ["w2"], refused.navigate
    assert refused.code == "G9", refused.code


def test_route_replaced_by_a_single_realm_bind_is_refused():
    """The route becomes a single-realm placement: the requirer stays admitted
    but leaves w2, exactly the change a pinned `isolate` would be refused for."""
    admitted = _ir(ADMITTED_ROUTE)
    candidate = _ir('isolate ops in realm("w1")')
    refused = _refused(admitted, candidate)
    assert REFUSAL in refused.message, refused.message
    assert 'realm("w1")' in refused.message, refused.message
    assert refused.navigate["refused"]["realms"] == ["w2"], refused.navigate


def test_route_renamed_onto_a_shared_key_is_refused_by_the_realm_set():
    """The per-key walk cannot see a renamed key, so the component's realm set
    is what refuses: the admitted Router names w1 and w2 through its route."""
    admitted = _ir(ADMITTED_ROUTE)
    candidate = _ir("", header="component Router requires ops2: Ops")
    refused = _refused(admitted, candidate)
    assert SHRINK in refused.message, refused.message
    assert "`Router`" in refused.message, refused.message
    assert 'realm("w1")' in refused.message, refused.message
    assert 'realm("w2")' in refused.message, refused.message
    assert refused.code == "G9", refused.code


def test_dropped_degenerate_route_keeps_its_route_spelling():
    """An admitted `realms("w1")` is a route, not a pin: the refusal says the
    candidate removed the clause the source actually wrote."""
    refused = _refused(_ir('isolate ops in realms("w1")'), _ir(""))
    assert 'isolate ops in realms("w1")' in refused.message, refused.message
    assert 'isolate ops in realm("w1")' not in refused.message, refused.message


def test_one_route_shrinks_while_another_keeps_the_realm_set():
    """The component still names both realms through the other route, so the
    realm-set walk sees no shrink and only the per-key walk can refuse the
    `ops` route losing w2."""
    admitted = {"components": [
        _comp("W1", isolate={"ops": "w1", "kv": "w1"},
              provides={"ops": "Ops", "kv": "Kv"}),
        _comp("W2", isolate={"ops": "w2", "kv": "w2"},
              provides={"ops": "Ops", "kv": "Kv"}),
        _comp("Router", requires={"ops": "Ops", "kv": "Kv"}, routes={
            "ops": {"realms": ["w1", "w2"], "strategy": None},
            "kv": {"realms": ["w1", "w2"], "strategy": None}}),
    ]}
    candidate = {"components": [
        _comp("W1", isolate={"ops": "w1", "kv": "w1"},
              provides={"ops": "Ops", "kv": "Kv"}),
        _comp("W2", isolate={"ops": "w2", "kv": "w2"},
              provides={"ops": "Ops", "kv": "Kv"}),
        _comp("Router", requires={"ops": "Ops", "kv": "Kv"}, routes={
            "ops": {"realms": ["w1"], "strategy": None},
            "kv": {"realms": ["w1", "w2"], "strategy": None}}),
    ]}
    refused = _refused(admitted, candidate)
    assert REFUSAL in refused.message, refused.message
    assert "`ops` requirement" in refused.message, refused.message
    assert SHRINK not in refused.message, refused.message
    assert refused.navigate["refused"]["realms"] == ["w2"], refused.navigate


def test_mixed_provision_and_route_loss_names_every_realm():
    """A withdrawn provider and a dropped route in one edit: the navigable
    refusal names every realm the edit left, not only the provision's."""
    admitted = {"components": [
        _comp("W1", isolate={"ops": "w1"}, provides={"ops": "Ops"}),
        _comp("W2", isolate={"ops": "w2"}, provides={"ops": "Ops"}),
        _comp("W3", isolate={"ops": "w3"}, provides={"ops": "Ops"}),
        _comp("Router", requires={"ops": "Ops"},
              routes={"ops": {"realms": ["w2", "w3"], "strategy": None}}),
    ]}
    candidate = {"components": [
        _comp("W2", isolate={"ops": "w2"}, provides={"ops": "Ops"}),
        _comp("W3", isolate={"ops": "w3"}, provides={"ops": "Ops"}),
        _comp("SharedOps", provides={"ops": "Ops"}),
        _comp("Router", requires={"ops": "Ops"}),
    ]}
    refused = _refused(admitted, candidate)
    assert REFUSAL in refused.message, refused.message
    assert set(refused.navigate["refused"]["realms"]) == {"w1", "w2", "w3"}


# ------------------------------------------------------------------ controls


def test_identical_route_is_admitted():
    check_no_realm_removal(_ir(ADMITTED_ROUTE), _ir(ADMITTED_ROUTE), _untrusted())


def test_widened_route_is_admitted():
    """A route that keeps every admitted realm and adds one is not a loss."""
    check_no_realm_removal(
        _ir('isolate ops in realms("w1")'),
        _ir('isolate ops in realms("w1", "w2")'),
        _untrusted(),
    )


def test_route_to_a_single_realm_is_admitted_when_it_keeps_the_admitted_set():
    """A degenerate one-realm route is the same placement as that realm's pin:
    `realms("w1")` against an admitted `realm("w1")` loses nothing."""
    check_no_realm_removal(
        _ir('isolate ops in realm("w1")'),
        _ir('isolate ops in realms("w1")'),
        _untrusted(),
    )


def test_withdrawn_requirer_is_exempt():
    admitted = _ir(ADMITTED_ROUTE)
    candidate = compile_source(PROVIDERS, "router.rvl")
    check_no_realm_removal(admitted, candidate, _untrusted())


def test_trusted_author_may_drop_the_route():
    check_no_realm_removal(_ir(ADMITTED_ROUTE), _ir(""), None)


# ------------------------------------------------------- the policy selection


def test_component_realms_counts_route_realms():
    """The policy selects a component by every realm it is placed in; a routed
    requirer reaches the realms it routes across."""
    ir = _ir(ADMITTED_ROUTE)
    assert component_realms(ir["manifest"], "Router") == frozenset({"w1", "w2"})


def test_component_realms_keeps_the_isolate_map_for_pinned_components():
    ir = _ir(ADMITTED_ROUTE)
    assert component_realms(ir["manifest"], "W1") == frozenset({"w1"})


def test_component_realms_is_empty_for_an_unplaced_component():
    ir = _ir("", with_shared=True)
    assert component_realms(ir["manifest"], "Router") == frozenset()
