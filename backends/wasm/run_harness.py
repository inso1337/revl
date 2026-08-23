"""Single-process, once-mode runner for `revl run --backend wasm --once`
(docs/v2.0-roadmap.md §2, "Toward early production").

The wasm tier is the substrate tier: components are compiled to WAT modules
(backends/wasm/emit.py) and run on the cordis-wasm runtime, where the paradigm
is enforced by the wasmtime sandbox (the coeffect specification *is* the import
section; provisions *are* exports). That runtime lives in its own repo with its
own wasmtime-bearing venv, so — exactly like the rust tier boots a separate
cordis-rs process over the bridge seam — the wasm driver (src/revl/run_wasm.py)
boots this harness as a separate process under the cordis-wasm interpreter. The
driver does the revl-side work (compile + emit the WAT) and hands this harness a
spec of pre-emitted modules, so the harness needs only wasmtime + the runtime,
never the revl toolchain.

`--once` round-trip, mirroring the rust runner
(backends/rust/placement_runner/src/main.rs) and the py driver
(src/revl/run.py):

* plug every module in load order (providers first) on one cordis-wasm
  ``Runtime``; a consumer stays inactive until its coeffect is satisfied, so a
  provider-first order brings the whole mesh to ``ACTIVE``;
* print ``UP``;
* unplug LIFO (consumers before providers) — the compiled ``deactivate`` state
  machine replays each component's inverses;
* prove no residue: after teardown the live runtime must hold nothing — no
  fiber left in ``rt.fibers`` and no key left in ``rt.table`` (Σ). This is the
  substrate mirror of the py driver's ``registry.size==0`` / ``reflect.store=={}``
  check and the rust runner's ``registry().len()`` / ``reflect().services()``
  check: the tier proves the composition left nothing behind, not merely that
  unplug was called.

Usage (driven by run_wasm.py, not by hand):

    CORDIS_WASM=<dir> <cordis-wasm-venv-python> run_harness.py <spec.json>
"""

from __future__ import annotations

import json
import os
import pathlib
import sys


def _log(name: str, channel: str, subject: str, detail: str = "") -> None:
    print(f"[{name}] {channel:<6}| {subject:<16}| {detail}".rstrip(), flush=True)


def main() -> int:
    spec = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    name = spec.get("name", "run")
    once = bool(spec.get("once", False))
    order = spec.get("order") or list((spec.get("modules") or {}).keys())
    modules = spec.get("modules") or {}

    cordis_wasm = os.environ.get("CORDIS_WASM") or str(pathlib.Path.home() / "Projects" / "cordis-wasm")
    sys.path.insert(0, cordis_wasm)
    from runtime import Runtime, State  # noqa: PLC0415 — cordis-wasm, wasmtime-backed

    rt = Runtime()
    fibers = []
    for cname in order:
        fiber = rt.plug(cname, modules[cname])
        fibers.append((cname, fiber))
        _log(name, "load", cname, f"state={fiber.state.value}")

    # every fiber the composition placed must be ACTIVE for the mesh to be up
    # (a consumer left INACTIVE means its coeffect never resolved)
    all_active = all(f.state is State.ACTIVE for _, f in fibers)
    provided = sorted(rt.table)
    _log(name, "provide", "keys", ", ".join(provided) or "-")
    if not all_active:
        stuck = ", ".join(c for c, f in fibers if f.state is not State.ACTIVE)
        _log(name, "note", "inactive", stuck)
    print(f"[{name}] {'UP' if all_active else 'PARTIAL'}", flush=True)

    # teardown, consumers before providers (reverse load order)
    for cname, fiber in reversed(fibers):
        rt.unplug(fiber)
        _log(name, "swap", cname, "dispose -> compiled inverses replay (LIFO)")

    if once:
        live_fibers = len(rt.fibers)
        live_services = len(rt.table)
        _log(name, "residue", "registry", f"{live_fibers} live plugin(s)")
        _log(name, "residue", "provisions", f"{live_services} service(s) provided")
        if live_fibers == 0 and live_services == 0:
            print(f"[{name}] NO-RESIDUE — the composition left nothing behind", flush=True)
        else:
            print(f"[{name}] RESIDUE-LEFT — see the residue lines above", flush=True)

    print(f"[{name}] DOWN", flush=True)
    return 0 if all_active else 1


if __name__ == "__main__":
    sys.exit(main())
