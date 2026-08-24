"""Runtime driver for the java tier (docs/v2.0-roadmap.md §2, "Toward early
production").

`revl run <manifest> --backend java` wired behind the *same driver contract*
the py and rust tiers use: compile -> emit the cordis4j module -> build the
composition -> boot it -> tear down LIFO -> prove no residue -> exit. Like the
rust tier (:mod:`revl.run_rust`), java boots as a *separate process* rather than
in-process — a JVM running the once-mode runner
(``backends/java/placement/RunOnce.java``), the single-process sibling of the
``PlacementRunner`` the cross-tier bridge (roadmap item 23) already drives. The
seam is identical to rust's; only the language of the child process differs.

What is wired and runs live (wherever a working JDK is present):

* **--once** — the boot -> LIFO teardown -> no-residue-proof -> exit round-trip
  on the in-repo cordis4j runtime (``backends/java/stubs`` — a faithful
  implementation of the cordis4j core API: real ``Context``/``Plugin`` lifecycle
  and real LIFO effect-scope teardown). The runner loads every component in load
  order, reports ``UP``, disposes every fiber in reverse (consumers before
  providers), and asserts the live runtime holds nothing afterwards: no provided
  service still resolves through ``ctx.get`` (the java mirror of the py driver's
  ``registry.size``/``reflect.store`` check and the rust runner's
  ``registry().len()``/``reflect().services()`` check). ``NO-RESIDUE`` is printed
  only when that holds.

What is NOT the ``run`` once-path's job (honestly fenced, not faked):

* the **reactive real-cordis4j runtime** (JDK 21 + a compiled cordis4j-core,
  ``REVL_CORDIS4J_CLASSES``) — with peer-death-as-withdrawal — is exercised by
  ``revl run --placement`` and the java scenarios, not by this single-process
  once round-trip. A single-process ``run --once`` has no peer to withdraw, so
  the stub runtime carries its full contract (load, LIFO teardown, no residue);
* the **interactive REPL** over provided java services, for the same reason it
  is unwired on the rust tier (see :mod:`revl.run_rust`): it needs a persistent
  RPC client against a served stub. Without ``--once`` the driver notes the gap
  and completes the same once round-trip rather than pretending to hold a REPL.

The emitted module is Java 21 (the emitter lowers ``match`` to pattern
``switch`` expressions, docs/v2.0-roadmap.md item 77(e)), so the driver compiles
with ``--release 21`` — the same gate ``revl test --backend java`` uses, and
what the real cordis4j runtime (JDK 21) wants. Compiling the emitted module at
17 is the bug this driver used to carry: any ``match`` failed javac.

Runtime availability is a gate, not a lie: with no working JDK the driver *skips
with a reason* and exits nonzero, exactly as the py tier does for a missing
cordis-py (never a green run that booted nothing). macOS ships a ``javac`` shim
that errors when no JDK is installed, so the gate checks that ``javac``/``java``
actually respond, not merely that they are on PATH.
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

from .errors import RevlError

_BACKENDS_DIR = Path(__file__).resolve().parents[2] / "backends"
_JAVA_DIR = _BACKENDS_DIR / "java"
_PLACEMENT_DIR = _JAVA_DIR / "placement"

# The emitted module is Java 21 (pattern `switch` from `match`), the same
# release `revl test --backend java` compiles at and the real cordis4j runtime
# (JDK 21) wants. Kept as a module constant so the gate is assertable without a
# JDK on PATH (tests/test_run_java.py).
JAVAC_RELEASE = "21"


def _responds(exe: str) -> bool:
    """Whether a toolchain binary actually works (macOS ships a ``javac`` shim
    that errors when no JDK is installed, so being on PATH is not enough)."""
    try:
        probe = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def _working_jdk_bin() -> str | None:
    """The ``bin`` dir of a JDK whose ``javac`` and ``java`` both respond, or
    ``None``. Prefers an explicitly configured JDK, then common install
    locations, then PATH (verified, not merely present)."""
    candidates: list[Path] = []
    for env in ("JAVA21_HOME", "JAVA_HOME"):
        home = os.environ.get(env)
        if home:
            candidates.append(Path(home) / "bin")
    candidates += [
        Path("/opt/homebrew/opt/openjdk/bin"),
        Path("/opt/homebrew/opt/openjdk@21/bin"),
        Path("/opt/homebrew/opt/openjdk@17/bin"),
        Path("/usr/lib/jvm/temurin-21-jdk/bin"),
        Path("/usr/lib/jvm/temurin-17-jdk/bin"),
    ]
    # macOS java_home knows about installed JDKs the shim hides
    try:
        found = subprocess.run(["/usr/libexec/java_home"], capture_output=True, text=True, timeout=30)
        if found.returncode == 0 and found.stdout.strip():
            candidates.append(Path(found.stdout.strip()) / "bin")
    except (OSError, subprocess.TimeoutExpired):
        pass
    on_path = shutil.which("javac")
    if on_path:
        candidates.append(Path(on_path).resolve().parent)
    for bin_dir in candidates:
        javac, java = bin_dir / "javac", bin_dir / "java"
        if javac.exists() and java.exists() and _responds(str(javac)) and _responds(str(java)):
            return str(bin_dir)
    return None


def java_runtime_reason() -> str | None:
    """``None`` when the java tier can actually run here, else why it cannot.

    The single-process ``run --once`` boot compiles the emitted module and the
    once-runner against the in-repo cordis4j stubs and runs them on a JVM, so all
    it needs is a *working* JDK (>= 21, the release the emitter targets). A
    missing JDK is a skip-with-reason, not a red run — the same shape the rust
    tier uses for a missing cargo/cordis-rs.
    """
    bin_dir = _working_jdk_bin()
    if bin_dir is None:
        return ("no working JDK found (javac/java that respond to -version).\n"
                "       install a JDK (>= 21), or point JAVA_HOME/JAVA21_HOME at one, then re-run.\n"
                "       (macOS ships a javac shim that errors until a JDK is installed.)")
    # the emitter lowers `match` to Java 21 pattern switches (FR-10 / item
    # 77(e)), so the run driver compiles at --release 21. A JDK older than 21
    # responds to -version yet cannot build the composition — that must be a
    # skip-with-reason, not a red run (the frontend CI image used to carry one).
    if not _accepts_release21(Path(bin_dir) / "javac"):
        return ("the working JDK is older than 21 (the emitter's `match` lowers to\n"
                "       Java 21 pattern switches — FR-10 / roadmap item 77(e)); install a JDK\n"
                "       (>= 21), or point JAVA_HOME/JAVA21_HOME at one, then re-run.")
    return None


def _accepts_release21(javac: Path) -> bool:
    """``True`` when this javac accepts ``--release 21`` (a JDK >= 21)."""
    try:
        probe = subprocess.run(
            [str(javac), "--release", "21", "-version"],
            capture_output=True, text=True, timeout=30)
        return probe.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _load_order(ir: dict) -> list[str]:
    manifest = ir.get("manifest") or {}
    return manifest.get("loadOrder") or [c["name"] for c in ir.get("components") or []]


def _key_service(ir: dict) -> dict[str, str]:
    """provided key -> its service name, across the composition (only provides —
    a required-but-not-provided key answers to no local service here)."""
    out: dict[str, str] = {}
    for comp in ir.get("components") or []:
        for key, service in (comp.get("provides") or {}).items():
            out[key] = service
    return out


def _emit_components(ir: dict, gen_dir: Path) -> None:
    spec = importlib.util.spec_from_file_location("revl_java_emit", _JAVA_DIR / "emit.py")
    emit_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emit_module)
    (gen_dir / "Components.java").write_text(emit_module.emit(ir), encoding="utf-8")


def _build(ir: dict, tmp: Path, jdk_bin: str) -> str:
    """Emit revl/Components.java and compile it + the cordis4j stubs +
    PlacementRunner (for its shared JSON parser) + RunOnce into a classes dir;
    return that dir (the ``java -cp`` classpath)."""
    out = tmp / "java_out"
    out.mkdir()
    gen = tmp / "java_gen" / "revl"
    gen.mkdir(parents=True)
    _emit_components(ir, gen)

    javac = str(Path(jdk_bin) / "javac")
    stubs = [str(p) for p in (_JAVA_DIR / "stubs").rglob("*.java")]
    compile_runner = subprocess.run(
        [javac, "--release", JAVAC_RELEASE, "-d", str(out), *stubs,
         str(_PLACEMENT_DIR / "PlacementRunner.java"), str(_PLACEMENT_DIR / "RunOnce.java")],
        capture_output=True, text=True,
    )
    if compile_runner.returncode:
        raise RuntimeError(f"javac (runner) failed:\n{compile_runner.stderr.strip()}")
    compile_components = subprocess.run(
        [javac, "--release", JAVAC_RELEASE, "-cp", str(out), "-d", str(out), str(gen / "Components.java")],
        capture_output=True, text=True,
    )
    if compile_components.returncode:
        raise RuntimeError(f"javac (components) failed:\n{compile_components.stderr.strip()}")
    return str(out)


def _spec(ir: dict, config: dict) -> dict:
    key_service = _key_service(ir)
    return {
        "name": "run",
        "module": "revl.Components",
        "components": _load_order(ir),
        "config": config,
        "provides": list(key_service),
        "ifaces": {k: f"revl.Components${s}" for k, s in key_service.items()},
    }


def run_java(ir: dict, config: dict, files, once: bool = False,
             interactive: bool = False) -> int:
    """Emit -> build -> boot the composition on the cordis4j runtime as a JVM
    process, then run the once round-trip (LIFO teardown + no-residue proof) and
    exit. Returns 0 on a clean ``UP`` -> ``NO-RESIDUE`` -> ``DOWN``; nonzero
    otherwise. A missing JDK is a skip-with-reason and exit 3, mirroring the py
    and rust tiers (never a feint at passing)."""
    reason = java_runtime_reason()
    if reason is not None:
        print(f"error: the java (cordis4j) runtime is not available.\n"
              f"       {reason}", file=sys.stderr)
        return 3

    if once is False and interactive:
        print("note: the interactive REPL is wired for the py tier only; the "
              "java tier runs the\n      boot -> teardown -> no-residue "
              "round-trip (as with --once) and exits.", flush=True)

    jdk_bin = _working_jdk_bin()
    assert jdk_bin is not None  # java_runtime_reason() already gated on this
    tmp = Path(tempfile.mkdtemp(prefix="revl_run_java_"))
    try:
        try:
            classpath = _build(ir, tmp, jdk_bin)
        except (RevlError, RuntimeError, OSError) as exc:
            print(f"error: could not build the java composition:\n{exc}",
                  file=sys.stderr)
            return 1

        spec_file = tmp / "run.spec.json"
        spec_file.write_text(json.dumps(_spec(ir, config)), encoding="utf-8")

        print("== load composition (java tier) ==", flush=True)
        proc = subprocess.Popen(
            [str(Path(jdk_bin) / "java"), "-cp", classpath, "RunOnce", str(spec_file)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
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
        print(f"error: the java composition process exited {rc}", file=sys.stderr)
        return 1
    if not (saw_up and saw_down):
        print("error: the java composition did not complete the boot/teardown "
              "round-trip (no UP/DOWN)", file=sys.stderr)
        return 1
    if saw_residue_left or not saw_no_residue:
        print("error: the java composition left residue after teardown",
              file=sys.stderr)
        return 1
    return 0
