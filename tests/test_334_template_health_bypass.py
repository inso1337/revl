"""The item-334 health gate must not take the candidate's word for what it is
exempt from (EDGE 1, the template-exclusion bypass).

`_assert_successor_activated` excludes keys declared by TEMPLATE components from
its ROOT-resolution check, because a template's provisions come up per-instance
behind a spawn handle and never at ROOT (tests/test_instance_migration.py is
exactly that path). But `manifest.templates` is derived from the CANDIDATE's own
`spawn` targets, so the candidate chooses what the gate looks away from: name
your own provider in a `spawn`, and the key you declare is lifted out of the
static composition AND out of the gate. gen N — which was genuinely serving that
key — is then disposed with no revert path, and `Gate.propose` still hands the
loop `swapped=True` with `keys` naming a service no caller can reach.

The fix holds a successor to the keys its PREDECESSOR ACTUALLY SERVED that the
successor still DECLARES, whatever `manifest.templates` says, and reports `keys`
from the live `providedKeys` rather than re-deriving them from the IR. These
tests pin both halves, and the last two pin that the fix is not over-broad: a
real template composition still swaps, and a brand-new template-provided key is
still allowed to be absent from ROOT.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the health gate is a runtime property — needs cordis-py "
           "(`sh backends/python/setup.sh`, run under backends/python/.venv)",
)

from revl.compiler import compile_source  # noqa: E402
from revl.gate import Gate  # noqa: E402
from revl.mcp.session import Session, SessionError  # noqa: E402


# --------------------------------------------------------------------------- #
# gen N: an ordinary ROOT provider of `greeter`, callable.
# --------------------------------------------------------------------------- #
_BASE = (
    "service Greeter { fn describe() -> Str }\n"
    "component GreeterV1 provides greeter: Greeter {\n"
    '  provide greeter { fn describe() = "v1" }\n'
    "}\n"
)

# The bypass candidate. `GreeterV2` still DECLARES `provides greeter`, but it is
# a `spawn` target, so the compiler lists it in `manifest.templates` and lifts it
# out of the static composition: ROOT ends up providing nothing at all while the
# document keeps claiming `greeter`.
_BYPASS = (
    "service Greeter { fn describe() -> Str }\n"
    "component GreeterV2 provides greeter: Greeter {\n"
    "  config { tag: Str }\n"
    '  provide greeter { fn describe() = "v2" }\n'
    "}\n"
    "component Sup {\n"
    '  let w = effect spawn GreeterV2 with { tag: "x" } undo w.dispose()\n'
    "}\n"
)


@needs_cordis
def test_swap_rejects_a_successor_that_hides_its_declared_provider_in_a_template():
    """The `Session.swap` seam. The candidate's own `manifest.templates` names
    the provider of `greeter`, so the templates-only exclusion skipped it and the
    swap was ACCEPTED with gen N gone and `greeter` unreachable. The gate must
    reject it and roll back to gen N, still serving."""
    session = Session()
    session.load(compile_source(_BASE, "base.rvl"), record=True)
    try:
        assert session.state()["providedKeys"] == ["greeter"]
        assert session.call("greeter", "describe", [])["result"] == "v1"

        cand = compile_source(_BYPASS, "cand.rvl")
        # precondition: the candidate really does exercise the exclusion.
        assert "GreeterV2" in ((cand.get("manifest") or {}).get("templates") or [])
        assert any("greeter" in (c.get("provides") or {})
                   for c in cand["components"])

        with pytest.raises(SessionError, match="did not resolve to a live provider"):
            session.swap(cand)

        # gen N is intact and still answering — the revert guarantee held.
        assert session.state()["providedKeys"] == ["greeter"]
        assert session.call("greeter", "describe", [])["result"] == "v1"
    finally:
        session.unload()


@needs_cordis
def test_propose_reverts_the_template_bypass_and_never_reports_phantom_keys():
    """The `Gate.propose` seam — the attacker-facing verb. The candidate carries
    no extern and reaches nothing ungranted, so it ADMITS; the health gate must
    then revert the swap, and the result must be the refusal shape
    (`swapped=False`, `reverted=True`, `SWAP_REVERTED`) rather than an admission
    carrying a key the loop cannot call."""
    gate = Gate()
    try:
        gate.load(_BASE)
        assert gate.call("greeter", "describe", [])["result"] == "v1"

        result = gate.propose(_BYPASS, granted=[])
        assert result.admitted, result.message
        assert not result.swapped and result.reverted
        assert result.code == "SWAP_REVERTED"
        assert result.keys == ()

        # gen N kept serving through the whole transaction.
        assert gate._session.state()["providedKeys"] == ["greeter"]
        assert gate.call("greeter", "describe", [])["result"] == "v1"
    finally:
        gate.close()


@needs_cordis
def test_propose_keys_are_the_live_provided_keys_not_the_declarations():
    """The report and the runtime cannot disagree: on a swap that DOES take,
    `keys` is read off the same `providedKeys` a caller resolves against, so
    every reported key is callable."""
    gate = Gate()
    try:
        gate.load(_BASE)
        result = gate.propose(
            _BASE.replace('"v1"', '"v2"').replace("GreeterV1", "GreeterV3"),
            granted=[])
        assert result.admitted and result.swapped, result.message
        assert set(result.keys) == set(result.state["providedKeys"])
        for key in result.keys:
            gate.call(key, "describe", [])  # every reported key is reachable
    finally:
        gate.close()


# --------------------------------------------------------------------------- #
# Not over-broad: the legitimate instance path the exclusion exists for.
# `Worker` is a real template — its `store` lives behind the spawn handle and is
# never at ROOT, in gen N and gen N+1 alike — so the gate must let it through.
# --------------------------------------------------------------------------- #
def _instance_source(version: int) -> str:
    return f"""
service Store {{ fn version() -> Int }}
service Admin {{ fn ver() -> Int }}

component Worker provides store: Store {{
  provide store {{ fn version() = {version} }}
}}

component Supervisor provides admin: Admin {{
  let w = effect spawn Worker undo w.dispose()
  provide admin {{ fn ver() = w.store.version() }}
}}
"""


@needs_cordis
def test_a_legitimate_template_swap_still_succeeds():
    """T -> T' with a live instance: `store` is template-provided and absent from
    ROOT in BOTH generations, so it is not an inherited provision and the gate
    must not demand it. The swap takes and the successor's code is live."""
    session = Session()
    session.load(compile_source(_instance_source(1), "mig.rvl"))
    try:
        assert session.state()["providedKeys"] == ["admin"]
        assert session.call("admin", "ver", [])["result"] == 1

        state = session.swap(compile_source(_instance_source(2), "mig.rvl"))

        assert state["providedKeys"] == ["admin"]
        assert session.call("admin", "ver", [])["result"] == 2
    finally:
        session.unload()


@needs_cordis
def test_a_successor_may_introduce_a_brand_new_template_provided_key():
    """A key the predecessor never served may be declared on a template and be
    legitimately absent from ROOT — the gate judges inherited provisions, not
    every declaration a template makes."""
    session = Session()
    session.load(compile_source(
        "service Admin { fn ver() -> Int }\n"
        "component Supervisor provides admin: Admin {\n"
        "  provide admin { fn ver() = 0 }\n"
        "}\n", "plain.rvl"))
    try:
        assert session.state()["providedKeys"] == ["admin"]

        # gen N+1 adds a spawned Worker whose `store` is template-provided.
        state = session.swap(compile_source(_instance_source(1), "mig.rvl"))

        assert state["providedKeys"] == ["admin"]  # `store` is per-instance
        assert session.call("admin", "ver", [])["result"] == 1
    finally:
        session.unload()
