"""Runtime driver for the go tier (docs/v2.0-roadmap.md §2, "Toward early
production" — roadmap item 77(e) / FR-8: `revl run --backend go`).

`revl run <manifest> --backend go` wired behind the *same driver contract* the
py, rust, java and wasm tiers use: compile -> emit the cordis-go module -> build
the composition -> boot it -> tear down LIFO -> prove no residue -> exit. Like
the rust/java/wasm tiers, go boots as a *separate process*: the placement
runner (``backends/go/placement_runner``) on the dependency-free stc-go runtime,
driven in its degenerate single-process once form. The seam is identical; only
the address space differs.

What is wired and runs live (wherever a go toolchain with the pinned stc-go is
present):

* **--once** — the boot -> LIFO teardown -> no-residue-proof -> exit round-trip.
  The runner loads every component in load order on a real stc-go ``Context``,
  reports ``UP``, then disposes every fiber in reverse (consumers before
  providers), awaits each out of the fiber registry, and asserts the live
  runtime holds nothing afterwards: ``len(root.Fibers()) == 0`` and no provided
  key still resolves (``RevlStillProvided`` — the generated per-key read that
  mirrors the py driver's ``registry.size``/``reflect.store`` check and the rust
  runner's ``registry().len()``/``reflect().services()`` check). ``NO-RESIDUE``
  is printed only when that holds.

What is NOT yet wired (honestly fenced, not faked):

* the **interactive REPL** over provided go services, for the same reason it is
  unwired on the rust/java/wasm tiers (see :mod:`revl.run_rust`). Without
  ``--once`` the driver notes the gap and completes the same once round-trip.
* **v3 typed-core documents** (top-level ``fn``/``type``/``extern``/``test``
  declarations) — the go placement bridge generates per-service proxies/stub
  dispatch, which needs v1/v2 live stc-go components; a v3 typed-core
  composition is emitted as ordinary Go with no runnable component and is
  refused at emit (the same boundary ``revl run --placement`` draws).

Runtime availability is a gate, not a lie: with no go toolchain or no
resolvable stc-go the driver *skips with a reason* and exits nonzero, exactly as
the py tier does for a missing cordis-py (never a green run that booted
nothing). The resolve probe is offline-first (a warm module cache answers in
milliseconds with no network), falling back to a networked resolve only when the
offline attempt failed for a resolution reason and proxy.golang.org is
reachable.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

# read-only reuse of the placement machinery (roadmap item 23): the same emit +
# `go build` path (backends/go/emit.py::emit_placement -> the runner's `emitted`
# package) the cross-tier bridge already uses. We drive the resulting binary in
# `once` mode.
from . import placement as _placement
from ._paths import backends_root
from .errors import RevlError

_BACKENDS_DIR = backends_root()
_GO_DIR = _BACKENDS_DIR / "go"
_GO_RUNNER = _GO_DIR / "placement_runner"
_GO_SCENARIOS = _GO_DIR / "scenarios"

# Phrases `go build` prints when a *resolve* failed — the offline attempt may
# not have the module cached. Anything else that fails is a real build failure
# (or a stale go.sum), which must surface, not read as "unavailable".
_DOWNLOAD_MARKERS = (
    "cannot find module",
    "missing go.sum entry",
    "GOPROXY=off",
    "no required module provides",
    # an older local Go than the go.mod directive wants: GOTOOLCHAIN tries to
    # fetch the newer toolchain itself, which the offline attempt always
    # refuses. Same shape as a cold module cache — worth the networked retry.
    "toolchain not available",
    "download go",
)


def _go_require_line() -> str | None:
    """The `require github.com/0xdenny218/stc-go <pin>` line from the pinned
    scenarios go.mod — the single source of truth for the stc-go version (the
    same helper tools/validate.py keys on)."""
    gomod = _GO_SCENARIOS / "go.mod"
    if not gomod.is_file():
        return None
    for line in gomod.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("require ") and "stc-go" in stripped:
            return stripped
    return None


def _proxy_reachable() -> bool:
    try:
        socket.create_connection(("proxy.golang.org", 443), timeout=5).close()
        return True
    except OSError:
        return False


def _resolve_probe(tmp: Path) -> tuple[bool, str | None]:
    """Can the pinned stc-go actually be obtained and built against? Compiles
    nothing of the corpus — a probe package importing stc-go — offline first,
    then a networked retry only if the module is not already cached. Returns
    ``(ok, reason)``."""
    require = _go_require_line() or (
        "require github.com/0xdenny218/stc-go "
        "v0.6.1-0.20260818143352-b3d6788a428e")
    (tmp / "go.mod").write_text(
        f"module revl_go_run_probe\n\ngo 1.25.0\n\n{require}\n", encoding="utf-8")
    gosum = _GO_SCENARIOS / "go.sum"
    if gosum.is_file():
        shutil.copyfile(gosum, tmp / "go.sum")
    pkg = tmp / "probe"
    pkg.mkdir()
    (pkg / "probe.go").write_text(
        'package probe\n\nimport stc "github.com/0xdenny218/stc-go"\n\n'
        "var _ = stc.NewKey[any]\n", encoding="utf-8")

    env = dict(os.environ)
    offline = subprocess.run(
        ["go", "build", "./..."], cwd=str(tmp), text=True, capture_output=True,
        env={**env, "GOFLAGS": "-mod=mod", "GOPROXY": "off"},
    )
    if offline.returncode == 0:
        return True, None
    blob = ((offline.stderr or "") + (offline.stdout or "")).lower()
    if not any(marker in blob for marker in _DOWNLOAD_MARKERS):
        detail = (offline.stderr or "go build failed").strip().splitlines()
        return False, f"go build probe failed: {detail[-1] if detail else '?'}"
    if not _proxy_reachable():
        return False, ("stc-go is not in the local module cache and "
                       "proxy.golang.org is unreachable — run `go build` once "
                       "with network to populate the module cache")
    networked = subprocess.run(
        ["go", "build", "./..."], cwd=str(tmp), text=True, capture_output=True,
        env=env,
    )
    if networked.returncode == 0:
        return True, None
    detail = (networked.stderr or "go build failed").strip().splitlines()
    return False, f"go build could not resolve stc-go: {detail[-1] if detail else '?'}"


def go_runtime_reason() -> str | None:
    """``None`` when the go tier can actually run here, else why it cannot.

    The once-mode boot compiles the emitted module and the runner against the
    pinned stc-go and runs the binary, so all it needs is a go toolchain with
    stc-go obtainable. The gate mirrors :class:`tools.validate.GoValidator`:
    `go` must be on PATH, and a resolve probe (compiles nothing of the corpus)
    must build — offline first, networked fallback only when the module is not
    cached and the proxy is reachable.
    """
    if shutil.which("go") is None:
        return ("go not on PATH — install a Go toolchain "
                "(>= 1.25, https://go.dev/dl), then re-run")
    with tempfile.TemporaryDirectory(prefix="revl_run_go_probe_") as tmpd:
        ok, reason = _resolve_probe(Path(tmpd))
    if ok:
        return None
    return reason


def _load_order(ir: dict) -> list[str]:
    manifest = ir.get("manifest") or {}
    return manifest.get("loadOrder") or [c["name"] for c in ir.get("components") or []]


def _key_service(ir: dict) -> dict[str, str]:
    """provided key -> its service name, across the composition."""
    out: dict[str, str] = {}
    for comp in ir.get("components") or []:
        for key, service in (comp.get("provides") or {}).items():
            out[key] = service
    return out


def _spec(ir: dict, config: dict) -> dict:
    """The single-process, once-mode runner spec: every component placed
    locally, in load order, tear down and prove no residue on boot. Shape-
    compatible with the placement spec the runner already reads — this is just
    the degenerate one-process placement (no proxies, no serve, no probes)."""
    return {
        "name": "run",
        "components": _load_order(ir),  # go keeps PascalCase names (RevlLoad)
        "config": config,
        "provides": list(_key_service(ir)),
        "proxies": {},
        "probe": [],
        "once": True,
    }


def run_go(ir: dict, config: dict, files, once: bool = False,
           interactive: bool = False) -> int:
    """Emit -> build -> boot the composition on stc-go as a process, then run
    the once round-trip (LIFO teardown + no-residue proof) and exit.

    Returns 0 when the composition booted (``UP``), tore down LIFO, proved no
    residue (``NO-RESIDUE``), and the process exited cleanly; nonzero otherwise.
    A missing/unresolvable go toolchain is a skip-with-reason and exit 3,
    mirroring the rust/java/wasm tiers (never a feint at passing).
    """
    reason = go_runtime_reason()
    if reason is not None:
        print(f"error: the cordis-go (stc-go) runtime is not available.\n"
              f"       {reason}", file=sys.stderr)
        return 3

    if once is False and interactive:
        # the interactive REPL over the go tier is not wired (see the module
        # docstring): hold-and-REPL needs a persistent RPC client against the
        # runner's stub. Be explicit and complete the boot/teardown round-trip.
        print("note: the interactive REPL is wired for the py tier only; the "
              "go tier runs the\n      boot -> teardown -> no-residue "
              "round-trip (as with --once) and exits.", flush=True)

    tmp = Path(tempfile.mkdtemp(prefix="revl_run_go_"))
    try:
        try:
            binary = _placement._build_go(ir, tmp)
        except (RevlError, RuntimeError, OSError) as exc:
            print(f"error: could not build the go composition:\n{exc}",
                  file=sys.stderr)
            return 1

        spec_file = tmp / "run.spec.json"
        spec_file.write_text(json.dumps(_spec(ir, config)), encoding="utf-8")

        print("== load composition (go tier) ==", flush=True)
        proc = subprocess.Popen(
            [binary, str(spec_file)],
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
        shutil.rmtree(tmp, ignore_errors=True)

    if rc != 0:
        print(f"error: the go composition process exited {rc}", file=sys.stderr)
        return 1
    if not (saw_up and saw_down):
        print("error: the go composition did not complete the boot/teardown "
              "round-trip (no UP/DOWN)", file=sys.stderr)
        return 1
    if saw_residue_left or not saw_no_residue:
        print("error: the go composition left residue after teardown",
              file=sys.stderr)
        return 1
    return 0
