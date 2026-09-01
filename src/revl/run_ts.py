"""Runtime driver for the ts tier (docs/v2.0-roadmap.md §2, "Toward early
production"; FEATURE-REQUESTS FR-2).

`revl run <manifest> --backend ts` wired behind the *same driver contract* the
py/rust/java/wasm tiers use (the ``_Driver`` in :mod:`revl.run`): compile →
emit the cordis-ts module → boot it on node → tear down LIFO → prove no
residue → exit. The ts tier boots the composition as a *separate node process*
over the same ``backends/typescript/placement_runner.ts`` the cross-tier bridge
(roadmap item 23) already drives — the seam is identical to the rust tier's
(:mod:`revl.run_rust`); only the child runtime differs (node on cordis v4).

What is wired and runs live (wherever node >= 23.6 with a resolvable cordis-ts
is present):

* **--once** — the boot → LIFO teardown → no-residue-proof → exit round-trip.
  The runner loads every component in load order on a real ``Context``,
  reports ``UP``, then disposes every fiber in reverse (consumers before
  providers), and — in once mode — asserts the live runtime matches its
  pre-load snapshot afterwards (``snapshotRuntime``/``assertNoResidue`` from
  ``backends/typescript/runtime.ts``: registry, reflect store, root effects,
  event hooks, and host resources — the cordis-ts mirror of the py driver's
  ``registry.size``/``reflect.store`` check and the rust runner's
  ``registry().len()``/``reflect().services().len()`` check). ``NO-RESIDUE`` is
  printed only when that holds.

What is NOT yet wired (honestly fenced, not faked):

* the **interactive REPL** over provided ts services, for the same reason it is
  unwired on the rust and java tiers (see :mod:`revl.run_rust`): it needs the
  driver to hold a persistent RPC client against a served stub. Without
  ``--once`` the ts driver notes the gap and completes the same once
  round-trip rather than pretending to hold a REPL.
* a **tsc build gate**: node executes the emitted module with native type
  stripping (the same path ``revl run --placement`` node processes take), so a
  run needs the module to *run*, not to satisfy ``tsc``. The emitter's
  ``revlSlice`` union typing gap (FR-7) is therefore a `revl test --backend ts`
  / typecheck concern, not a run blocker, and is deliberately left to its own
  agent.

Runtime availability is a gate, not a lie: with no node, or no resolvable
cordis-ts, the driver *skips with a reason* and exits nonzero, exactly as the
other tiers do for a missing runtime (never a green run that booted nothing).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# read-only reuse of the placement machinery (roadmap item 23): the same emit
# path the cross-tier bridge uses for node processes. We drive the resulting
# module through the same placement runner, in `once` mode.
from . import placement as _placement
from ._paths import stdlib_root
from .errors import RevlError

_TS_DIR = _placement._TS_DIR
_RUNNER = _TS_DIR / "placement_runner.ts"

# node >= 23.6 strips types natively (22.6-23.5 need --experimental-strip-types,
# which the runner is not invoked with — matching `revl run --placement`).
_MIN_NODE = (23, 6)


def _node_version() -> "tuple[int, int] | None":
    """(major, minor) of the node on PATH, or None when node is absent/broken."""
    try:
        probe = subprocess.run(["node", "--version"], capture_output=True,
                               text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.match(r"^v(\d+)\.(\d+)\.", probe.stdout.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def ts_runtime_reason() -> str | None:
    """``None`` when the ts tier can actually run here, else why it cannot.

    The gate mirrors the placement preflight's node check (src/revl/placement.py
    :func:`_preflight`): node must be on PATH and cordis-ts must be installed.
    On top of that it pins the node version that strips types natively — an old
    node would fail on the runner's first `.ts` import with a syntax error that
    says nothing about how to fix it.
    """
    if shutil.which("node") is None:
        return ("node not on PATH — install Node >= 23.6 "
                "(native .ts type-stripping), then re-run")
    version = _node_version()
    if version is None:
        return "node on PATH but does not answer `node --version`"
    if version < _MIN_NODE:
        return (f"node {version[0]}.{version[1]} is too old to run .ts modules "
                f"natively — install Node >= {_MIN_NODE[0]}.{_MIN_NODE[1]}, "
                "then re-run")
    if not (_TS_DIR / "node_modules" / "cordis").is_dir():
        return ("the cordis-ts runtime is not installed "
                f"(backends/typescript/node_modules/cordis missing).\n"
                f"       set it up:  (cd {_TS_DIR} && npm install)")
    return None


def _load_order(ir: dict) -> list[str]:
    manifest = ir.get("manifest") or {}
    return manifest.get("loadOrder") or [c["name"] for c in ir.get("components") or []]


def _spec(ir: dict, config: dict, files, module: str) -> dict:
    """The single-process, once-mode runner spec: every component placed
    locally, in load order, tear down and prove no residue on boot. Shape-
    compatible with the placement spec the runner already reads — this is just
    the degenerate one-process placement (no proxies, no serve), with ``once``
    set. Node keeps PascalCase component names (``mod[cname]``), unlike rust's
    snake_case remap."""
    return {
        "name": "run",
        "backend": "node",
        "files": [str(f) for f in files],
        "module": module,
        "components": _load_order(ir),
        "config": config,
        "provides": list({k for c in ir.get("components") or []
                          for k in (c.get("provides") or {})}),
        "proxies": {},
        "probe": [],
        "once": True,
        # item 396 option B: the ts thunk resolves a `@ts ref` file at call time
        # through a root the RUNNER provides (so the artifact text is
        # machine-independent). The root is the root compile file's directory,
        # exactly as the py driver's appended sys.path entry; the runner joins it
        # with the recorded relative path and hash-checks the file before any
        # host code runs. Present always (harmless when no ref); `refs` is the
        # list the runner hash-checks.
        "refRoot": (os.path.dirname(os.path.abspath(str(files[0])))
                    if files else ""),
        # item 410: the SECOND root a stdlib-origin `@ts ref` resolves against —
        # the install tree, layout-uniform. The runner also self-derives this
        # from `import.meta.url` when the key is absent, but setting it here keeps
        # the single-process runner explicit. Each ref carries its `root` kind so
        # the runner joins and hash-checks against the right root, never the wrong
        # trust domain.
        "stdlibRefRoot": str(stdlib_root().parent),
        "refs": [
            {"extern": e.get("name"), "path": r["path"], "sha256": r["sha256"],
             **({"root": r["root"]} if r.get("root") else {})}
            for e in ir.get("externs") or []
            for r in [(e.get("refs") or {}).get("ts")] if r is not None
        ],
    }


def run_ts(ir: dict, config: dict, files, once: bool = False,
           interactive: bool = False) -> int:
    """Emit → boot the composition on cordis-ts as a node process, then run the
    once round-trip (LIFO teardown + no-residue proof) and exit.

    Returns 0 when the composition booted (``UP``), tore down, proved no residue
    (``NO-RESIDUE``), and the process exited cleanly; nonzero otherwise. A
    missing/unrunnable node or cordis-ts is a skip-with-reason and exit 3,
    mirroring the other tiers' missing-runtime path (never a feint at passing).
    """
    reason = ts_runtime_reason()
    if reason is not None:
        print(f"error: the cordis-ts runtime is not available.\n"
              f"       {reason}", file=sys.stderr)
        return 3

    if once is False and interactive:
        # the interactive REPL over the ts tier is not wired (see the module
        # docstring): hold-and-REPL needs a persistent RPC client against the
        # runner's stub. Be explicit and complete the boot/teardown round-trip.
        print("note: the interactive REPL is wired for the py tier only; the "
              "ts tier runs the\n      boot -> teardown -> no-residue "
              "round-trip (as with --once) and exits.", flush=True)

    tmp = Path(tempfile.mkdtemp(prefix="revl_run_ts_"))
    module_path: str | None = None
    try:
        try:
            # emits into backends/typescript/_gen/mod_<tmp>.ts so its
            # `../runtime.ts` / `cordis` imports resolve; removed in `finally`
            module_path = _placement._emit_ts_module(ir, tmp)
        except (RevlError, RuntimeError, OSError) as exc:
            print(f"error: could not emit the ts composition:\n{exc}",
                  file=sys.stderr)
            return 1

        spec_file = tmp / "run.spec.json"
        spec_file.write_text(json.dumps(_spec(ir, config, files, module_path)),
                             encoding="utf-8")

        print("== load composition (ts tier) ==", flush=True)
        proc = subprocess.Popen(
            ["node", str(_RUNNER), str(spec_file)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
        )
        # once mode ignores stdin; close it so the runner never blocks on it
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
        if module_path is not None:
            try:
                os.unlink(module_path)
            except OSError:
                pass
        shutil.rmtree(tmp, ignore_errors=True)

    if rc != 0:
        print(f"error: the ts composition process exited {rc}", file=sys.stderr)
        return 1
    if not (saw_up and saw_down):
        print("error: the ts composition did not complete the boot/teardown "
              "round-trip (no UP/DOWN)", file=sys.stderr)
        return 1
    if saw_residue_left or not saw_no_residue:
        print("error: the ts composition left residue after teardown",
              file=sys.stderr)
        return 1
    return 0
