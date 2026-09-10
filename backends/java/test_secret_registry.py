"""The java tier's declared `Secret[T]` registry (roadmap item 421 F6, the java half).

A config field declared `Secret[T]` is handed to the component's own host
binding, the legitimate use the checker does not refuse. The host body then
fails with a message that quotes its arguments, the plain shape a driver error
takes. Three sinks could repeat the credential: the console the runner prints,
the seam-failure text a provider sends back to its consumer, and the WAL under
`--record`. py, ts and go register a declared value with a runtime registry and
funnel every sink through it; java had only the argument stage of the seam
funnel, which cannot see a held credential that is not among the failing call's
own arguments.

The emitted `Components` now carries the registry (`revlMarkSecret` in the
plugin constructor, `revlRedactText` over it) and the runners bind to it
reflectively, so every console line and every seam reply reads through it.

What is proved here, by RUNNING two JVMs across a real socket seam and grepping
what they printed (never by asserting that a redaction function was called):

* the value appears nowhere in the trace, the seam-failure text is still worth
  reading, an ordinary config value beside it is still verbatim, and the
  caller's own argument still carries the argument marker;
* a deliberately more aggressive unwrap of the dispatch failure in a copy of
  the runner still leaks nothing, so the registry stage closes the path rather
  than the current shape of the message merely not reaching it;
* the same run with the registry unbound, and with the constructor's
  registration stripped, DOES print the value: the assertions above are not
  vacuous.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import socket as sockets_mod
import subprocess
import sys
import tempfile
import time
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

# Long enough that an exact match means something, and not a substring of
# anything else the run prints.
CANARY = "SEKRIT-JAVA-CANARY-421-F6"
# The ordinary config value beside it: the control that the registry redacts
# what was declared and nothing else.
PUBLIC_URL = "pg://real-host-5432/app"
REDACTED_SECRET = "<redacted:secret>"
REDACTED_ARG = "<redacted:arg>"

needs_jdk = pytest.mark.skipif(javac_gate.JAVAC is None, reason=javac_gate.NO_JDK)


def _emitter():
    spec = importlib.util.spec_from_file_location("revl_java_emit", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _compile(source: str) -> dict:
    """A literal default on a `Secret[T]` field warns (it is source, so it is in
    the IR); the scenario needs one so the no-arg constructor door exists."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return compile_source(source)


def _emit(source: str = SCENARIO, **kwargs) -> str:
    """Emit, and prove the emitted unit compiles before asserting on its text."""
    return javac_gate.compile_check(_emitter().emit(_compile(source), **kwargs),
                                    "secret registry")


# ---------------------------------------------------------------------------
# the emitted shape (runs everywhere; the compile gate needs a JDK)
# ---------------------------------------------------------------------------

def test_the_plugin_registers_the_secret_config_field_at_load():
    code = _emit()
    # both doors: the parameterised constructor the runner instantiates through
    # from `[config.<Comp>]`, and the no-arg one when the declared default stands
    assert code.count("revlMarkSecret(this.api_key);") == 2, code
    # ...and only the declared field: the ordinary one beside it is not marked
    assert "revlMarkSecret(this.url" not in code
    assert "this.url, " not in code.split("revlMarkSecret(")[1].split(")")[0]
    # the registry itself, once, with the shared marker
    assert code.count("public static String revlRedactText(String text)") == 1
    assert 'REVL_REDACTED_SECRET = "<redacted:secret>"' in code


def test_a_secretless_document_is_byte_identical():
    """The registry is emitted only for a document that declares a `Secret[T]`,
    so every existing golden and the selfhost mirror stay untouched."""
    plain = SCENARIO.replace("api_key: Secret[Str]", "api_key: Str").replace(
        "key: Secret[Str]", "key: Str")
    code = javac_gate.compile_check(_emitter().emit(_compile(plain)), "secretless control")
    assert "revlMarkSecret" not in code
    assert "revlRedactText" not in code
    assert "REVL_REDACTED_SECRET" not in code


def test_the_wal_descriptor_reads_through_the_registry_under_record():
    """The WAL is a plaintext file at rest: under `--record` a descriptor
    argument is scrubbed before it is written. Emitted only in secret mode, on
    the witnessed composition the crash-recovery proof records."""
    fixture = BACKEND / "scenarios" / "crashproof" / "crashproof.ir.json"
    ir = json.loads(fixture.read_text())
    ir["components"][0]["config"] = [
        {"name": "api_key", "type": "Str", "default": "k", "secret": True}]
    code = javac_gate.compile_check(_emitter().emit(ir, record=True), "record + secret")
    assert "revlWalStr(revlRedactText(args[i]))" in code
    assert "revlMarkSecret(this.api_key);" in code
    plain = javac_gate.compile_check(
        _emitter().emit(json.loads(fixture.read_text()), record=True), "record control")
    assert "revlWalStr(args[i])" in plain
    assert "revlRedactText" not in plain


# ---------------------------------------------------------------------------
# two JVMs, one socket seam, grep what they printed
# ---------------------------------------------------------------------------

def _classpath(work: Path, runner_source: str, components_source: str) -> Path:
    """Compile the in-repo cordis4j stubs + a PlacementRunner source + the
    emitted unit into one classes dir, the way `placement._build_java` does."""
    runner = work / "PlacementRunner.java"
    runner.write_text(runner_source, encoding="utf-8")
    gen = work / "revl"
    gen.mkdir()
    (gen / "Components.java").write_text(components_source, encoding="utf-8")
    out = work / "out"
    out.mkdir()
    javac = [javac_gate.JAVAC, "--release", javac_gate.RELEASE, "-d", str(out)]
    # The runner references `Estop` for the operator E-Stop seam (item 443,
    # issue #122): `placement._build_java` compiles the shipped `Estop.java`
    # beside the runner, so this helper does too.
    estop = str(PLACEMENT / "Estop.java")
    result = subprocess.run(
        javac + [str(s) for s in javac_gate.STUB_SOURCES] + [estop, str(runner)],
        capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        javac + ["-cp", str(out), str(gen / "Components.java")],
        capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr
    return out


def _spawn(classpath: "str | Path", spec_path: Path, *, main: str = "PlacementRunner",
           wal: Path | None = None) -> subprocess.Popen:
    """One runner process. `wal` is the `$REVL_WAL` the emitted `Components`
    records to (unset -> the sink is the no-op the non-record default keeps)."""
    env = None if wal is None else {**os.environ, "REVL_WAL": str(wal)}
    return subprocess.Popen(
        [javac_gate.JAVA, "-cp", str(classpath), main, str(spec_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        stdin=subprocess.DEVNULL, env=env)


def _read_until(proc: subprocess.Popen, marker: str, collected: list[str], timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        collected.append(line)
        if line.strip() == marker:
            return
    raise AssertionError(f"never saw {marker!r}:\n{''.join(collected)}")


def _stop(proc: subprocess.Popen, collected: list[str]) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
    try:
        rest, _ = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        rest, _ = proc.communicate()
    if rest:
        collected.append(rest)


def _wire_reply(socket_path: str) -> str:
    """One raw request over the provider's socket, the reply as the wire carries
    it: the seam-failure text itself, before any consumer decides how to print
    it (a py consumer logs it verbatim; the java probe used to say `null`)."""
    with sockets_mod.socket(sockets_mod.AF_UNIX, sockets_mod.SOCK_STREAM) as conn:
        conn.settimeout(30)
        conn.connect(socket_path)
        conn.sendall((json.dumps({"key": "vault", "method": "open", "args": ["alice"]}) + "\n").encode())
        reply = b""
        while not reply.endswith(b"\n"):
            chunk = conn.recv(65536)
            if not chunk:
                break
            reply += chunk
    return reply.decode("utf-8", "replace")


def _run_seam(classpath: "str | Path", provider_config: dict, *,
              consumer_classpath: "str | Path | None" = None,
              consumer_main: str = "PlacementRunner",
              wal: dict[str, Path] | None = None) -> str:
    """Boot a java provider and a java consumer on the stub runtime, cross the
    seam once through the consumer's probe, tear both down, and return every
    line either printed plus the raw seam reply (prefixed `[wire]`). The same
    spec shape `revl run --placement` writes.

    `consumer_classpath`/`consumer_main` swap the consumer for another compiled
    runner (the reactive twin compiles only against the real cordis4j, so the
    two sides can live on different classpaths — which is also how
    `placement._build_java` picks between them). `wal` maps a side's name to the
    `$REVL_WAL` path it records to.
    """
    consumer_classpath = consumer_classpath or classpath
    wal = wal or {}
    # A Unix socket path is bounded (104 bytes on macOS), so the socket lives
    # under the system tmp dir rather than a pytest tmp_path.
    sockets = Path(tempfile.mkdtemp(prefix="revl-jseam-"))
    socket = str(sockets / "vault.sock")
    ifaces = {"vault": "revl.Components$Vault", "front": "revl.Components$Front"}
    provider_spec = sockets / "provider.json"
    provider_spec.write_text(json.dumps({
        "name": "provider", "module": "revl.Components", "ifaces": ifaces,
        "components": ["Keeper"], "config": {"Keeper": provider_config},
        "serve": {"keys": ["vault"], "socket": socket},
    }))
    consumer_spec = sockets / "consumer.json"
    consumer_spec.write_text(json.dumps({
        "name": "consumer", "module": "revl.Components", "ifaces": ifaces,
        "components": ["Portal"], "proxies": {"vault": {"socket": socket}},
        "probe": ["front.login('alice')"],
    }))
    lines: list[str] = []
    provider = _spawn(classpath, provider_spec, wal=wal.get("provider"))
    try:
        _read_until(provider, "[provider] UP", lines)
        lines.append("[wire] " + _wire_reply(socket))
        consumer = _spawn(consumer_classpath, consumer_spec, main=consumer_main,
                          wal=wal.get("consumer"))
        try:
            _read_until(consumer, "[consumer] UP", lines)
        finally:
            _stop(consumer, lines)
    finally:
        _stop(provider, lines)
    return "".join(lines)


RUNNER_SOURCE = (PLACEMENT / "PlacementRunner.java").read_text()

SECRET_CONFIG = {"url": PUBLIC_URL, "api_key": CANARY}


@pytest.fixture(scope="module")
def emitted() -> str:
    return _emitter().emit(_compile(SCENARIO))


@pytest.fixture(scope="module")
def shipped_classpath(tmp_path_factory, emitted) -> Path:
    if javac_gate.JAVAC is None:
        pytest.skip(javac_gate.NO_JDK)
    return _classpath(tmp_path_factory.mktemp("shipped"), RUNNER_SOURCE, emitted)


@needs_jdk
def test_no_sink_of_a_seam_run_carries_the_secret(shipped_classpath):
    trace = _run_seam(shipped_classpath, SECRET_CONFIG)

    # the run really did the things whose output is under test; without these
    # the absence assertions below would hold vacuously
    assert "[provider] serve" in trace, trace
    assert '[wire] {"ok":false,"error":"RuntimeException: vault refused key' in trace, trace
    assert "probe | front.login('alice')| ERROR RuntimeException:" in trace, trace

    # the credential is nowhere: not the seam reply, not either console
    assert CANARY not in trace, trace
    # ...and its place is marked, so a reader knows what was there
    assert f"vault refused key {REDACTED_SECRET} at {PUBLIC_URL} for {REDACTED_ARG}" in trace, trace


@needs_jdk
def test_the_failure_is_still_worth_reading(shipped_classpath):
    """Redaction touches only what the message SAYS about the secret. The
    exception's type, the sentence around it and the ordinary config value
    survive, and the caller's own argument carries the argument marker, not
    the secret one: a reader can tell which fact each placeholder stands for."""
    trace = _run_seam(shipped_classpath, SECRET_CONFIG)
    assert "RuntimeException: vault refused key" in trace, trace
    assert PUBLIC_URL in trace, trace
    assert REDACTED_ARG in trace, trace
    # the dispatch wrapper is unwrapped: the reply names the provider's failure,
    # not the reflection plumbing around it
    assert "InvocationTargetException" not in trace, trace


@needs_jdk
def test_an_ordinary_config_value_is_not_redacted(shipped_classpath):
    """A registry that redacts everything is worse than none: with no secret
    declared in the config table (the default stands), the ordinary value is
    verbatim and no secret marker appears anywhere."""
    trace = _run_seam(shipped_classpath, {"url": PUBLIC_URL, "api_key": "k"})
    assert PUBLIC_URL in trace, trace
    assert REDACTED_SECRET not in trace, trace
    assert "vault refused key k at" in trace, trace


# The trap: someone improving the diagnostic unwraps MORE of the cause chain.
# This copy of the runner renders every throwable in the chain, message and
# all, which is the most a well-meaning unwrap could reasonably print.
_SHIPPED_TEXT = (
    "        Throwable failure = unwrapDispatch(t);\n"
    "        String text = failure.getClass().getSimpleName() + \": \" + failure.getMessage();\n"
)
_AGGRESSIVE_TEXT = (
    "        StringBuilder chain = new StringBuilder();\n"
    "        for (Throwable cause = t; cause != null; cause = cause.getCause()) {\n"
    "            if (chain.length() > 0) chain.append(\" <- \");\n"
    "            chain.append(cause.getClass().getSimpleName()).append(\": \").append(cause.getMessage());\n"
    "        }\n"
    "        String text = chain.toString();\n"
)


@needs_jdk
def test_unwrapping_the_whole_cause_chain_still_leaks_nothing(tmp_path, emitted):
    assert _SHIPPED_TEXT in RUNNER_SOURCE, "the seam-failure text builder moved; re-pin it"
    aggressive = RUNNER_SOURCE.replace(_SHIPPED_TEXT, _AGGRESSIVE_TEXT)
    classpath = _classpath(tmp_path, aggressive, emitted)
    trace = _run_seam(classpath, SECRET_CONFIG)
    assert "vault refused key" in trace, trace
    assert CANARY not in trace, trace
    assert REDACTED_SECRET in trace, trace


# ---------------------------------------------------------------------------
# non-vacuity: take the registry away and the value appears
# ---------------------------------------------------------------------------

_BIND_HEAD = "    static void bindSecretRegistry(String container) {\n"


@needs_jdk
def test_with_the_runner_unbound_the_secret_appears(tmp_path, emitted):
    """The runner's binding is what closes the seam reply and the console."""
    assert _BIND_HEAD in RUNNER_SOURCE
    unbound = RUNNER_SOURCE.replace(_BIND_HEAD, _BIND_HEAD + "        if (container != null) return;\n")
    classpath = _classpath(tmp_path, unbound, emitted)
    trace = _run_seam(classpath, SECRET_CONFIG)
    assert "vault refused key" in trace, trace
    assert CANARY in trace, trace


@needs_jdk
def test_with_the_registration_stripped_the_secret_appears(tmp_path, emitted):
    """...and the constructor's registration is what gives the funnel a value
    to look for. Without it the funnel runs and finds nothing."""
    unmarked = emitted.replace("            revlMarkSecret(this.api_key);\n", "")
    assert unmarked != emitted
    classpath = _classpath(tmp_path, RUNNER_SOURCE, unmarked)
    trace = _run_seam(classpath, SECRET_CONFIG)
    assert "vault refused key" in trace, trace
    assert CANARY in trace, trace
    assert REDACTED_SECRET not in trace, trace


def test_the_scenario_is_the_legitimate_use():
    """The composition compiles: handing a declared `Secret[T]` config field to
    the component's own host binding is not a disclosure crossing."""
    ir = _compile(SCENARIO)
    keeper = next(c for c in ir["components"] if c["name"] == "Keeper")
    assert [f["name"] for f in keeper["config"] if f.get("secret")] == ["api_key"]


# ---------------------------------------------------------------------------
# item 1 of the audit (#815): the durable WAL, written by a real JVM
#
# The shape test above reads the emitted source and finds the registry on the
# WAL descriptor path. What it cannot say is that the value a composition
# declared secret is absent from the FILE — the WAL is plaintext at rest, and a
# recorded inverse's arguments are part of it, so the file is a sink of its own.
# These two RUN the sink: the repo's own crash producer drives the recorded
# composition on a real JVM and `$REVL_WAL` points at a file we then read.
# ---------------------------------------------------------------------------

CRASHPROOF = BACKEND / "scenarios" / "crashproof"


def _wal_ir(canary: str, *, mark: bool) -> dict:
    """The recorded composition whose inverse's own argument IS the declared
    value: a host call hands back a credential the composition then passes to
    the re-issuable undo call, so the descriptor carries it by construction.
    `mark=False` is the control that makes the declaration absent, not the
    value: the same bytes reach the same sink without a registry to read them
    through."""
    ir = json.loads((CRASHPROOF / "crashproof.ir.json").read_text(encoding="utf-8"))
    if mark:
        ir["components"][0]["config"] = [
            {"name": "api_key", "type": "Str", "default": canary, "secret": True}]
    ir["externs"][0]["bodies"]["java"] = f'\n    return new RevlResult.Ok<>("{canary}");\n'
    return ir


def _wal_classpath(work: Path, components_source: str) -> Path:
    """The stub runtime + the recorded unit + the repo's crash producer, the
    classpath `tests/test_java_crash_recovery.py` compiles for the same sink."""
    gen = work / "revl"
    gen.mkdir(parents=True)
    (gen / "Components.java").write_text(components_source, encoding="utf-8")
    out = work / "out"
    out.mkdir()
    result = subprocess.run(
        [javac_gate.JAVAC, "--release", javac_gate.RELEASE, "-d", str(out)]
        + [str(s) for s in javac_gate.STUB_SOURCES]
        + [str(gen / "Components.java"), str(CRASHPROOF / "CrashProducer.java")],
        capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr
    return out


def _recorded_wal(classpath: Path, wal: Path) -> str:
    """Activate -> dispose -> discharge: the clean run, so the descriptor is on
    disk and the file we read is the one the sink wrote."""
    result = subprocess.run(
        [javac_gate.JAVA, "-cp", str(classpath), "CrashProducer"],
        env={"REVL_WAL": str(wal), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
    assert wal.exists(), "the run wrote no WAL at all"
    return wal.read_text(encoding="utf-8")


@needs_jdk
def test_the_wal_a_live_run_writes_carries_no_declared_secret(tmp_path):
    ir = _wal_ir(CANARY, mark=True)
    recorded = javac_gate.compile_check(_emitter().emit(ir, record=True), "wal + secret")
    text = _recorded_wal(_wal_classpath(tmp_path / "marked", recorded), tmp_path / "marked.wal")
    # the record really is there (a silently dead sink would pass the rest)
    assert '"record":"discharge-descriptor"' in text, text
    assert '"method":"delete_row"' in text, text
    # ...and the value the composition declared secret is not in the file
    assert CANARY not in text, text
    assert f'"args":["{REDACTED_SECRET}"]' in text, text


@needs_jdk
def test_without_the_declaration_the_same_run_writes_it(tmp_path):
    """Non-vacuity: the scrub is the registry's, not the shape of the value or
    a sink that never saw it. The identical run without the declaration records
    the identical call with the value verbatim."""
    recorded = javac_gate.compile_check(
        _emitter().emit(_wal_ir(CANARY, mark=False), record=True), "wal control")
    text = _recorded_wal(_wal_classpath(tmp_path / "unmarked", recorded), tmp_path / "unmarked.wal")
    assert f'"args":["{CANARY}"]' in text, text


# ---------------------------------------------------------------------------
# item 4 of the audit (#815): the reactive twin, in secret mode
#
# `RealPlacementRunner` is a second `main` with its own printer, and everything
# above compiles the JDK-17 stub runner instead. The twin is the cordis4j
# composition path (`placement._build_java` prefers it whenever
# `REVL_CORDIS4J_CLASSES` is set), so its console and its probe path need the
# same treatment: the twin binds the emitted registry before its first line and
# funnels its printers through it, and this runs that binding rather than
# grepping for it.
# ---------------------------------------------------------------------------

CORDIS4J = os.environ.get("REVL_CORDIS4J_CLASSES")
NO_CORDIS4J = ("the reactive runner compiles only against the real cordis4j "
               "(REVL_CORDIS4J_CLASSES); CI's backend-java job provisions it")
needs_cordis4j = pytest.mark.skipif(not CORDIS4J, reason=NO_CORDIS4J)


def _twin_classpath(work: Path, components_source: str) -> str:
    """The shipped reactive runner + its Estop seam against the REAL cordis4j,
    plus the emitted unit — and both of those on the RUN classpath too, since
    `Contexts` is resolved from the runtime at boot. The in-repo stubs are
    deliberately absent: the twin's whole point is the cordis4j API they only
    approximate."""
    gen = work / "revl"
    gen.mkdir(parents=True)
    (gen / "Components.java").write_text(components_source, encoding="utf-8")
    out = work / "out"
    out.mkdir()
    javac = [javac_gate.JAVAC, "--release", javac_gate.RELEASE, "-d", str(out),
             "-cp", str(CORDIS4J)]
    result = subprocess.run(
        javac + [str(PLACEMENT / "Estop.java"), str(PLACEMENT / "RealPlacementRunner.java")],
        capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        javac + ["-cp", f"{CORDIS4J}{os.pathsep}{out}", str(gen / "Components.java")],
        capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr
    return f"{out}{os.pathsep}{CORDIS4J}"


@pytest.fixture(scope="module")
def reactive_classpath(tmp_path_factory, emitted) -> str:
    if javac_gate.JAVAC is None:
        pytest.skip(javac_gate.NO_JDK)
    if not CORDIS4J:
        pytest.skip(NO_CORDIS4J)
    return _twin_classpath(tmp_path_factory.mktemp("reactive"), emitted)


def _run_twin_spec(classpath: str, spec: dict) -> str:
    """One twin process, one spec: boot, read to UP, stop cleanly, return every
    line it printed."""
    work = Path(tempfile.mkdtemp(prefix="revl-jtwin-"))
    spec_path = work / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    lines: list[str] = []
    proc = _spawn(classpath, spec_path, main="RealPlacementRunner")
    try:
        _read_until(proc, "[consumer] UP", lines)
    finally:
        _stop(proc, lines)
    return "".join(lines)


# A consumer that holds a declared secret of its OWN and probes a method whose
# host binding fails quoting it: the failure is local, so what the twin prints
# is the twin's own registry's business and nothing the provider did.
TWIN_LOCAL_PROBE = {
    "name": "consumer", "module": "revl.Components",
    "ifaces": {"vault": "revl.Components$Vault"},
    "components": ["Keeper"], "config": {"Keeper": SECRET_CONFIG},
    "probe": ["vault.open('alice')"],
}


@needs_jdk
@needs_cordis4j
def test_the_reactive_twin_keeps_the_secret_out_of_its_probe_path(shipped_classpath,
                                                                  reactive_classpath):
    """The consumer here is the twin, not the stub: the reactive inject branch
    and its probe window are the twin's own code, and no other test in this
    file compiles or runs them. What this CANNOT say is that the twin's funnel
    redacted anything — the provider scrubs the reply before it leaves — so the
    two tests below run the twin against a sink it alone owns."""
    trace = _run_seam(shipped_classpath, SECRET_CONFIG,
                      consumer_classpath=reactive_classpath,
                      consumer_main="RealPlacementRunner")
    # the twin really booted, loaded the component reactively and crossed once
    assert "load" in trace and "ACTIVE (reactive)" in trace, trace
    assert "front.login('alice')" in trace, trace
    assert "ERROR RuntimeException:" in trace, trace
    assert CANARY not in trace, trace
    assert f"vault refused key {REDACTED_SECRET} at {PUBLIC_URL} for {REDACTED_ARG}" in trace, trace


@needs_jdk
@needs_cordis4j
def test_the_reactive_twin_scrubs_the_secret_it_holds_itself(reactive_classpath):
    trace = _run_twin_spec(reactive_classpath, TWIN_LOCAL_PROBE)
    assert "vault.open('alice')" in trace, trace
    assert "ERROR RuntimeException:" in trace, trace
    assert CANARY not in trace, trace
    assert f"vault refused key {REDACTED_SECRET} at {PUBLIC_URL} for alice" in trace, trace


@needs_jdk
@needs_cordis4j
def test_the_reactive_twin_leaks_it_once_the_registration_is_stripped(tmp_path, emitted):
    """Non-vacuity: the twin's binding is what stands between its probe window
    and the value. Strip the registration and the same run prints it."""
    unmarked = emitted.replace("            revlMarkSecret(this.api_key);\n", "")
    assert unmarked != emitted
    classpath = _twin_classpath(tmp_path, unmarked)
    trace = _run_twin_spec(classpath, TWIN_LOCAL_PROBE)
    assert "vault refused key" in trace, trace
    assert CANARY in trace, trace
    assert REDACTED_SECRET not in trace, trace
