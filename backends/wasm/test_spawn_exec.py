"""Instance-parametric `spawn`, EXECUTED on the cordis-wasm substrate.

The proof of this feature is execution (docs/design-v2-instances.md, phase 2.5).
A `spawn` is an acquisition whose inverse is the instance's own teardown; on
this tier the target *template* is registered with the runtime and instantiated
on demand into a FRESH LOCAL realm (B3), as its own nested teardown scope. This
compiles a revl composition, emits WAT, and drives it on the real cordis-wasm
runtime (Python host + wasmtime), asserting BY RUNNING the four properties the
feature exists to provide:

  1. two live instances of one component coexist in distinct local realms
     (both provide the same key, non-colliding);
  2. disposing one runs ITS LIFO teardown and leaves the others live;
  3. a request-scoped reclaim happens AT `dispose()` (inside a provide-method
     call), not deferred to the parent component's teardown;
  4. supervision-tree addressing — the spawner reaches its own instance through
     the handle; a sibling / the root, holding no handle and no realm, cannot.

The runtime is the first-party cordis-wasm prototype. Point CORDIS_WASM at a
checkout (default: ~/Projects/cordis-wasm); without it (or without the wasmtime
Python package) these skip with a reason — never reported as passing.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402


def _emitter():
    spec = importlib.util.spec_from_file_location("revl_wasm_emit", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cordis_runtime():
    """Load the cordis-wasm runtime by explicit path (avoids colliding with the
    backends/python `runtime` module) or skip with a reason."""
    pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")
    root = os.environ.get("CORDIS_WASM") or str(Path.home() / "Projects" / "cordis-wasm")
    path = Path(root) / "runtime.py"
    if not path.exists():
        pytest.skip(f"cordis-wasm runtime not found at {path} (set CORDIS_WASM)")
    spec = importlib.util.spec_from_file_location("cordis_wasm_runtime", path)
    module = importlib.util.module_from_spec(spec)
    # register before exec: the runtime's @dataclass fields resolve their types
    # through sys.modules[cls.__module__], which is None for an unregistered
    # synthetic module (a CPython importlib/dataclasses gotcha).
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"cordis-wasm runtime failed to import: {exc}")
    if not hasattr(module, "Runtime") or not hasattr(Runtime := module.Runtime, "register_template"):
        pytest.skip("cordis-wasm runtime predates instance-parametric spawn (no register_template)")
    return module


# A worker holds its own realm-scoped provision (`counter`) and traces two
# ordered effects through a Probe coeffect so teardown order is observable; its
# `config.id` distinguishes the two instances. The supervisor spawns two in its
# activation body and exposes one request-scoped op, `retire_a`, that disposes
# worker A through the handle it alone holds.
SOURCE = """
service Probe { fn mark(m: Int) -> Int }
service Counter { fn value() -> Int }
service Ctl { fn retire_a() -> Int }

component Worker requires probe: Probe provides counter: Counter {
  config { id: Int }
  effect probe.mark(config.id * 10 + 1) undo probe.mark(config.id * 10 + 4)
  effect probe.mark(config.id * 10 + 2) undo probe.mark(config.id * 10 + 3)
  provide counter { fn value() = config.id }
}

component Supervisor provides ctl: Ctl {
  let w1 = effect spawn Worker with { id: 1 } undo w1.dispose()
  let w2 = effect spawn Worker with { id: 2 } undo w2.dispose()
  provide ctl {
    fn retire_a() { let r = w1.dispose() return 1 }
  }
}
"""


def _template_config_order(ir: dict, name: str) -> list[str]:
    """A template's declared config field order — the positional order a spawn
    passes them and `register_template` records them in."""
    comp = next(c for c in ir["components"] if c["name"] == name)
    return [f["name"] for f in comp.get("config") or []]


def _driver():
    """Compile + emit + wire the scenario onto a live cordis-wasm runtime.

    Returns (rt, supervisor_fiber, marks, handles) with both workers already
    spawned and ACTIVE (the supervisor's activation ran to completion)."""
    mod = _cordis_runtime()
    ir = compile_source(SOURCE)
    modules = _emitter().emit(ir)

    marks: list[int] = []

    def mark(_fiber, value):
        marks.append(int(value))
        return value

    rt = mod.Runtime()
    rt.host_provide("probe", {"mark": mark})
    # A spawn target is a template: registered, never statically composed.
    for tmpl in ir["manifest"].get("templates") or []:
        rt.register_template(tmpl, modules[tmpl], _template_config_order(ir, tmpl))
    # The static composition members are plugged; here that is the Supervisor,
    # whose activation spawns the two workers.
    supervisor = None
    for entry in ir["manifest"]["loadOrder"]:
        supervisor = rt.plug(entry, modules[entry])
    return mod, rt, supervisor, marks


def test_two_instances_coexist_in_distinct_local_realms():
    """Property 1: two live instances of one template, both providing `counter`,
    coexist without a provision collision — each in its own local realm."""
    mod, rt, _sup, marks = _driver()
    inst = rt.instance_states()
    assert sorted(inst.values()) == ["active", "active"], inst
    # both published `counter`, but into DISTINCT per-instance realms: the flat
    # table carries `#<n>/counter` twice, and no un-prefixed `counter` at all.
    counter_keys = sorted(k for k in rt.table if k.endswith("counter"))
    assert len(counter_keys) == 2 and all(k != "counter" for k in counter_keys), counter_keys
    assert "counter" not in rt.table
    # both activated with their own config: 10*id + step for id in {1, 2}.
    assert sorted(marks) == [11, 12, 21, 22], marks


def test_dispose_one_runs_its_lifo_teardown_others_live():
    """Property 2: disposing one instance runs ITS OWN inverses in LIFO order
    and leaves the sibling untouched."""
    mod, rt, sup, marks = _driver()
    h1, h2 = sorted(rt.instance_states())
    marks.clear()
    rt.call(sup, "provide:ctl.retire_a")   # disposes worker A (id 1)
    # LIFO: the later step's inverse (mark 13) runs before the earlier's (14).
    assert marks == [13, 14], marks
    inst = rt.instance_states()
    assert h1 not in inst and inst.get(h2) == "active", inst
    # idempotent: the supervisor's own deactivate safety net will dispose h1
    # again at teardown; a second dispose must be a no-op.
    marks.clear()
    rt._dispose(h1)
    assert marks == [], marks


def test_request_scoped_reclaim_at_dispose_not_deferred():
    """Property 3: the instance is reclaimed the moment `dispose()` runs inside
    a request (retire_a), NOT deferred to the supervisor's own teardown — the
    anti-leak guarantee. The supervisor stays ACTIVE throughout."""
    mod, rt, sup, marks = _driver()
    State = mod.State
    h1, h2 = sorted(rt.instance_states())
    assert sup.state is State.ACTIVE
    rt.call(sup, "provide:ctl.retire_a")
    # reclaimed NOW: handle gone, its realm-scoped provision withdrawn, while
    # the supervisor and the sibling instance are still live.
    assert h1 not in rt.instance_states()
    assert not any(k.startswith(f"#") and k.endswith("counter") and rt.table[k].uid == h1
                   for k in rt.table)
    assert sup.state is State.ACTIVE
    assert rt.instance_states().get(h2) == "active"
    # converse anti-leak: an UN-disposed instance (h2) does not outlive its
    # spawner — the supervisor's deactivate disposes it (LIFO 23 before 24).
    marks.clear()
    rt.unplug(sup)
    assert marks == [23, 24], marks
    assert rt.instance_states() == {}


def test_supervision_tree_addressing():
    """Property 4: the spawner reaches its own instance through the handle; a
    sibling / the root, holding no handle and sharing no realm, cannot."""
    mod, rt, _sup, _marks = _driver()
    h1, h2 = sorted(rt.instance_states())
    # the spawner (holding the handles) reaches each instance's realm-scoped op.
    assert rt.call_instance(h1, "provide:counter.value") == 1
    assert rt.call_instance(h2, "provide:counter.value") == 2
    # a root/sibling resolves the bare key `counter` — which no instance
    # published (each provides into its own local realm), so it is unreachable.
    assert "counter" not in rt.table
    # the two instances' realms are distinct, so neither reaches the other's.
    realms = {rt.instances[h].realm for h in (h1, h2)}
    assert len(realms) == 2, realms
