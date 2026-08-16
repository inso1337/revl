"""v2 realms & interception on the real cordis-py runtime: two providers of
one key in two realms; each consumer observes ITS OWN provider; intercept
metadata is visible on the consumer's context chain."""

from __future__ import annotations

import pathlib
import sys

from cordis import Context
from cordis.fiber import FiberState

import emit
import runtime as runtime_mod
from conftest import flush, load_module

ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402


def _tenants_module():
    ir = compile_files([str(ROOT / "examples" / "tenants.rvl")])
    assert ir["ir_version"] == 2
    return load_module(emit.emit(ir), "v2_tenants")


async def test_realms_give_each_consumer_its_own_provider(trace):
    module = _tenants_module()
    root = Context()
    fibers = {
        name: runtime_mod.plug(root, getattr(module, name))
        for name in ("TenantAStore", "TenantBStore", "TenantAApp", "TenantBApp")
    }
    await flush()
    for name, fiber in fibers.items():
        assert fiber.state is FiberState.ACTIVE, f"{name} is {fiber.state}"

    # each app wrote through ITS realm's provider: the stores disagree
    a_kv = fibers["TenantAApp"].ctx.kv
    b_kv = fibers["TenantBApp"].ctx.kv
    assert a_kv.get("who") == "alice"
    assert b_kv.get("who") == "bob"
    assert a_kv is not b_kv, "realm separation must resolve to distinct providers"

    # withdrawal stays realm-local: unloading A's store deactivates A's app
    # only; tenant B is untouched (reactive resolution per realm)
    fibers["TenantAStore"].dispose()
    await flush()
    assert fibers["TenantAApp"].state is not FiberState.ACTIVE
    assert fibers["TenantBApp"].state is FiberState.ACTIVE
    assert b_kv.get("who") == "bob"


async def test_intercept_metadata_reaches_the_consumer_context(trace):
    module = _tenants_module()
    root = Context()
    runtime_mod.plug(root, module.TenantAStore)
    app = runtime_mod.plug(root, module.TenantAApp)
    await flush()
    assert app.state is FiberState.ACTIVE

    effective = app.ctx._effective_intercept()
    assert effective["kv"] == {"quota": 5, "tags": ["tenant_a"]}


def test_realm_labels_are_shared_by_value():
    assert runtime_mod.realm_label("t") is runtime_mod.realm_label("t")
    assert runtime_mod.realm_label("t") is not runtime_mod.realm_label("u")
