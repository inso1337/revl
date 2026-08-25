"""`revl run --backend java` end-to-end (docs/v2.0-roadmap.md §2, "Toward early
production": the exit test wants `git clone && revl run` to print a running
composition on *every* supported tier).

The java tier is wired behind the same driver contract the py tier uses
(src/revl/run.py), but boots the composition as a *separate JVM process* running
the once-mode runner (backends/java/placement/RunOnce.java) on the in-repo
cordis4j runtime, rather than in-process. This file drives the real CLI as a
subprocess, exactly as test_run.py does for py and test_run_rust.py for rust.

The same two honesty rules apply as for the other tiers:

* the boot/exit assertion runs only where a working JDK is present
  (`needs_jdk`); a machine with no JDK (or only the macOS `javac` shim that
  errors until one is installed) *skips with the reason the driver reports* — a
  skipped tier is never green, and never a spurious red;
* the tier is no longer a flat "not wired yet" refusal — that regression is
  guarded on every interpreter, JDK or not.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.run import RUNNABLE_BACKENDS  # noqa: E402
from revl.run_java import JAVAC_RELEASE, java_runtime_reason  # noqa: E402

# A minimal Int-only provider/consumer pair (no config, no strings, no ADTs) —
# so the test does not lean on any richer emitter feature, and the two
# components make the LIFO teardown order observable.
PAIR = str(ROOT / "examples" / "counter_pair.rvl")

# A match-bearing composition (FR-10 / roadmap item 77(e)): the java emitter
# lowers `match` over a Result to a Java 21 pattern `switch`, so the run driver
# must compile the emitted module at `--release 21` — at 17 javac refuses it.
MATCH = str(ROOT / "examples" / "java_match.rvl")

# A legitimately-empty void provide-op (roadmap item 222): `reset()` has an
# empty body, and a consumer calls it during activation. The emitter used to
# render an unrenderable body as a throwing trap, which made this valid no-op
# throw at runtime; the composition must now boot and tear down cleanly.
EMPTY_RESET = str(ROOT / "examples" / "java_empty_reset.rvl")

_JAVA_REASON = java_runtime_reason()
needs_jdk = pytest.mark.skipif(
    _JAVA_REASON is not None,
    reason=f"needs a working JDK: {_JAVA_REASON}")


def _run_cli(args, input_text: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "revl", "run", *args],
        capture_output=True, text=True, input=input_text, env=env,
        check=False)


# ------------------------------------------------------ runtime-independent
#
# These run on every interpreter, JDK present or not.


def test_java_is_a_runnable_backend():
    """The tier is wired: `java` joins `py` in RUNNABLE_BACKENDS. Additive —
    py stays runnable."""
    assert "java" in RUNNABLE_BACKENDS
    assert "py" in RUNNABLE_BACKENDS


def test_java_is_no_longer_a_flat_refusal():
    """`--backend java` must never be the flat `not wired yet` / exit-2 refusal.
    It either boots (JDK present) or skips with a runtime reason and exit 3 (JDK
    absent) — but never rc 2."""
    result = _run_cli([PAIR, "--backend", "java", "--once"], input_text="")
    assert result.returncode != 2, result.stdout + result.stderr
    assert "not wired yet" not in result.stderr


def test_java_plan_reports_the_tier_as_runnable():
    """`--plan` needs no runtime, and now shows java without the
    `(not runnable yet)` caveat."""
    result = _run_cli([PAIR, "--backend", "java", "--plan"], input_text="")
    assert result.returncode == 0, result.stderr
    assert "backend: java" in result.stdout
    assert "not runnable yet" not in result.stdout


def test_java_run_driver_compiles_at_release_21():
    """FR-10 / roadmap item 77(e): the emitted module is Java 21 (the emitter
    lowers `match` to pattern `switch` expressions), so the run driver's javac
    gate must be `--release 21` — the same release `revl test --backend java`
    compiles at and the real cordis4j runtime wants. A 17 gate fails every
    match-bearing composition ("patterns in switch statements are not supported
    in -source 17"). Assertable without a JDK: this is the constant both javac
    invocations read."""
    assert JAVAC_RELEASE == "21"


def test_java_match_composition_emits_a_pattern_switch():
    """The regression's premise, pinned without a JDK: `examples/java_match.rvl`
    really emits a Java 21 pattern `switch` — so a run through a 17 gate would
    fail javac, and only a 21 gate can boot it."""
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location(
        "revl_java_emit", ROOT / "backends" / "java" / "emit.py")
    emit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emit)
    from revl.compiler import compile_files  # noqa: PLC0415

    source = emit.emit(compile_files([MATCH]))
    assert "switch (" in source, "the match must lower to a Java pattern switch"
    assert "case RevlResult.Ok" in source


def test_java_empty_void_op_emits_a_noop_not_a_trap():
    """Roadmap item 222, pinned without a JDK: an empty void provide-op
    (`fn reset() { }`) lowers to an empty method body, not the
    `UnsupportedOperationException` trap the emitter used for bodies it cannot
    render. Assertable off the emitted source — the runtime boot below proves
    the same no-op fires without throwing."""
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location(
        "revl_java_emit", ROOT / "backends" / "java" / "emit.py")
    emit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emit)
    from revl.compiler import compile_files  # noqa: PLC0415

    source = emit.emit(compile_files([EMPTY_RESET]))
    assert "public void reset() {  }" in source, source
    assert "reset() { throw new UnsupportedOperationException" not in source


# --------------------------------------------------------- with the runtime
#
# The golden path: emit java -> javac -> boot the composition as a JVM process
# on cordis4j -> LIFO teardown -> prove no residue -> exit 0.


@needs_jdk
def test_run_java_once_boots_tears_down_lifo_and_proves_no_residue():
    result = _run_cli([PAIR, "--backend", "java", "--once"], input_text="")
    assert result.returncode == 0, result.stderr + result.stdout
    out = result.stdout

    # the composition boots as a real JVM process: both components reach Active,
    # providers first (CounterSvc provides `counter`, CounterUser requires it)
    assert "== load composition (java tier) ==" in out
    assert "CounterSvc" in out and "CounterUser" in out
    assert "state=Active" in out
    assert "[run] UP" in out

    # LIFO teardown: the consumer (CounterUser) is disposed before the provider
    # (CounterSvc) — reverse load order, the same contract the py driver's
    # _dispose_all enforces
    down_user = out.index("swap  | CounterUser")
    down_svc = out.index("swap  | CounterSvc")
    assert down_user < down_svc, "consumer must tear down before its provider"

    # the no-residue proof, read off the live runtime after teardown (the java
    # mirror of the py driver's registry/reflect check): no provided service
    # still resolves
    assert "0 service(s) still provided" in out
    assert "NO-RESIDUE" in out
    assert "[run] DOWN" in out


@needs_jdk
def test_run_java_once_boots_an_empty_void_op_as_a_noop():
    """Roadmap item 222: an empty void provide-op is a real no-op. The consumer
    (ResetUser) calls `reset()` during activation, so the emitter's old throwing
    trap would have blown up the boot; the composition must instead reach Active
    and tear down LIFO with no residue and exit 0."""
    result = _run_cli([EMPTY_RESET, "--backend", "java", "--once"], input_text="")
    assert result.returncode == 0, result.stderr + result.stdout
    out = result.stdout

    assert "== load composition (java tier) ==" in out
    assert "ResetSvc" in out and "ResetUser" in out
    assert "state=Active" in out
    assert "[run] UP" in out
    # the no-op fired without throwing: no UnsupportedOperationException escaped
    assert "UnsupportedOperationException" not in out
    assert "UnsupportedOperationException" not in result.stderr

    # LIFO teardown: the consumer (ResetUser) before the provider (ResetSvc).
    down_user = out.index("swap  | ResetUser")
    down_svc = out.index("swap  | ResetSvc")
    assert down_user < down_svc, "consumer must tear down before its provider"

    assert "0 service(s) still provided" in out
    assert "NO-RESIDUE" in out
    assert "[run] DOWN" in out


@needs_jdk
def test_run_java_once_boots_a_match_composition():
    """FR-10 / roadmap item 77(e): the javac gate is `--release 21`, so a
    match-bearing composition (whose `match` lowers to a Java 21 pattern
    `switch`) must boot through the java run driver's full once round-trip —
    emit -> javac 21 -> boot -> LIFO teardown -> no-residue proof -> exit 0.
    Under the old 17 gate this composition failed javac outright."""
    result = _run_cli([MATCH, "--backend", "java", "--once"], input_text="")
    assert result.returncode == 0, result.stderr + result.stdout
    out = result.stdout

    assert "== load composition (java tier) ==" in out
    assert "Halver" in out and "HalverUser" in out
    assert "state=Active" in out
    assert "[run] UP" in out

    # LIFO teardown: the consumer (HalverUser) before the provider (Halver).
    # (The runner pads the subject to 16 chars, so match the padded form —
    # "swap  | Halver " — or the shorter name prefix-matches its user.)
    down_user = out.index("swap  | HalverUser")
    down_svc = out.index("swap  | Halver ")
    assert down_user < down_svc, "consumer must tear down before its provider"

    assert "0 service(s) still provided" in out
    assert "NO-RESIDUE" in out
    assert "[run] DOWN" in out
