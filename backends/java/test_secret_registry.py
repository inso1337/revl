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
    result = subprocess.run(
        javac + [str(s) for s in javac_gate.STUB_SOURCES] + [str(runner)],
        capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        javac + ["-cp", str(out), str(gen / "Components.java")],
        capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr
    return out


def _spawn(classpath: Path, spec_path: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [javac_gate.JAVA, "-cp", str(classpath), "PlacementRunner", str(spec_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        stdin=subprocess.DEVNULL)


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


def _run_seam(classpath: Path, provider_config: dict) -> str:
    """Boot a java provider and a java consumer on the stub runtime, cross the
    seam once through the consumer's probe, tear both down, and return every
    line either printed plus the raw seam reply (prefixed `[wire]`). The same
    spec shape `revl run --placement` writes."""
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
    provider = _spawn(classpath, provider_spec)
    try:
        _read_until(provider, "[provider] UP", lines)
        lines.append("[wire] " + _wire_reply(socket))
        consumer = _spawn(classpath, consumer_spec)
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
