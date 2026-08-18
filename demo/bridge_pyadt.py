#!/usr/bin/env python
"""py<->py ADT/Result crossing (docs/interop-bridge.md "Canonical value
encoding"): a provider returns a user ADT (`Found`) and a `Result` across the
seam, and the consumer rebuilds them as native case instances.

  provider (Python, separate process): DirSvc,  serves `dir`
  consumer (Python, this process):     DirUser, requires `dir` via a proxy

Both sides compile the *same* examples/outcome.rvl. The provider runs under
`python -m revl._process_runner` (the shipped placement path); the consumer is
driven here, in-process, so the checks can look at the value itself rather than
at a `repr` — that is what makes "the payload survived" a real assertion and
not a string match on a log line.

The three checks are the three claims of the canonical encoding:
  adt-tag      a *user* ADT case (`Found.Hit`) arrives as a native `Hit`
  adt-payload  its single argument (a `Row` record) is intact
  result-tag   the built-in `Result` gets the same treatment (`Ok`)

Run under the backend venv (needs cordis-py):
  backends/python/.venv/bin/python demo/bridge_pyadt.py
Exits nonzero on any failed check.

Note on components: outcome.rvl ships `DirSvc` (provider) and `DirUser`
(consumer), and nothing else — the same two every
examples/placement/outcome_*.toml pairs across languages. An earlier version of
this demo asked the runner for a component named `AskClient` that the fixture
has never defined, so it died at `module 'revl_proc_mod' has no attribute
'AskClient'` before reaching a single probe.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import subprocess
import sys
import types

DEMO = pathlib.Path(__file__).resolve().parent
ROOT = DEMO.parent
BACKEND = ROOT / "backends" / "python"
for _path in (str(ROOT / "src"), str(BACKEND)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import bridge  # noqa: E402  (backends/python/bridge.py)
import emit  # noqa: E402

from revl import compile_files  # noqa: E402

try:
    from cordis import Context  # noqa: E402
    from cordis.fiber import FiberState  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit(
        f"{exc.name!r} missing. run under the backend venv: "
        f"{BACKEND / '.venv/bin/python'} {pathlib.Path(__file__).name}"
    ) from exc

RVL = ROOT / "examples" / "outcome.rvl"
ENV = {**os.environ, "PYTHONPATH": os.pathsep.join(
    [str(ROOT / "src"), os.environ.get("PYTHONPATH", "")])}


def _load_module() -> types.ModuleType:
    source = emit.emit(compile_files([str(RVL)]))
    module = types.ModuleType("revl_adt_mod")
    sys.modules[module.__name__] = module
    exec(compile(source, "<revl-adt>", "exec"), module.__dict__)
    return module


async def _flush() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


def _start_provider(sock: str, spec_path: pathlib.Path) -> subprocess.Popen:
    """DirSvc in its own process, serving `dir` over `sock`."""
    spec_path.write_text(json.dumps(
        {"name": "prov", "files": [str(RVL)], "components": ["DirSvc"],
         "serve": {"socket": sock, "keys": ["dir"],
                   "methods": {"dir": ["lookup", "check"]}}},
    ), encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "revl._process_runner", str(spec_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=ENV,
    )


async def main() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<12} {detail}", flush=True)
        if not ok:
            failures.append(name)

    tmp = pathlib.Path(f"/tmp/revl_pyadt_{os.getpid()}")
    tmp.mkdir(exist_ok=True)
    sock = str(tmp / "dir.sock")
    provider = _start_provider(sock, tmp / "prov.json")

    root = Context()
    proxy_fiber = None
    try:
        for line in provider.stdout:  # blocks until the provider is serving
            sys.stdout.write(line)
            if line.strip() == "[prov] UP":
                break
        else:
            print("provider failed to start", file=sys.stderr)
            return 1
        print("== provider up (DirSvc serving `dir` in a separate process) ==",
              flush=True)

        module = _load_module()
        # `module` is what lets the proxy rebuild `{"$kind","$value"}` into the
        # consumer's own case classes; without it the values stay raw JSON.
        proxy_fiber = root.plugin(
            bridge.proxy_component("dir", ["lookup", "check"], sock, module))
        await proxy_fiber
        await _flush()
        user_fiber = root.plugin(getattr(module, "DirUser"))
        await user_fiber
        await _flush()
        check("consumer-up", user_fiber.state == FiberState.ACTIVE,
              f"DirUser ACTIVE against a cross-process `dir` "
              f"(state={FiberState(user_fiber.state).name})")

        found = root.get("dir").lookup("ada")
        if hasattr(found, "__await__"):
            found = await found
        check("adt-tag", type(found) is getattr(module, "Hit"),
              f"user ADT `Found` rebuilt as native {type(found).__name__} "
              f"(module class, not a dict)")
        check("adt-payload", getattr(found, "value", None) == {"id": 1, "name": "ada"},
              f"Hit payload (Row) survived the crossing: {getattr(found, 'value', None)!r}")

        outcome = root.get("dir").check("ada")
        if hasattr(outcome, "__await__"):
            outcome = await outcome
        check("result-tag",
              type(outcome).__name__ == "Ok"
              and getattr(outcome, "value", None) == {"id": 1, "name": "ada"},
              f"built-in Result rebuilt as native {type(outcome).__name__}"
              f"({getattr(outcome, 'value', None)!r})")
    finally:
        if proxy_fiber is not None:
            try:
                await proxy_fiber.dispose()
            except Exception:  # noqa: BLE001 — teardown is best-effort
                pass
        if provider.poll() is None:
            provider.terminate()
            try:
                provider.wait(timeout=5)
            except subprocess.TimeoutExpired:
                provider.kill()
        for stale in tmp.glob("*"):
            stale.unlink()
        tmp.rmdir()

    print("\n" + ("all checks passed" if not failures
                  else f"{len(failures)} check(s) FAILED"), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
