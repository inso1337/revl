"""The java tier's javac gate, in one place (issue #154).

A test that greps emitted source proves the emitter wrote what we expected it
to write. It does not prove the result is a valid program, and for a compiler
that is the whole question. Two uncompilable-output defects reached main under
a fully green suite because every assertion covering them was a substring match
on emitted text:

  * a routed require emitted a router class that throws ``CordisException``
    while ``_core_imports`` added that import only for a ``fail`` step, so
    ``scenarios/router.rvl`` emitted 6119 bytes javac rejects;
  * v3 rendered the host Pool's undeclared ``Row`` as a bare ``Row`` type,
    uncompilable for any v3 document carrying ``List[Row]``.

So emission in this suite goes through :func:`compile_check`, and a text
assertion is a claim about a program that javac has already accepted.

Two further things this module exists to keep honest.

**One idea of "a usable JDK".** The gate used to be ``shutil.which("javac")``
plus a ``-version`` probe. macOS ships a ``javac`` shim that satisfies
``which`` and errors when no JDK is installed, and a Homebrew JDK is keg-only —
``/opt/homebrew/opt/openjdk`` is off PATH, so the shim answers first, the probe
fails, and every compile test skips on a machine that has a perfectly good
JDK 26. That is how javac "looked absent" during the audit that found both
defects above. :mod:`revl.run_java` already resolves this correctly for
``revl run --backend java`` (``_working_jdk_bin`` searches the configured
homes, the common install locations and ``/usr/libexec/java_home`` before
PATH, and ``_accepts_release21`` rejects a JDK too old for the pattern
switches the emitter lowers ``match`` to). This module reuses that resolver
rather than inventing a third answer.

**A skip is not a pass.** Off CI a missing JDK leaves the text assertions
running and the compile silently absent, which is the shape roadmap item 445
is about. What stops that from becoming permanent is not a runtime probe, it is
a static one: ``tests/test_java_javac_gate_runs_in_ci.py`` reads the workflow
and fails unless the ``backend-java`` job both provisions a JDK and runs every
file that consumes this gate.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl.run_java import _accepts_release21, _working_jdk_bin  # noqa: E402

# The emitter lowers `match` to Java 21 pattern switches (FR-10 / roadmap item
# 77(e)), so everything here compiles at the release `revl test --backend java`
# and `revl run --backend java` compile at. Kept in one constant so the gate
# and the driver cannot drift apart.
RELEASE = "21"

# The in-repo cordis4j API stubs an emitted unit is compiled against.
STUB_SOURCES = sorted((HERE / "stubs").rglob("*.java"))


def _resolve() -> tuple[str | None, str | None]:
    bin_dir = _working_jdk_bin()
    if bin_dir is None:
        return None, None
    javac, java = Path(bin_dir) / "javac", Path(bin_dir) / "java"
    if not _accepts_release21(javac):
        return None, None
    return str(javac), str(java)


JAVAC, JAVA = _resolve()

# Why a JDK is unusable here, for a skip reason that names the cause instead of
# repeating "no working javac" on a machine that has one.
def jdk_reason() -> str | None:
    """``None`` when the gate can compile, else why it cannot."""
    if JAVAC is None or JAVA is None:
        return ("no JDK >= %s that responds to -version (checked JAVA_HOME/"
                "JAVA21_HOME, the common install locations, java_home and "
                "PATH; on macOS /usr/bin/javac is a shim and a Homebrew JDK is "
                "keg-only — export JAVA_HOME=/opt/homebrew/opt/openjdk)"
                % RELEASE)
    return None


SKIP_REASON = jdk_reason()

_stub_classes: Path | None = None
# Emitted units already accepted by javac, keyed by source text. The suite
# emits the same document from several tests, and compiling each unit once per
# distinct source keeps the gate's cost proportional to what it actually
# covers.
_accepted: set[str] = set()
_checked_units = 0


def stub_classes() -> Path:
    """The compiled cordis4j API stubs, built once per process."""
    global _stub_classes
    if _stub_classes is None:
        out = Path(tempfile.mkdtemp(prefix="revl-java-stubs-"))
        result = subprocess.run(
            [JAVAC, "--release", RELEASE, "-d", str(out)]
            + [str(s) for s in STUB_SOURCES],
            capture_output=True, text=True, timeout=600,
        )
        assert result.returncode == 0, (
            "the in-repo cordis4j stubs no longer compile:\n" + result.stderr)
        _stub_classes = out
    return _stub_classes


def _numbered(source: str, limit: int = 400) -> str:
    lines = source.splitlines()
    body = "\n".join(f"{i:4d}| {line}" for i, line in enumerate(lines[:limit], 1))
    if len(lines) > limit:
        body += f"\n ...| ({len(lines) - limit} more lines)"
    return body


def compile_check(source: str, label: str = "emitted unit") -> str:
    """Prove ``source`` is a valid Java program, and return it unchanged.

    A no-op when no JDK is reachable, so the text assertions still run on a
    toolchain-free checkout (the `frontend` CI job). That the gate is NOT a
    no-op where it matters is asserted statically, in
    ``tests/test_java_javac_gate_runs_in_ci.py`` — see this module's docstring.
    """
    global _checked_units
    if JAVAC is None or source in _accepted:
        return source
    work = Path(tempfile.mkdtemp(prefix="revl-java-unit-"))
    pkg = work / "revl"
    pkg.mkdir()
    unit = pkg / "Components.java"
    unit.write_text(source, encoding="utf-8")
    out = work / "out"
    out.mkdir()
    result = subprocess.run(
        [JAVAC, "--release", RELEASE, "-cp", str(stub_classes()),
         "-d", str(out), str(unit)],
        capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, (
        f"{label}: the emitter produced Java that javac rejects. A substring "
        f"assertion would have passed on this (issue #154).\n\n"
        f"{result.stderr}\n--- emitted ---\n{_numbered(source)}"
    )
    _accepted.add(source)
    _checked_units += 1
    return source


def checked_units() -> int:
    """How many distinct emitted units javac has accepted this process."""
    return _checked_units


def compile_unit(tmp_path: Path, source: str) -> Path:
    """Compile ``source`` AND the stub sources into one classes dir, returned.

    The runtime tests compile a harness against that dir and then run it on a
    JVM with the same dir as the whole classpath, so the stubs have to land
    beside the emitted classes rather than on a separate classpath entry.
    """
    pkg = tmp_path / "revl"
    pkg.mkdir()
    (pkg / "Components.java").write_text(source, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    result = subprocess.run(
        [JAVAC, "--release", RELEASE, "-d", str(out)]
        + [str(s) for s in STUB_SOURCES]
        + [str(pkg / "Components.java")],
        capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stderr
    return out
