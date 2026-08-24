"""Runtime driver for the wasm tier (docs/v2.0-roadmap.md §2, "Toward early
production").

`revl run <manifest> --backend wasm` wired behind the *same driver contract*
the py, rust and java tiers use: compile -> emit the cordis-wasm modules ->
boot them -> tear down LIFO -> prove no residue -> exit. The wasm tier is the
substrate tier: components compile to WAT modules (backends/wasm/emit.py) that
run on the cordis-wasm runtime, where the paradigm is enforced by the wasmtime
sandbox (the coeffect specification *is* the emitted import section; a provision
*is* an export). That runtime lives in its own repo with its own wasmtime-bearing
interpreter, so — exactly as the rust tier boots a separate cordis-rs process
over the bridge seam (:mod:`revl.run_rust`) — this driver boots a separate
process under the cordis-wasm interpreter: the once-mode harness
(``backends/wasm/run_harness.py``). The driver does the revl-side work here
(compile + emit the WAT); the harness needs only wasmtime + the runtime.

What is wired and runs live (wherever the cordis-wasm runtime is present):

* **--once** — the boot -> LIFO teardown -> no-residue-proof -> exit round-trip.
  The harness plugs every module in load order on one cordis-wasm ``Runtime``
  (a consumer stays inactive until its coeffect is satisfied, so provider-first
  order brings the mesh to ``ACTIVE``), reports ``UP``, unplugs LIFO (the
  compiled ``deactivate`` state machine replays each component's inverses), and
  asserts the live runtime holds nothing afterwards (``rt.fibers`` empty and the
  coeffect table ``rt.table`` empty — the substrate mirror of the py driver's
  ``registry.size``/``reflect.store`` check). ``NO-RESIDUE`` is printed only when
  that holds.

What is NOT yet wired (honestly fenced, not faked):

* the **interactive REPL** over provided wasm services, for the same reason it
  is unwired on the rust/java tiers (see :mod:`revl.run_rust`). Without ``--once``
  the driver notes the gap and completes the same once round-trip.

The wasm tier is also the strictest emitter: ``config`` blocks, host builtins,
method-time effects, non-Int component services and a few other constructs are
hard ``EmitError`` (docs live in backends/wasm/README.md). A composition that
uses one is reported as an emit failure (exit 1) — the compile is fine, the
substrate simply does not carry it.

Runtime availability is a gate, not a lie: with no cordis-wasm runtime (no
wasmtime, or the harness cannot import it) the driver *skips with a reason* and
exits nonzero, exactly as the py tier does for a missing cordis-py.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ._paths import backends_root

_BACKENDS_DIR = backends_root()
_WASM_DIR = _BACKENDS_DIR / "wasm"
_HARNESS = _WASM_DIR / "run_harness.py"


def _cordis_wasm_dir() -> Path:
    return Path(os.environ.get("CORDIS_WASM") or (Path.home() / "Projects" / "cordis-wasm"))


def _cordis_wasm_python() -> str | None:
    """The interpreter that runs the harness — the cordis-wasm venv (it carries
    wasmtime), overridable for CI. ``None`` when none is configured/present."""
    override = os.environ.get("REVL_CORDIS_WASM_PYTHON")
    if override and Path(override).exists():
        return override
    venv = _cordis_wasm_dir() / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else None


def wasm_runtime_reason() -> str | None:
    """``None`` when the wasm tier can actually run here, else why it cannot.

    The harness boots the emitted WAT on the cordis-wasm ``Runtime``, which is
    backed by the ``wasmtime`` Python bindings; the gate is that some interpreter
    can import both ``wasmtime`` and the cordis-wasm ``runtime`` module. A quick
    ``-c`` probe (imports only, boots nothing) is the fast, honest check — the
    wasm sibling of the rust tier's ``cargo generate-lockfile`` resolve.
    """
    cordis_wasm = _cordis_wasm_dir()
    if not cordis_wasm.is_dir():
        return (f"the cordis-wasm runtime was not found at {cordis_wasm} — "
                "clone it (https://github.com/inso1337/cordis-wasm) or set "
                "CORDIS_WASM to its checkout, then re-run")
    python = _cordis_wasm_python()
    if python is None:
        return (f"no cordis-wasm interpreter found (looked for {cordis_wasm}/.venv, "
                "or set REVL_CORDIS_WASM_PYTHON) — a wasmtime-bearing venv is "
                "needed to boot the WAT modules")
    probe = subprocess.run(
        [python, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import wasmtime; "
         "from runtime import Runtime", str(cordis_wasm)],
        capture_output=True, text=True,
    )
    if probe.returncode == 0:
        return None
    detail = (probe.stderr or "").strip().splitlines()
    return (f"the cordis-wasm runtime could not be imported by {python}: "
            f"{detail[-1] if detail else '?'}")


def _load_order(ir: dict) -> list[str]:
    manifest = ir.get("manifest") or {}
    return manifest.get("loadOrder") or [c["name"] for c in ir.get("components") or []]


def _emit_modules(ir: dict) -> dict[str, str]:
    spec = importlib.util.spec_from_file_location("revl_wasm_emit", _WASM_DIR / "emit.py")
    emit_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emit_module)
    return emit_module.emit(ir)


def run_wasm(ir: dict, config: dict, files, once: bool = False,
             interactive: bool = False) -> int:
    """Emit -> boot the composition on the cordis-wasm runtime as a process,
    then run the once round-trip (LIFO teardown + no-residue proof) and exit.
    Returns 0 on a clean ``UP`` -> ``NO-RESIDUE`` -> ``DOWN``; nonzero otherwise.
    A missing cordis-wasm runtime is a skip-with-reason and exit 3, mirroring the
    py/rust/java tiers (never a feint at passing)."""
    reason = wasm_runtime_reason()
    if reason is not None:
        print(f"error: the cordis-wasm runtime is not available.\n"
              f"       {reason}", file=sys.stderr)
        return 3

    if once is False and interactive:
        print("note: the interactive REPL is wired for the py tier only; the "
              "wasm tier runs the\n      boot -> teardown -> no-residue "
              "round-trip (as with --once) and exits.", flush=True)

    order = _load_order(ir)
    try:
        modules = _emit_modules(ir)
    except Exception as exc:  # noqa: BLE001 — surface any EmitError as one diagnostic
        print(f"error: could not emit the wasm composition (the substrate tier "
              f"is the strictest emitter; see backends/wasm/README.md):\n{exc}",
              file=sys.stderr)
        return 1
    missing = [name for name in order if name not in modules]
    if missing:
        print(f"error: the wasm emitter produced no module for: {', '.join(missing)}",
              file=sys.stderr)
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="revl_run_wasm_"))
    try:
        spec_file = tmp / "run.spec.json"
        spec_file.write_text(json.dumps({
            "name": "run",
            "once": True,
            "order": order,
            "modules": {name: modules[name] for name in order},
        }), encoding="utf-8")

        env = dict(os.environ)
        env["CORDIS_WASM"] = str(_cordis_wasm_dir())
        print("== load composition (wasm tier) ==", flush=True)
        proc = subprocess.Popen(
            [_cordis_wasm_python(), str(_HARNESS), str(spec_file)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, env=env,
        )
        if proc.stdin is not None:
            proc.stdin.close()

        saw_up = saw_down = saw_no_residue = saw_residue_left = False
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            text = line.strip()
            if text == "[run] UP":
                saw_up = True
            elif text.startswith("[run] NO-RESIDUE"):
                saw_no_residue = True
            elif text.startswith("[run] RESIDUE-LEFT"):
                saw_residue_left = True
            elif text == "[run] DOWN":
                saw_down = True
        rc = proc.wait()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if rc != 0:
        print(f"error: the wasm composition process exited {rc}", file=sys.stderr)
        return 1
    if not (saw_up and saw_down):
        print("error: the wasm composition did not complete the boot/teardown "
              "round-trip (no UP/DOWN)", file=sys.stderr)
        return 1
    if saw_residue_left or not saw_no_residue:
        print("error: the wasm composition left residue after teardown",
              file=sys.stderr)
        return 1
    return 0
