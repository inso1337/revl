"""Instance **accessor** (`s.<key>.method(..)`), EXECUTED on cordis-wasm.

The proof of this feature is execution (docs/design-v2-instances.md,
"Instance accessor — frozen"). Phase-2.5 spawn already isolates each instance's
provision into its own B3 local realm; the accessor adds *reading a provision
back* through the spawn handle. `s.<key>` resolves the service the target
provides under `key` — through the handle's stored instance context, the
private local realm the matching `spawn` isolated the key into — so
`s.<key>.method(..)` yields THAT instance's provision and no other's.

This compiles a revl composition, emits WAT, and drives it on the real
cordis-wasm runtime (Python host + wasmtime), asserting BY RUNNING the three
properties the accessor exists to provide:

  1. positive supervision-tree direction — a spawner reading through its handle
     gets THAT spawned instance's provision (w1 -> 100, w2 -> 200), never the
     sibling's;
  2. negative — root / a sibling cannot resolve the instance's provision: the
     realm keeps it private (no bare `counter` in the shared table; the
     realm-qualified read needs the handle only the spawner holds);
  3. reading a key the target does not provide is a compile error
     (frontend-enforced).

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
    """Load the cordis-wasm runtime by explicit path or skip with a reason."""
    pytest.importorskip("wasmtime", reason="wasmtime Python package not installed")
    root = os.environ.get("CORDIS_WASM") or str(Path.home() / "Projects" / "cordis-wasm")
    path = Path(root) / "runtime.py"
    if not path.exists():
        pytest.skip(f"cordis-wasm runtime not found at {path} (set CORDIS_WASM)")
    spec = importlib.util.spec_from_file_location("cordis_wasm_runtime", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"cordis-wasm runtime failed to import: {exc}")
    if not hasattr(module, "Runtime") or not hasattr(Runtime := module.Runtime, "register_template"):
        pytest.skip("cordis-wasm runtime predates instance-parametric spawn (no register_template)")
    if not hasattr(Runtime, "_instance_get"):
        pytest.skip("cordis-wasm runtime predates the instance accessor (no _instance_get)")
    return module


# Two live Worker instances, each providing `counter` into its OWN local realm.
# `counter.value()` returns `config.id * 100`, so the two instances' provisions
# are distinguishable (100 vs 200). The Supervisor spawns both and exposes two
# request ops that read a provision BACK through the handle it alone holds:
# `read_a` through w1, `read_b` through w2 — the accessor under test.
SOURCE = """
service Counter { fn value() -> Int }
service Ctl { fn read_a() -> Int fn read_b() -> Int }

component Worker provides counter: Counter {
  config { id: Int }
  provide counter { fn value() = config.id * 100 }
}

component Supervisor provides ctl: Ctl {
  let w1 = effect spawn Worker with { id: 1 } undo w1.dispose()
  let w2 = effect spawn Worker with { id: 2 } undo w2.dispose()
  provide ctl {
    fn read_a() = w1.counter.value()
    fn read_b() = w2.counter.value()
  }
}
"""


def _template_config_order(ir: dict, name: str) -> list[str]:
    comp = next(c for c in ir["components"] if c["name"] == name)
    return [f["name"] for f in comp.get("config") or []]


def _driver():
    """Compile + emit + wire the scenario onto a live cordis-wasm runtime.

    Returns (mod, rt, supervisor) with both workers already spawned and ACTIVE
    (the supervisor's activation ran to completion)."""
    mod = _cordis_runtime()
    ir = compile_source(SOURCE)
    modules = _emitter().emit(ir)

    rt = mod.Runtime()
    for tmpl in ir["manifest"].get("templates") or []:
        rt.register_template(tmpl, modules[tmpl], _template_config_order(ir, tmpl))
    supervisor = None
    for entry in ir["manifest"]["loadOrder"]:
        supervisor = rt.plug(entry, modules[entry])
    return mod, rt, supervisor


def test_accessor_reads_the_instances_own_provision():
    """Property 1 (positive): reading `s.<key>.method(..)` through a spawn
    handle, in emitter-produced WAT run on wasmtime, returns THAT spawned
    instance's provision — w1 -> 100, w2 -> 200 — and never the sibling's."""
    mod, rt, sup = _driver()
    # BOTH reads are driven by the emitted `provide:ctl.*` WAT, which calls the
    # `$inst_Worker_counter_value` import with each handle. w1 (id 1) resolves in
    # w1's own realm; w2 (id 2) in w2's — no crossing.
    assert rt.call(sup, "provide:ctl.read_a") == 100
    assert rt.call(sup, "provide:ctl.read_b") == 200


def test_accessor_is_realm_private_root_and_sibling_cannot_resolve():
    """Property 2 (negative): the provision the accessor reads is private to the
    instance's local realm. A root/sibling holds no handle and shares no realm,
    so it has no path to it — the table carries only realm-prefixed `#<n>/counter`
    and NO bare `counter` a shared-realm resolve could reach."""
    mod, rt, sup = _driver()
    h1, h2 = sorted(rt.instance_states())

    # each instance published `counter` into its OWN realm — two prefixed keys,
    # never the bare key a root/sibling would resolve.
    counter_keys = sorted(k for k in rt.table if k.endswith("counter"))
    assert len(counter_keys) == 2 and all(k != "counter" for k in counter_keys), counter_keys
    assert "counter" not in rt.table          # nothing to resolve without a handle

    # the realm-qualified read (what the accessor lowers to) is keyed by the
    # handle's realm: h1 reaches only w1's provision, h2 only w2's.
    assert rt._instance_get(h1, "counter", "value", ()) == 100
    assert rt._instance_get(h2, "counter", "value", ()) == 200

    # the two instances live in DISTINCT realms, so neither handle reaches the
    # other's provision — the supervision tree keeps siblings apart.
    realms = {rt.instances[h].realm for h in (h1, h2)}
    assert len(realms) == 2, realms

    # a root/sibling resolving the bare key (no handle, shared realm "") finds
    # nothing — the KeyError is the "unreachable" the accessor's privacy rests on.
    with pytest.raises(KeyError):
        _ = rt.table["counter"]


def test_reading_a_non_provided_key_is_a_compile_error():
    """Property 3: `s.<key>` for a key the target does not provide never reaches
    a backend — it is refused at the frontend (docs/design-v2-instances.md)."""
    from revl import RevlError  # noqa: E402

    bad = SOURCE.replace("w1.counter.value()", "w1.ledger.value()")
    with pytest.raises(RevlError) as exc:
        compile_source(bad)
    assert "ledger" in str(exc.value) and "provision" in str(exc.value)
