"""The java placement runners' uncaught-failure channel sits behind the
redaction funnel (issue #814, the java half of advisory GHSA-4x4q-296m-9x9x L1).

`log()` funnels every line this process prints and `seam_failure` funnels the
reply it sends back across the seam, but the failures those two expect are not
the only ones a boot can raise: a config hook, a serve setup, an inverse during
teardown — anything the load path and the probe path do not catch — used to
escape `main` as a bare stack trace on `System.err`, which the conductor merges
verbatim (`placement.py::pump` spawns children with `stderr=STDOUT`). The stack
trace quotes whatever the failing frame interpolated, and no funnel ever saw it.
The py tier closed this in the same issue (`_process_runner.main`'s catch-all);
java had no `System.err` sink of its own at all.

WHAT IS COVERED HERE, CHANNEL BY CHANNEL. Each row names the site in
`placement/PlacementRunner.java` (and its reactive twin) that would have to be
un-wired for that test to fail, except the shape tests, which pin a property
rather than a redaction:

  the catch-all        test_both_runners_wrap_main_in_a_catch_all
                       delete the wrapper -> FAIL
  the FATAL line       test_the_fatal_line_is_printed_to_stderr_through_the_funnel
                       un-funnel the `redactFatal` call -> FAIL
  `halt`, not `exit`   test_the_fatal_path_halts_so_no_down_line_is_printed
                       swap `halt` for `exit` -> FAIL (the shutdown hook would
                       print `DOWN`, the conductor's clean-teardown signal, for
                       a process that died mid-boot — E7)
  the funnel itself    test_a_funnel_that_cannot_run_withholds_the_detail
                       the registry's redactor THROWS when the container's
                       `revlRedactText` cannot be invoked; a failure raised
                       inside the failure path would escape as exactly the bare
                       stack trace this funnel exists to remove
  the whole channel    test_an_uncaught_boot_failure_prints_one_redacted_line
                       live JVM: the real emitted registry, the real runner
  non-vacuity          test_with_the_fatal_funnel_unwired_the_value_appears
                       the same run with `redactFatal(message)` -> `message`
                       DOES print the value
  the OTHER threads    test_the_thread_funnel_is_installed_before_anything_starts_a_thread
                       a java `try` covers the thread that runs it, so main's
                       catch-all is a fence around main's own frame; the JVM's
                       default handler prints every other thread's failure as a
                       bare stack trace on the same channel
  the thread funnel's  test_the_thread_funnel_never_prints_its_own_stack_trace
  own failure          a handler that throws escapes as exactly the trace the
                       channel exists to remove, and it can fire before the
                       container is loaded
  the thread channel   test_a_failure_on_another_thread_prints_one_redacted_line
                       live JVM: the real `installThreadFunnel`, the real
                       `failAndHalt`, the real registry
  non-vacuity (2)      test_without_the_thread_funnel_the_runtime_prints_the_value
                       the same harness with the install line deleted leaks the
                       value the way the JVM prints an uncaught failure

The live tests need a JDK and are skipped without one, exactly like the
neighbouring item-421 files. Every assertion is PAIRED (canary absent AND
marker present) so none can pass on an empty line.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import javac_gate  # noqa: E402
from revl import compile_source  # noqa: E402

PLACEMENT = BACKEND / "placement"
SCENARIO = (BACKEND / "scenarios" / "secret_registry.rvl").read_text()
RUNNERS = (PLACEMENT / "PlacementRunner.java", PLACEMENT / "RealPlacementRunner.java")

# The canary is a class name ON PURPOSE. The uncaught failure this funnel covers
# is whatever the failing frame raises, and the emitted `Components` resolves
# each component by name: an unresolvable one raises `ClassNotFoundException`
# whose MESSAGE is the name it was handed. So a declared `Secret[T]` holding
# this exact string is registered by the plugin that loads first and then quoted
# verbatim by the exception the next component raises — the plain shape a driver
# error takes, with nothing the author wrote interpolating the secret.
CANARY_COMPONENT = "SEKRIT_JAVA_FATAL_814"
CANARY = f"revl.Components${CANARY_COMPONENT}Plugin"
# The ordinary config value beside it: the control that the registry redacts
# what was declared and nothing else.
PUBLIC_URL = "pg://real-host-5432/app"
REDACTED_SECRET = "<redacted:secret>"
WITHHELD = "<detail withheld: redaction unavailable>"
WITHHELD_FUNNEL = "<detail withheld: funnel unavailable>"

needs_jdk = pytest.mark.skipif(javac_gate.JAVAC is None, reason=javac_gate.NO_JDK)


def _emitter():
    spec = importlib.util.spec_from_file_location("revl_java_emit", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _emitted() -> str:
    """The real emitted unit for the secret-registry scenario, compiled."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ir = compile_source(SCENARIO)
    return javac_gate.compile_check(_emitter().emit(ir), "fatal funnel")


def _classpath(work: Path, runner_source: str, components_source: str,
               extra: tuple[str, str] | None = None) -> Path:
    """Compile the in-repo cordis4j stubs + a PlacementRunner source + the
    emitted unit into one classes dir, the way `placement._build_java` does."""
    runner = work / "PlacementRunner.java"
    runner.write_text(runner_source, encoding="utf-8")
    sources = [str(PLACEMENT / "Estop.java"), str(runner)]
    if extra is not None:
        name, text = extra
        (work / name).write_text(text, encoding="utf-8")
        sources.append(str(work / name))
    gen = work / "revl"
    gen.mkdir()
    (gen / "Components.java").write_text(components_source, encoding="utf-8")
    out = work / "out"
    out.mkdir()
    javac = [javac_gate.JAVAC, "--release", javac_gate.RELEASE, "-d", str(out)]
    result = subprocess.run(
        javac + [str(s) for s in javac_gate.STUB_SOURCES] + sources,
        capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        javac + ["-cp", str(out), str(gen / "Components.java")],
        capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr
    return out


def _boot_that_fails(classpath: Path) -> tuple[int, str, str]:
    """Boot one runner on a spec whose SECOND component cannot resolve, and
    return (exit code, stdout, stderr).

    `Keeper` loads first and its constructor registers the declared
    `Secret[T]`; the component after it is named by the canary, so the
    `ClassNotFoundException` the loop raises quotes the registered value. The
    spec carries no `serve` and no `probe`, so nothing funnelled by the load or
    probe path can be what prints.
    """
    work = Path(tempfile.mkdtemp(prefix="revl-jfatal-"))
    spec = work / "boot.json"
    spec.write_text(json.dumps({
        "name": "boot", "module": "revl.Components",
        "components": ["Keeper", CANARY_COMPONENT],
        "config": {"Keeper": {"url": PUBLIC_URL, "api_key": CANARY}},
    }))
    result = subprocess.run(
        [javac_gate.JAVA, "-cp", str(classpath), "PlacementRunner", str(spec)],
        capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    return result.returncode, result.stdout, result.stderr


# ---------------------------------------------------------------------------
# the shape (runs everywhere)
# ---------------------------------------------------------------------------

# The exact line `failAndHalt` composes. It is the ONE composer: `main`'s catch
# and the default uncaught-exception handler both go through it, so the two
# channels cannot drift apart.
_FATAL_PRINT = (
    '        System.err.println("[" + label + "] FATAL " + redactFatal(message));\n')


def test_both_runners_wrap_main_in_a_catch_all():
    """Both entry points, because `placement._build_java` picks between them:
    the stub runtime gets `PlacementRunner`, the real cordis4j gets the reactive
    twin, and a channel closed in one of them is still open in the other."""
    for path in RUNNERS:
        source = path.read_text(encoding="utf-8")
        assert ("public static void main(String[] argv) {\n"
                "        installThreadFunnel();\n"
                "        try {\n"
                "            run(argv);\n") in source, path
        assert "        } catch (Throwable fatal) {\n            failAndHalt(fatal);\n" in source, path
        assert "    static void run(String[] argv) throws Exception {\n" in source, path
        # the entry point itself no longer declares `throws`: a checked failure
        # reaching it is a failure the catch-all must see, not one the JVM
        # prints itself.
        assert "main(String[] argv) throws" not in source, path


def test_the_thread_funnel_is_installed_before_anything_starts_a_thread():
    """A java `try` covers the thread that runs it, so `main`'s catch-all is a
    fence around main's own frame: the JVM's default handler prints a failure on
    any OTHER thread as a bare stack trace on `System.err`, the same unfunnelled
    channel one thread over. This runner starts several — the `revl-estop`
    watcher, `bridge-stub`'s accept loop, a `bridge-conn` per connection — and
    the last carries a crossing's own arguments.

    `Thread.setDefaultUncaughtExceptionHandler` is the only process-wide channel
    java offers, and it has to be installed BEFORE `run` starts any of them."""
    for path in RUNNERS:
        source = path.read_text(encoding="utf-8")
        main = source[source.index("public static void main(String[] argv) {"):]
        main = main[:main.index("static void run(String[] argv)")]
        assert "Thread.setDefaultUncaughtExceptionHandler(" in main, path
        # installed first, not after `run` has already spawned a thread.
        assert main.index("installThreadFunnel();") < main.index("run(argv);"), path
        # the handler routes through the SAME composer as the catch-all, so a
        # second channel cannot be added that quietly forgets the funnel.
        assert source.count('System.err.println("[" + label + "] FATAL "') == 1, path
        # ...and a thread with a handler of its own would bypass the default one.
        assert "setUncaughtExceptionHandler(" not in source.replace(
            "setDefaultUncaughtExceptionHandler(", ""), path


def test_the_thread_funnel_never_prints_its_own_stack_trace():
    """A failure raised INSIDE the handler would escape as exactly the bare
    stack trace the channel exists to remove — and it is likelier here than in
    `main`'s catch, because the handler can fire before the container is loaded
    and `redactFatal` only catches `RuntimeException`. So the handler withholds
    the detail and dies: one line, still, and no trace."""
    for path in RUNNERS:
        source = path.read_text(encoding="utf-8")
        handler = source[source.index("static void installThreadFunnel() {"):]
        handler = handler[:handler.index("\n    }\n")]
        assert "failAndHalt(fatal);" in handler, path
        assert "} catch (Throwable funnelDown) {" in handler, path
        assert WITHHELD_FUNNEL in handler, path
        assert "Runtime.getRuntime().halt(1);" in handler, path
        assert "printStackTrace" not in handler, path


def test_the_fatal_line_is_printed_to_stderr_through_the_funnel():
    """One redacted line on stderr — the channel the conductor merges into the
    interleaved trace — carrying the failing type and, through the funnel, its
    message."""
    for path in RUNNERS:
        source = path.read_text(encoding="utf-8")
        assert _FATAL_PRINT in source, path
        assert '        System.err.flush();\n' in source, path
        # the class simple name matches the probe arm's rendering, and a
        # message-less failure still prints a readable line.
        assert "String message = detail == null\n" in source, path
        assert "fatal.getClass().getSimpleName() + \": \" + detail;\n" in source, path
        # `name` is read from the spec, so a boot that fails before it is read
        # (a missing spec file, a spec without `name`) still prints a label —
        # and it is read at FAILURE time, from a field the main thread wrote,
        # which is why it is volatile.
        assert 'String label = name == null ? "?" : name;\n' in source, path
        assert "static volatile String name = " in source, path


def test_the_fatal_path_halts_so_no_down_line_is_printed():
    """`halt`, not `exit`: the shutdown hook prints `[name] DOWN`, which is what
    the conductor reads as a clean teardown. A process that died mid-boot must
    not claim one — the same "die where it stands, non-zero, no DOWN" rule the
    E-Stop watcher follows (E7)."""
    for path in RUNNERS:
        source = path.read_text(encoding="utf-8")
        fatal = source[source.index("static void failAndHalt(Throwable fatal) {"):]
        fatal = fatal[:fatal.index("\n    }\n")]
        assert "Runtime.getRuntime().halt(1);" in fatal, path
        assert "System.exit(" not in fatal, path


def test_a_funnel_that_cannot_run_withholds_the_detail():
    """`bindSecretRegistry` installs a redactor that THROWS when the container's
    `revlRedactText` cannot be invoked, because a funnel that cannot run must
    not let the text through. The FATAL line is the one caller that cannot
    propagate that: a failure raised inside the failure path would escape as
    exactly the bare stack trace the funnel exists to remove. So it withholds
    the detail instead."""
    for path in RUNNERS:
        source = path.read_text(encoding="utf-8")
        assert "    static String redactFatal(String text) {\n" in source, path
        body = source[source.index("static String redactFatal(String text) {"):]
        body = body[:body.index("\n    }\n")]
        assert "        } catch (RuntimeException funnelDown) {\n" in body, path
        assert f'            return "{WITHHELD}";\n' in body, path
        assert "redactSecrets(text)" in body, path


# ---------------------------------------------------------------------------
# the live channel: one JVM, the real registry, the real runner
# ---------------------------------------------------------------------------

@needs_jdk
def test_an_uncaught_boot_failure_prints_one_redacted_line(tmp_path):
    classpath = _classpath(tmp_path, RUNNERS[0].read_text(encoding="utf-8"), _emitted())
    code, out, err = _boot_that_fails(classpath)

    # the run really raised the failure under test, and it really quoted the
    # registered value: without this the absence assertions would be vacuous
    assert code == 1, (out, err)
    assert err.count("FATAL") == 1, err
    assert "ClassNotFoundException" in err, err
    assert REDACTED_SECRET in err, err

    # the credential is nowhere on either stream, and the marker took its place
    assert CANARY not in out + err, (out, err)
    assert err.strip() == f"[boot] FATAL ClassNotFoundException: {REDACTED_SECRET}"

    # a process that died mid-boot printed no protocol line: no `UP` it never
    # reached, and no `DOWN` it did not earn.
    assert "UP" not in out, out
    assert "DOWN" not in out, out
    # ...and the ordinary config value beside it is still verbatim, so the
    # funnel redacts what was declared and nothing else. (It is not printed on
    # this path at all; the marker's presence above is what proves the funnel
    # ran rather than the message having been dropped.)
    assert PUBLIC_URL not in err, err


@needs_jdk
def test_with_the_fatal_funnel_unwired_the_value_appears(tmp_path):
    """The non-vacuity arm: the same run, one edit to the wrapper."""
    source = RUNNERS[0].read_text(encoding="utf-8")
    assert _FATAL_PRINT in source
    unwired = source.replace(
        _FATAL_PRINT,
        '            System.err.println("[" + label + "] FATAL " + message);\n')
    classpath = _classpath(tmp_path, unwired, _emitted())
    code, out, err = _boot_that_fails(classpath)

    assert code == 1, (out, err)
    assert CANARY in err, err
    assert REDACTED_SECRET not in err, err


# ---------------------------------------------------------------------------
# the live thread channel
# ---------------------------------------------------------------------------

# Drives the runner's own thread funnel in a real JVM: the real
# `installThreadFunnel`, the real `failAndHalt`, the real registry, the real
# halt. The failing thread is a plain one because NONE of the runner's own
# threads has a deterministic escape — the seam's per-connection handler wraps
# every request in `catch (Throwable)`, `Estop.readLatch` fails closed rather
# than throwing, and `peer-monitor` catches its own — so what this arm pins is
# the CHANNEL (a thread other than the one that runs `main`), while the shape
# arms above pin that `main` installs it before `run` and that the composer is
# shared.
#
# `Keeper` is instantiated the way the boot path instantiates it (the runner's
# own `instantiate`, over a LinkedHashMap so the constructor's positional
# arguments keep the spec's order), because the declared `Secret[T]` is
# registered by the component that holds it.
#
# The two substitutions are token-replaced rather than `str.format`-ed: the
# harness is java, so it is made of braces.
_THREAD_HARNESS = """
import java.util.LinkedHashMap;
import java.util.Map;

public class ThreadFunnelHarness {
    public static void main(String[] argv) throws Exception {
        PlacementRunner.bindSecretRegistry("revl.Components");
        PlacementRunner.name = "boot";
        PlacementRunner.installThreadFunnel();
        Map<String, Object> config = new LinkedHashMap<>();
        config.put("url", "@URL@");
        config.put("api_key", "@CANARY@");
        PlacementRunner.instantiate(Class.forName("revl.Components$KeeperPlugin"), config);
        Thread worker = new Thread(() -> { throw new RuntimeException("boom " + "@CANARY@"); });
        worker.start();
        worker.join();
        System.out.println("worker returned");
    }
}
"""


def _harness_source() -> str:
    return _THREAD_HARNESS.replace("@URL@", PUBLIC_URL).replace("@CANARY@", CANARY)


_INSTALL_LINE = "        PlacementRunner.installThreadFunnel();\n"


def _run_harness(classpath: Path) -> tuple[int, str, str]:
    result = subprocess.run(
        [javac_gate.JAVA, "-cp", str(classpath), "ThreadFunnelHarness"],
        capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    return result.returncode, result.stdout, result.stderr


@needs_jdk
def test_a_failure_on_another_thread_prints_one_redacted_line(tmp_path):
    """The channel `main`'s catch cannot reach: the failure is raised on a
    thread other than the one that runs `main`, and the JVM's default handler
    used to print it — value and stack trace — straight to `System.err`."""
    classpath = _classpath(tmp_path, RUNNERS[0].read_text(encoding="utf-8"), _emitted(),
                           extra=("ThreadFunnelHarness.java", _harness_source()))
    code, out, err = _run_harness(classpath)

    # the run really raised the failure under test, and it really quoted the
    # registered value: without this the absence assertions would be vacuous
    assert code == 1, (out, err)
    assert err.count("FATAL") == 1, err
    assert "RuntimeException" in err, err
    assert REDACTED_SECRET in err, err
    assert err.strip() == f"[boot] FATAL RuntimeException: boom {REDACTED_SECRET}"

    # the credential is nowhere on either stream, and the JVM's own rendering of
    # an uncaught failure — the bare stack trace this channel exists to remove —
    # is gone with it.
    assert CANARY not in out + err, (out, err)
    assert "Exception in thread" not in err, err
    assert "at ThreadFunnelHarness" not in err, err

    # and the halt really happened: the thread that raised never ran on to its
    # own continuation, so nothing after it printed.
    assert "worker returned" not in out, out
    assert PUBLIC_URL not in err, err


@needs_jdk
def test_without_the_thread_funnel_the_runtime_prints_the_value(tmp_path):
    """The non-vacuity arm: the same harness with the install line deleted, so
    the failure is the JVM's default handler and its rendering is the bare stack
    trace this channel exists to replace."""
    source = _harness_source()
    assert _INSTALL_LINE in source
    classpath = _classpath(tmp_path, RUNNERS[0].read_text(encoding="utf-8"), _emitted(),
                           extra=("ThreadFunnelHarness.java", source.replace(_INSTALL_LINE, "")))
    code, out, err = _run_harness(classpath)

    assert CANARY in err, err
    assert "Exception in thread" in err, err
    assert REDACTED_SECRET not in err, err
    # the canary's own text contains "FATAL", so the marker has to be the
    # funnelled LINE and not the word
    assert "] FATAL " not in err, err
