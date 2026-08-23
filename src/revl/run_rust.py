"""Runtime driver for the rust tier (docs/v2.0-roadmap.md §2, "Toward early
production").

`revl run <manifest> --backend rust` wired behind the *same driver contract*
the py tier uses (the ``_Driver`` in :mod:`revl.run`): compile → emit the
cordis-rs module → build the composition into a process → boot it → tear down
LIFO → prove no residue → exit. The py tier boots cordis-py *in-process* and
holds an interactive REPL; the rust tier boots a *separate process* — the same
``backends/rust/placement_runner`` the cross-tier bridge (roadmap item 23)
already drives over its language-agnostic Unix-socket JSON protocol — and
drives that process's lifecycle. The seam is identical; only the address space
differs.

What is wired and runs live (wherever a cordis-rs toolchain is present):

* **--once** — the boot → LIFO teardown → no-residue-proof → exit round-trip.
  The runner loads every component in load order on a real ``cordis::Context``,
  reports ``UP``, then disposes every fiber in reverse (consumers before
  providers), and — in once mode — asserts the live runtime holds nothing
  afterwards (``registry().len() == 0`` and ``reflect().services().len() == 0``,
  the cordis-rs mirror of the py driver's ``registry.size``/``reflect.store``
  check). ``NO-RESIDUE`` is printed only when that holds.

What is NOT yet wired (honestly fenced, not faked):

* the **interactive REPL** over provided rust services. It needs the driver to
  hold an RPC client (``backends/python/bridge.py``'s ``_Client``) against the
  runner's stub for the whole session — the runner already *can* serve, but the
  py-side REPL loop that would forward each typed call across the seam is not
  built here. So without ``--once`` (or on a TTY) the rust driver notes the gap
  and completes the same once round-trip rather than pretending to hold a REPL.

Runtime availability is a gate, not a lie: with no cargo / no resolvable
cordis-rs the driver *skips with a reason* and exits nonzero, exactly as the py
tier does for a missing cordis-py (never a green run that booted nothing).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# read-only reuse of the placement machinery (roadmap item 23): the same emit +
# cargo-build path and the same PascalCase -> snake plugin-name mapping the
# cross-tier bridge already uses. We drive the resulting binary in `once` mode.
from . import placement as _placement
from .errors import RevlError

_RUNNER = _placement._RUST_RUNNER

# Phrases cargo prints when an *offline resolve* could not find a crate — the
# same set tools/validate.py keys on. They appear before any compilation, which
# is what makes them safe to read as "not cached" rather than "build failed".
_OFFLINE_RESOLVE_MARKERS = (
    "you're using offline mode",
    "without the offline flag",
    "--offline was specified",
    "registry index was not found",
    "no matching package",
    "failed to select a version",
)


def _crates_io_reachable() -> bool:
    import socket  # noqa: PLC0415

    try:
        socket.create_connection(("index.crates.io", 443), timeout=5).close()
        return True
    except OSError:
        return False


def rust_runtime_reason() -> str | None:
    """``None`` when the rust tier can actually run here, else why it cannot.

    The gate mirrors :class:`tools.validate.RustValidator`: cargo must be on
    PATH, and cordis-rs must be resolvable — offline first (a warm ~/.cargo
    answers in milliseconds with no network), falling back to a networked
    resolve only when the offline attempt failed for a *resolution* reason and
    the index is reachable. ``generate-lockfile`` runs the resolver the build
    needs and compiles nothing, so this is fast and never launders a real build
    failure into "unavailable".
    """
    if shutil.which("cargo") is None:
        return ("cargo not on PATH — install a Rust toolchain "
                "(https://rustup.rs), then re-run")
    manifest = str(_RUNNER / "Cargo.toml")
    offline = subprocess.run(
        ["cargo", "generate-lockfile", "--offline", "--manifest-path", manifest],
        capture_output=True, text=True,
    )
    if offline.returncode == 0:
        return None
    blob = ((offline.stderr or "") + (offline.stdout or "")).lower()
    if not any(marker in blob for marker in _OFFLINE_RESOLVE_MARKERS):
        # a non-resolution failure (e.g. a malformed manifest) — surface it
        detail = (offline.stderr or "").strip().splitlines()
        return f"cargo could not resolve cordis-rs: {detail[-1] if detail else '?'}"
    if not _crates_io_reachable():
        return ("cordis-rs is not in the local cargo registry and "
                "index.crates.io is unreachable — run cargo once with network "
                "to populate ~/.cargo, then re-run")
    networked = subprocess.run(
        ["cargo", "generate-lockfile", "--manifest-path", manifest],
        capture_output=True, text=True,
    )
    if networked.returncode == 0:
        return None
    detail = (networked.stderr or "").strip().splitlines()
    return f"cargo could not resolve cordis-rs: {detail[-1] if detail else '?'}"


def _load_order(ir: dict) -> list[str]:
    manifest = ir.get("manifest") or {}
    return manifest.get("loadOrder") or [c["name"] for c in ir.get("components") or []]


def _spec(ir: dict, config: dict, files) -> dict:
    """The single-process, once-mode runner spec: every component placed
    locally, in load order, tear down and prove no residue on boot. Shape-
    compatible with the placement spec the runner already reads — this is just
    the degenerate one-process placement (no proxies, no serve)."""
    return {
        "name": "run",
        "backend": "rust",
        "files": [str(f) for f in files],
        "components": [_placement._snake(name) for name in _load_order(ir)],
        "config": config,
        "provides": list({k for c in ir.get("components") or []
                          for k in (c.get("provides") or {})}),
        "proxies": {},
        "probe": [],
        "once": True,
    }


def run_rust(ir: dict, config: dict, files, once: bool = False,
             interactive: bool = False) -> int:
    """Emit → build → boot the composition on cordis-rs as a process, then run
    the once round-trip (LIFO teardown + no-residue proof) and exit.

    Returns 0 when the composition booted (``UP``), tore down, proved no residue
    (``NO-RESIDUE``), and the process exited cleanly; nonzero otherwise. A
    missing/unresolvable cordis-rs toolchain is a skip-with-reason and exit 3,
    mirroring the py tier's missing-runtime path (never a feint at passing).
    """
    reason = rust_runtime_reason()
    if reason is not None:
        print(f"error: the cordis-rs runtime is not available.\n"
              f"       {reason}", file=sys.stderr)
        return 3

    if once is False and interactive:
        # the interactive REPL over the rust tier is not wired (see the module
        # docstring): hold-and-REPL needs a persistent RPC client against the
        # runner's stub. Be explicit and complete the boot/teardown round-trip.
        print("note: the interactive REPL is wired for the py tier only; the "
              "rust tier runs the\n      boot -> teardown -> no-residue "
              "round-trip (as with --once) and exits.", flush=True)

    tmp = Path(tempfile.mkdtemp(prefix="revl_run_rust_"))
    # _build_rust regenerates the runner's committed src/components.rs in place
    # (it is per-composition codegen). Snapshot it so a run leaves the checkout
    # exactly as it found it — the committed module is a golden, not scratch.
    components_rs = _RUNNER / "src" / "components.rs"
    saved = components_rs.read_bytes() if components_rs.exists() else None
    try:
        try:
            binary = _placement._build_rust(ir, tmp)
        except (RevlError, RuntimeError, OSError) as exc:
            print(f"error: could not build the rust composition:\n{exc}",
                  file=sys.stderr)
            return 1

        spec_file = tmp / "run.spec.json"
        spec_file.write_text(json.dumps(_spec(ir, config, files)), encoding="utf-8")

        print("== load composition (rust tier) ==", flush=True)
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
        if saved is not None:
            components_rs.write_bytes(saved)
        shutil.rmtree(tmp, ignore_errors=True)

    if rc != 0:
        print(f"error: the rust composition process exited {rc}", file=sys.stderr)
        return 1
    if not (saw_up and saw_down):
        print("error: the rust composition did not complete the boot/teardown "
              "round-trip (no UP/DOWN)", file=sys.stderr)
        return 1
    if saw_residue_left or not saw_no_residue:
        print("error: the rust composition left residue after teardown",
              file=sys.stderr)
        return 1
    return 0
