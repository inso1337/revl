"""The go placement runner's uncaught-failure channel sits behind the redaction
funnel (issue #814, the go half of advisory GHSA-4x4q-296m-9x9x L1).

`log()` funnels every line this process prints (item 421 F5) and
`bridge.SeamFailure` funnels the reply it sends back across the seam, but the
failures those two expect are not the only ones a boot can raise: a config
hook, a serve setup, an inverse during teardown — anything the load path and
the probe path do not catch — used to escape as a PANIC, and the runtime prints
a panic value plus a goroutine stack trace straight to stderr, unfunnelled. The
conductor merges that verbatim (`placement.py::pump` spawns children with
`stderr=subprocess.STDOUT`), and the panic value quotes whatever the failing
frame held. The go tier has no second chance to redact it: `log()` is called by
the code that runs, and this is the code that did not get to run.

The py tier closed this in the same issue (`_process_runner.main`'s catch-all),
the java tier with a `main` wrapper, and the go tier with a `recover` in a defer
registered FIRST in `main` — registered first so it runs LAST, after the
teardown's `cancel`, and exiting rather than re-panicking so the teardown that
prints `DOWN` is never reached.

WHAT IS COVERED HERE, CHANNEL BY CHANNEL. Each row names the site in
`placement_runner/main.go` that would have to be un-wired for that test to
fail, except the shape tests, which pin a property rather than a redaction:

  the catch-all        test_main_wraps_its_body_in_a_recover_registered_first
                       delete the defer -> FAIL
  the FATAL line       test_the_fatal_line_is_printed_to_stderr_through_the_funnel
                       un-funnel the `bridge.ScrubText` call -> FAIL
  `os.Exit`, not a     test_the_fatal_path_exits_instead_of_unwinding_into_teardown
  re-panic             re-panic -> FAIL (the deferred teardown would run and
                       print `DOWN`, the conductor's clean-teardown signal, for
                       a process that died mid-boot — E7)
  the spec sinks       test_the_spec_load_sinks_are_funnelled
                       a json error quotes the spec text it choked on, and the
                       spec carries this process's config values — a declared
                       `Secret[T]` among them
  the whole channel    test_an_uncaught_probe_failure_prints_one_redacted_line
                       live binary: the real emitted registry, the real runner
  non-vacuity          test_with_the_fatal_funnel_unwired_the_value_appears
                       the same run with `bridge.ScrubText(...)` -> plain
                       `fmt.Sprint(...)` DOES print the value
  non-vacuity (2)      test_without_the_recover_the_runtime_prints_the_value
                       the same run with the defer deleted leaks the value the
                       way the runtime prints a panic: unfunnelled
  the OTHER            test_every_goroutine_that_can_hold_a_value_carries_its_own_recover
  goroutines           the E-Stop watcher is not main's goroutine, and `recover`
                       covers only the goroutine that runs it: Go has no
                       process-wide panic hook. The crossing goroutine recovers
                       in `bridge.Serve` instead (issue #2481)
  the guard is the     test_the_guard_is_the_deferred_function_itself
  deferred function    a guard that is a plain helper calling `recover` recovers
                       nothing: `defer guard(name)` with no trailing `()` defers
                       the call that BUILDS the closure, so the closure never
                       runs and the panic escapes anyway. The label is read when
                       the failure happens, not when the defer is registered, so
                       a post-boot panic is labelled with the spec's name.
  the crossing channel test_a_panic_in_a_crossing_comes_back_as_one_redacted_error_reply
                       live binary: a provider method panicking on the value it
                       holds, reached OVER THE WIRE. Issue #2481: the caller
                       gets an error reply through `bridge.SeamFailure` and the
                       process keeps serving, as a py, ts or java provider that
                       raises does
  non-vacuity (3)      test_without_the_crossing_recover_the_runtime_prints_the_value
                       the same crossing with `bridge.Serve`'s recover deleted
                       leaks the value the way the runtime prints a panic

The live tests need a go toolchain and are skipped without one, exactly like the
neighbouring item-421 files. Every assertion is PAIRED (canary absent AND marker
present) so none can pass on an empty line.
"""

from __future__ import annotations

import json
import os
import selectors
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source, placement  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("go") is None,
                                reason="no go toolchain on PATH")

MAIN = ROOT / "backends" / "go" / "placement_runner" / "main.go"
_RUNNER = ROOT / "backends" / "go" / "placement_runner"
BRIDGE = _RUNNER / "bridge" / "bridge.go"

CANARY = "SEKRIT-CANARY-814-GO"
REDACTED_SECRET = "<redacted:secret>"

# A component holding a credential as a declared secret, whose provide method
# hands it to a host emission that PANICS. The panic value is what a failing
# frame would interpolate — a driver message, a connection string — and it is
# dispatched in-process by the probe, so it is the main goroutine that unwinds.
# The service method is marked `emission` because the body reaches a host
# emission; the method itself is a plain `fn`, since emission-ness is inherited
# from the service declaration (G4 upper bound).
_PANIC_DOC = f'''
service Vault {{ emission fn show(user: Str) -> Str }}

component Impl provides vault: Vault {{
  config {{ token: Secret[Str] = "{CANARY}" }}

  provide vault {{
    fn show(user) {{ emit boom(config.token); return "shown " + user }}
  }}
}}

extern emission fn boom(key: Secret[Str]) -> Unit = @go {{
	panic("boom " + key)
}}
'''

_SPEC = {
    "name": "vault", "backend": "go", "components": ["Impl"], "config": {},
    "provides": ["vault"], "proxies": {},
    "probe": [{"key": "vault", "method": "show", "args": ["u1"]}],
    "once": True,
}

# The edits the non-vacuity arms make, spelled exactly as main.go spells them.
# `_RECOVER_DEFER` is main's own defer and `_RECOVER_CROSSING` the recover in
# `bridge.serveConn` that turns a handler's panic into its error reply (the
# E-Stop watcher's guard is asserted in place by `test_every_goroutine_...`,
# which slices its block rather than replacing the text).
_RECOVER_DEFER = '\tdefer guard(&name)()\n'
_RECOVER_CROSSING = (
    '\t\t\t\t\tdefer func() {\n'
    '\t\t\t\t\t\tif r := recover(); r != nil {\n'
    '\t\t\t\t\t\t\tvalue, ierr = nil, fmt.Errorf("panic: %v", r)\n'
    '\t\t\t\t\t\t}\n'
    '\t\t\t\t\t}()\n'
)
_FATAL_PRINT = (
    '\tfmt.Fprintln(os.Stderr, "["+name+"] FATAL "+bridge.ScrubText(fmt.Sprint(fatal)))\n'
)


def _build(work: Path, *, replace: tuple[str, str] | None = None,
           doc: str = _PANIC_DOC, edit: str = "main.go") -> str:
    """Build the runner + the generated `emitted` package for `_PANIC_DOC` in a
    COPY of the runner dir, so a test can edit a runner source (`edit`, relative
    to the runner dir) before building.

    `placement._build_go` writes the generated package into the runner it builds
    against, so pointing it at a copy is also what keeps these tests from
    leaving `emitted/gen.go` in the tree.
    """
    runner = work / "placement_runner"
    shutil.copytree(_RUNNER, runner,
                    ignore=shutil.ignore_patterns("emitted", "revl_placement_runner"))
    if replace is not None:
        old, new = replace
        text = (runner / edit).read_text(encoding="utf-8")
        assert old in text, old
        (runner / edit).write_text(text.replace(old, new), encoding="utf-8")
    saved = placement._GO_RUNNER
    placement._GO_RUNNER = runner
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return placement._build_go(compile_source(doc), work)
    finally:
        placement._GO_RUNNER = saved


def _run_probe(binary: str) -> tuple[int, str, str]:
    """Run one probe against a placement whose provide method panics on the
    value it holds as a declared secret. Returns (exit code, stdout, stderr)."""
    spec = Path(binary).parent / "spec.json"
    spec.write_text(json.dumps(_SPEC))
    done = subprocess.run([binary, str(spec)], capture_output=True, text=True,
                          timeout=300, stdin=subprocess.DEVNULL)
    return done.returncode, done.stdout, done.stderr


# The same document, driven OVER THE WIRE instead of in-process. `once: False`
# so the runner is still holding when the client connects, and `probe` empty so
# nothing runs before the crossing arrives.
_SPEC_SERVING = {
    "name": "vault", "backend": "go", "components": ["Impl"], "config": {},
    "provides": ["vault"], "proxies": {}, "probe": [],
    "serve": {"socket": "", "keys": ["vault"], "methods": {"vault": ["show"]}},
    "once": False,
}


def _cross(binary: str) -> tuple[int, str, str, str]:
    """Serve, wait for `UP`, then drive ONE crossing whose handler panics on the
    value it holds as a declared secret. Returns (exit code, stdout, stderr,
    the raw reply line, empty when none arrived).

    The panic is raised inside `bridge.Serve`'s per-connection goroutine, which
    is the point: main's recover cannot see it, so without the bridge's own
    recover the runtime prints the panic value verbatim. Stdin is held OPEN for
    the same reason `once` is off — the runner treats stdin EOF as the stop
    signal and would tear down before the client got there.

    Both streams are drained by reader threads and the rendezvous is polled with
    a deadline rather than a blocking `readline`: the boot path prints its load
    lines BEFORE `UP`, and reading a live process's stderr blocks until it
    exits, so either of the obvious shortcuts hangs instead of failing.
    """
    # AF_UNIX paths cap at ~104 bytes on macOS, well under pytest's tmp_path, so
    # the socket gets a directory of its own.
    sockdir = tempfile.mkdtemp(prefix="rvlgo_cross_", dir="/tmp")
    sock = str(Path(sockdir) / "v.sock")
    spec = Path(binary).parent / "serving.json"
    spec.write_text(json.dumps(
        {**_SPEC_SERVING, "serve": {**_SPEC_SERVING["serve"], "socket": sock}}))
    proc = subprocess.Popen([binary, str(spec)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            stdin=subprocess.PIPE)
    lines: dict[str, list[str]] = {"out": [], "err": []}
    reply = b""

    def pump(key: str, stream) -> None:
        for line in stream:
            lines[key].append(line)

    readers = [threading.Thread(target=pump, args=(key, stream), daemon=True)
               for key, stream in (("out", proc.stdout), ("err", proc.stderr))]
    for reader in readers:
        reader.start()
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if any("UP" in line for line in lines["out"]) or proc.poll() is not None:
                break
            time.sleep(0.05)
        assert any("UP" in line for line in lines["out"]), (
            "the runner never came up", "".join(lines["out"]), "".join(lines["err"]))

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(sock)
            client.settimeout(120)
            client.sendall(json.dumps(
                {"key": "vault", "method": "show", "args": ["u1"]}).encode() + b"\n")
            # Wait for the crossing to LAND before letting go of stdin. The
            # runner treats stdin EOF as its stop signal, so closing it here
            # would race the crossing: the teardown's `DOWN` and a clean exit 0
            # can beat the connection goroutine to the panic, and the arm would
            # then fail for a reason that has nothing to do with the recover.
            # Either the reply arrives (the recover answered the crossing) or
            # the process dies (it did not), and the assertions read which.
            #
            # `selectors`, not `select.select`: this file runs inside the whole
            # `tests/` root, where the process's open descriptors run past
            # FD_SETSIZE (1024) and `select` raises `filedescriptor out of range
            # in select()` on a socket that happens to land above it. The
            # default selector is kqueue/epoll, which has no such bound, so the
            # arm reports on the funnel rather than on how many files the run
            # before it left open.
            deadline = time.monotonic() + 120
            with selectors.DefaultSelector() as sel:
                sel.register(client, selectors.EVENT_READ)
                while time.monotonic() < deadline and proc.poll() is None:
                    if sel.select(0.05):
                        break
                while proc.poll() is None and not reply.endswith(b"\n"):
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    reply += chunk
        try:
            proc.stdin.close()  # the stop signal, if the process is still serving
        except OSError:  # pragma: no cover - the process already died
            pass
        proc.wait(timeout=120)
    finally:
        if proc.poll() is None:
            proc.kill()
        for reader in readers:
            reader.join(timeout=10)
        shutil.rmtree(sockdir, ignore_errors=True)
    return (proc.returncode, "".join(lines["out"]), "".join(lines["err"]),
            reply.decode("utf-8", "replace"))


# ---------------------------------------------------------------------------
# the shape (runs everywhere)
# ---------------------------------------------------------------------------

def test_main_wraps_its_body_in_a_recover_registered_first():
    """`recover` only sees a panic if the deferred function is registered before
    it, and the funnel has to cover the WHOLE body: the spec read, the parse,
    the load, the serve setup, the probes and the teardown are all inside it.
    Registering it first is also what makes it run last — after the teardown's
    `cancel`."""
    source = MAIN.read_text(encoding="utf-8")
    body = source[source.index("func main() {"):]
    assert _RECOVER_DEFER in body
    # nothing may `os.Exit` before the defer is registered, or that path escapes
    # the funnel by construction.
    assert body.index("defer guard(&name)()") < body.index("len(os.Args) < 2")
    assert body.index("defer guard(&name)()") < body.index("os.ReadFile(")
    # `name` is hoisted above the defer and defaulted, so a boot that fails
    # before the spec is parsed (an unreadable spec, a spec without `name`)
    # still prints a label rather than an empty bracket.
    assert '\tname := "proc"\n' in body
    assert body.index('name := "proc"') < body.index("defer guard(&name)()")
    assert '\tif s.Name != "" {\n\t\tname = s.Name\n\t}\n' in body


def test_the_fatal_line_is_printed_to_stderr_through_the_funnel():
    """One redacted line on stderr — the channel the conductor merges into the
    interleaved trace — carrying the same `[name] FATAL …` shape the py and java
    tiers print, and routed through the registry's scrub rather than around it.
    The panic value is an `any`, so it is rendered before it is scrubbed."""
    source = MAIN.read_text(encoding="utf-8")
    assert _FATAL_PRINT in source
    assert "bridge.ScrubText(fmt.Sprint(fatal))" in source
    # the line is NOT routed through `log()`: the funnel covers the paths that
    # never reach a caller of `log`, so it cannot depend on one.
    line = source[source.index("func fatalLine("):]
    line = line[:line.index("\n}\n")]
    assert "log(" not in line
    # and there is exactly ONE composer of the line, so a second channel cannot
    # be added that quietly forgets the funnel.
    assert source.count('fmt.Fprintln(os.Stderr, "["+name+"] FATAL "') == 1


def test_the_fatal_path_exits_instead_of_unwinding_into_teardown():
    """`os.Exit`, not a re-panic: main's later defers would run on the way out
    and the teardown prints `DOWN`, which is what the conductor reads as a clean
    teardown. A process that died mid-boot must not claim one — the same "die
    where it stands, non-zero, no DOWN" rule the E-Stop watcher follows (E7)."""
    source = MAIN.read_text(encoding="utf-8")
    guard = source[source.index("func guard("):]
    guard = guard[:guard.index("\n}\n")]
    assert "\t\tos.Exit(1)\n" in guard
    assert "panic(" not in guard
    # ...and the funnel does not swallow a healthy return: recovering `nil` is
    # the ordinary path and must leave the exit code alone.
    assert "if fatal := recover(); fatal != nil {" in guard


def test_every_goroutine_that_can_hold_a_value_carries_its_own_recover():
    """Go has no process-wide panic hook, and `recover` covers only the goroutine
    that runs it — main's defer is a fence around main's own frame. Two other
    goroutines reach into value-carrying code, and the runtime prints a panic it
    reaches the top of VERBATIM on stderr, stack trace and all.

    The crossing goroutine is the one that matters most: its frame holds
    `args []json.RawMessage`, the caller's arguments, which is exactly where a
    declared `Secret[T]` travels when a provider takes one. Since issue #2481 it
    is covered by `bridge.serveConn`'s recover, which answers the crossing with
    an error reply, and NOT by a guard: a guard deferred in the handler would
    run first and halt the process."""
    source = MAIN.read_text(encoding="utf-8")

    serve = source[source.index("go bridge.Serve(ln, func("):]
    serve = serve[:serve.index("})")]
    assert "guard(" not in serve
    # the recover sits in the bracket that invokes the handler, registered
    # before the call, or the panic is already past it
    bridge = BRIDGE.read_text(encoding="utf-8")
    assert bridge.count(_RECOVER_CROSSING) == 1
    assert (bridge.index(_RECOVER_CROSSING)
            < bridge.index("return invoke(req.Key, req.Method, req.Args)"))

    # the E-Stop watcher parses the latch file and composes the HALTED inventory
    # off the main goroutine, so its escapes reach no funnel either.
    latch = source[source.index('if s.EstopLatch != "" {'):]
    latch = latch[:latch.index("time.Sleep(20 * time.Millisecond)")]
    assert "defer guard(&name)()" in latch
    assert latch.index("defer guard(&name)()") < latch.index("estop.ReadLatch(")

    # and every guard routes through the one composer, not a hand-rolled line.
    assert source.count("func guard(name *string) func() {") == 1
    assert source.count("\tdefer guard(&name)()") == 2


def test_the_guard_is_the_deferred_function_itself():
    """`recover` stops a panic only when the DEFERRED FUNCTION calls it
    directly: a defer that calls a helper which calls `recover` recovers
    nothing, and the panic then reaches the top of the goroutine and is printed
    verbatim — the exact hole this guard closes, reintroduced by a refactor that
    looks harmless. So the guard RETURNS the deferred closure and every call
    site defers its result."""
    source = MAIN.read_text(encoding="utf-8")
    guard = source[source.index("func guard("):]
    guard = guard[:guard.index("\n}\n")]
    assert "func guard(name *string) func() {" in guard
    assert "\treturn func() {\n" in guard
    assert "recover()" in guard
    assert "fatalLine(*name, fatal)" in guard
    # the shape that silently recovers nothing: `defer guard(name)` defers the
    # call that BUILDS the closure, and the closure never runs.
    assert "\tdefer guard(name)" not in source
    assert "\tdefer func() { guard(name) }()" not in source


def test_the_spec_load_sinks_are_funnelled():
    """These two run before any component has loaded, but `bridge.ScrubText` is
    already live: the emitted package installs the scrub in an `init`, and
    `emitted` imports `bridge`, so the hook is in force from the first statement
    of main. `parse spec` is the one that needs it — a json error quotes the
    spec text it choked on, and the spec carries this process's config values, a
    declared `Secret[T]` among them."""
    source = MAIN.read_text(encoding="utf-8")
    assert '\t\tfmt.Fprintln(os.Stderr, bridge.ScrubText("read spec: "+err.Error()))\n' in source
    assert '\t\tfmt.Fprintln(os.Stderr, bridge.ScrubText("parse spec: "+err.Error()))\n' in source


# ---------------------------------------------------------------------------
# the live channel: one binary, the real registry, the real runner
# ---------------------------------------------------------------------------

def test_an_uncaught_probe_failure_prints_one_redacted_line(tmp_path):
    code, out, err = _run_probe(_build(tmp_path))

    # the run really panicked, and the panic really quoted the registered value:
    # without this the absence assertions below would be vacuous. The label is
    # `vault`, the spec's name, not the `proc` main defaulted before parsing it:
    # the guard reads the label when the failure happens.
    assert code == 1, (out, err)
    assert err.count("FATAL") == 1, err
    assert "boom" in err, err
    assert REDACTED_SECRET in err, err
    assert err.strip() == f"[vault] FATAL boom {REDACTED_SECRET}"

    # the credential is nowhere on either stream, and the runtime's own panic
    # rendering — the stack trace this funnel replaced — is gone with it.
    assert CANARY not in out + err, (out, err)
    assert "goroutine " not in err, err

    # a process that died mid-boot printed no protocol line: no `DOWN` it did
    # not earn. The load line before the panic is the control that the ordinary
    # channel is untouched.
    assert "DOWN" not in out, out
    assert "NO-RESIDUE" not in out, out
    assert "load" in out, out


def test_with_the_fatal_funnel_unwired_the_value_appears(tmp_path):
    """The non-vacuity arm: the same run, one edit to the funnel."""
    binary = _build(tmp_path, replace=(
        "bridge.ScrubText(fmt.Sprint(fatal))", "fmt.Sprint(fatal)"))
    code, out, err = _run_probe(binary)

    assert code == 1, (out, err)
    assert CANARY in err, err
    assert REDACTED_SECRET not in err, err


def test_without_the_recover_the_runtime_prints_the_value(tmp_path):
    """The non-vacuity arm for the channel itself: with the defer deleted the
    panic is the runtime's, and its rendering is the unfunnelled stack trace
    this funnel exists to replace."""
    binary = _build(tmp_path, replace=(_RECOVER_DEFER, ""))
    code, out, err = _run_probe(binary)

    assert code == 2, (out, err)
    assert CANARY in err, err
    assert "goroutine " in err, err
    assert "FATAL" not in err, err


def test_a_panic_in_a_crossing_comes_back_as_one_redacted_error_reply(tmp_path):
    """The channel main's recover cannot reach. `bridge.Serve` answers each
    connection on a goroutine of its own, and the frame that goroutine runs the
    crossing in holds `args []json.RawMessage` — the caller's arguments, which
    is where a declared `Secret[T]` travels when a provider takes one.

    Issue #2481: the caller gets the failure as an ordinary error reply, the
    shape a py (`bridge._invoke`), ts (`serve`) or java provider that raises
    sends, one line with no stack, scrubbed by `bridge.SeamFailure`. The process
    keeps serving and still tears down cleanly."""
    code, out, err, reply = _cross(_build(tmp_path))

    assert json.loads(reply) == {"ok": False, "error": f"panic: boom {REDACTED_SECRET}"}
    assert CANARY not in out + err + reply, (out, err, reply)
    assert "goroutine " not in err + reply, (err, reply)
    assert "FATAL" not in err, err

    # the crossing failed, the process did not: it was serving (`UP`) and it
    # earned its clean-teardown line when stdin closed.
    assert code == 0, (out, err)
    assert "UP" in out, out
    assert "DOWN" in out, out


def test_without_the_crossing_recover_the_runtime_prints_the_value(tmp_path):
    """The non-vacuity arm for the crossing goroutine: the same crossing with
    `bridge.Serve`'s recover deleted, so the panic is the runtime's and its
    rendering is the unfunnelled stack trace that recover exists to replace."""
    # `recover()` -> `any(nil)` rather than deleting the block, so `fmt` stays
    # used and the edited bridge still compiles: the arm then fails for the
    # recover's absence alone.
    disarmed = _RECOVER_CROSSING.replace("recover()", "any(nil)")
    binary = _build(tmp_path, replace=(_RECOVER_CROSSING, disarmed),
                    edit="bridge/bridge.go")
    code, out, err, reply = _cross(binary)

    assert code == 2, (out, err)
    assert CANARY in err, err
    assert "goroutine " in err, err
    assert "FATAL" not in err, err
    assert reply == "", reply


# ---------------------------------------------------------------------------
# an unknown component is fatal (issue #2223, the go half of #2200)
# ---------------------------------------------------------------------------

_UNDO_DOC = '''
service Vault { fn show(user: Str) -> Str }

extern pure fn mark(what: Str) -> Str = @go {
	println("MARK " + what)
	return what
}

component Impl provides vault: Vault {
  let slot = effect mark("applied") undo mark("INVERSE-RAN")

  provide vault {
    fn show(user) { return "shown " + user }
  }
}
'''


def test_an_unknown_component_is_fatal_and_disposes_what_booted(tmp_path):
    """The spec names `Ghost`, which the binary lacks, after `Impl`, which it
    has. The run must exit non-zero with a message naming `Ghost`, claim neither
    `UP` nor `NO-RESIDUE` nor `DOWN`, and have really torn `Impl` down: its
    inverse ran before the exit. `Fiber.Dispose` only requests the teardown, so
    the log line alone proves nothing; the inverse marker does."""
    binary = _build(tmp_path, doc=_UNDO_DOC)
    spec = tmp_path / "ghost.json"
    spec.write_text(json.dumps({**_SPEC, "components": ["Impl", "Ghost"], "probe": []}))
    done = subprocess.run([binary, str(spec)], capture_output=True, text=True,
                          timeout=300, stdin=subprocess.DEVNULL)
    assert done.returncode != 0, (done.stdout, done.stderr)
    assert "Ghost" in done.stderr and "FATAL" in done.stderr, done.stderr
    for claim in ("UP", "NO-RESIDUE", "DOWN"):
        assert f"] {claim}" not in done.stdout, done.stdout
    out = done.stdout + done.stderr
    assert "MARK applied" in out, out
    assert "MARK INVERSE-RAN" in out, out
    assert out.index("MARK applied") < out.index("MARK INVERSE-RAN"), out


# ---------------------------------------------------------------------------
# an unsupported proxy and a load error are fatal too (issue #2254)
# ---------------------------------------------------------------------------

def _assert_fatal_boot(done, names: str) -> None:
    """The shared verdict of a fatal boot: non-zero exit, one FATAL line on
    stderr naming *names*, and no `UP` / `NO-RESIDUE` / `DOWN` claim."""
    assert done.returncode != 0, (done.stdout, done.stderr)
    assert "FATAL" in done.stderr and names in done.stderr, done.stderr
    for claim in ("UP", "NO-RESIDUE", "DOWN"):
        assert f"] {claim}" not in done.stdout, done.stdout


def test_an_unsupported_proxy_service_is_fatal(tmp_path):
    """The spec asks for a proxy the binary has no proxy component for. The
    proxy loop runs before any component loads, so the run must stop there:
    exit non-zero naming the service, and never load `Impl` (its effect never
    applies). Before #2254 it logged `UNSUPPORTED`, booted `Impl` and exited 0
    with `UP` and `NO-RESIDUE`. The proxy socket really listens, so the arm
    reaches the service check rather than stopping at the dial."""
    binary = _build(tmp_path, doc=_UNDO_DOC)
    sockdir = tempfile.mkdtemp(prefix="rvlgo_2254_", dir="/tmp")
    sock = str(Path(sockdir) / "g.sock")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(sock)
            server.listen(1)
            spec = tmp_path / "ghost-proxy.json"
            spec.write_text(json.dumps({
                **_SPEC, "probe": [],
                "proxies": {"ghost": {"socket": sock, "service": "GhostSvc"}}}))
            done = subprocess.run([binary, str(spec)], capture_output=True,
                                  text=True, timeout=300, stdin=subprocess.DEVNULL)
    finally:
        shutil.rmtree(sockdir, ignore_errors=True)
    _assert_fatal_boot(done, "GhostSvc")
    assert "MARK applied" not in done.stdout + done.stderr, done.stdout


_LOAD_FAIL_DOC = _UNDO_DOC + '''
component Bad requires vault: Vault {
  fail "activation refused"
}
'''


def test_a_component_load_error_is_fatal_and_disposes_what_booted(tmp_path):
    """`Impl` boots, then `Bad`'s activation fails. Outside `--once` there is no
    teardown-and-prove round-trip to salvage, so, as in the rust runner, the
    boot is fatal: exit non-zero naming `Bad`, no `UP` / `NO-RESIDUE` / `DOWN`,
    and `Impl` really torn down (its inverse ran before the exit). Before #2254
    the error was logged, the process said `UP` and exited 0 on stdin EOF."""
    binary = _build(tmp_path, doc=_LOAD_FAIL_DOC)
    spec = tmp_path / "bad.json"
    spec.write_text(json.dumps({**_SPEC, "components": ["Impl", "Bad"],
                                "probe": [], "once": False}))
    done = subprocess.run([binary, str(spec)], capture_output=True, text=True,
                          timeout=300, stdin=subprocess.DEVNULL)
    _assert_fatal_boot(done, "Bad")
    out = done.stdout + done.stderr
    assert "MARK applied" in out, out
    assert "MARK INVERSE-RAN" in out, out
    assert out.index("MARK applied") < out.index("MARK INVERSE-RAN"), out


@pytest.mark.parametrize("once", [False, True], ids=["held", "once"])
def test_a_failed_proxy_dial_is_fatal(tmp_path, once):
    """The spec's proxy socket has no listener, so the proxy can never answer.
    In both modes, as with the rust runner's proxy unwrap, the boot stops:
    exit non-zero naming the key, and `Impl` never loads. `--once` gets no
    exception here: the fault sweep never targets a proxy, and a dead provider
    must not read as `UP` / `NO-RESIDUE` / exit 0. Before #2254 it logged
    `dial failed`, booted `Impl` and exited 0."""
    binary = _build(tmp_path, doc=_UNDO_DOC)
    sockdir = tempfile.mkdtemp(prefix="rvlgo_2254_", dir="/tmp")
    try:
        spec = tmp_path / "dead-proxy.json"
        spec.write_text(json.dumps({
            **_SPEC, "probe": [], "once": once,
            "proxies": {"ghost": {"socket": str(Path(sockdir) / "none.sock"),
                                  "service": "GhostSvc"}}}))
        done = subprocess.run([binary, str(spec)], capture_output=True,
                              text=True, timeout=300, stdin=subprocess.DEVNULL)
    finally:
        shutil.rmtree(sockdir, ignore_errors=True)
    _assert_fatal_boot(done, "ghost")
    assert "MARK applied" not in done.stdout + done.stderr, done.stdout


def test_a_fiber_that_already_failed_is_reported_by_its_end_state(tmp_path):
    """In `--once` mode `Bad`'s failed activation is kept, then `Ghost` (not in
    this build) makes the boot fatal. `Gone` returns at once for a FAILED
    fiber, which replays nothing, so its dispose line must name that state
    and never claim inverses replayed; `Impl`'s does replay."""
    binary = _build(tmp_path, doc=_LOAD_FAIL_DOC)
    spec = tmp_path / "bad-ghost.json"
    spec.write_text(json.dumps({**_SPEC, "components": ["Impl", "Bad", "Ghost"],
                                "probe": []}))
    done = subprocess.run([binary, str(spec)], capture_output=True, text=True,
                          timeout=300, stdin=subprocess.DEVNULL)
    _assert_fatal_boot(done, "Ghost")
    bad = [line for line in done.stdout.splitlines()
           if "| Bad " in line and "dispose" in line]
    assert bad and all("ended failed" in line and "inverses replay" not in line
                       for line in bad), done.stdout
    impl = [line for line in done.stdout.splitlines()
            if "| Impl " in line and "dispose" in line]
    assert impl and all("inverses replay" in line for line in impl), done.stdout
    assert "MARK INVERSE-RAN" in done.stdout + done.stderr, done.stderr


# ---------------------------------------------------------------------------
# held-mode teardown waits for each dispose (issue #2336)
# ---------------------------------------------------------------------------

# The inverse is slow on purpose: a busy loop long enough that a process which
# only REQUESTS the dispose reaches `DOWN` and exits long before the inverse
# prints. Plain `println` goes to stderr, so the run merges stderr into stdout
# to keep one total order with the runner's own `DOWN` line.
_SLOW_UNDO_DOC = '''
service Vault { fn show(user: Str) -> Str }

extern pure fn mark(what: Str) -> Str = @go {
	if what == "INVERSE-RAN" {
		sum := 0
		for i := 0; i < 400000000; i++ {
			sum += i & 3
		}
		println("SPIN", sum)
	}
	println("MARK " + what)
	return what
}

component Impl provides vault: Vault {
  let slot = effect mark("applied") undo mark("INVERSE-RAN")

  provide vault {
    fn show(user) { return "shown " + user }
  }
}
'''


def test_held_teardown_waits_for_the_inverse_before_down(tmp_path):
    """Outside `--once` the runner holds until stdin closes, then disposes each
    fiber. `Dispose` only requests the teardown, so a runner that does not wait
    prints `DOWN` and exits before the held component's inverse has run. The
    inverse marker must precede `DOWN`, and the dispose line must say what was
    confirmed rather than claim a bare `dispose`."""
    binary = _build(tmp_path, doc=_SLOW_UNDO_DOC)
    spec = tmp_path / "held.json"
    spec.write_text(json.dumps({**_SPEC, "probe": [], "once": False}))
    done = subprocess.run([binary, str(spec)], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, timeout=300,
                          stdin=subprocess.DEVNULL)
    out = done.stdout
    assert done.returncode == 0, out
    assert "MARK applied" in out and "] DOWN" in out, out
    assert "MARK INVERSE-RAN" in out, out
    assert out.index("MARK applied") < out.index("MARK INVERSE-RAN") < out.index("] DOWN"), out
    impl = [line for line in out.splitlines() if "| Impl " in line and "dispose" in line]
    assert impl and all("inverses replay" in line for line in impl), out


# ---------------------------------------------------------------------------
# an unconfirmed dispose is not a clean unwind (issue #2339)
# ---------------------------------------------------------------------------

# The inverse never returns, so the bounded wait for its dispose runs out.
_STUCK_UNDO_DOC = '''
service Vault { fn show(user: Str) -> Str }

extern pure fn mark(what: Str) -> Str = @go {
	if what == "INVERSE-RAN" {
		println("MARK STUCK")
		select {}
	}
	println("MARK " + what)
	return what
}

component Impl provides vault: Vault {
  let slot = effect mark("applied") undo mark("INVERSE-RAN")

  provide vault {
    fn show(user) { return "shown " + user }
  }
}
'''


@pytest.mark.parametrize("once", [False, True], ids=["held", "once"])
def test_an_unconfirmed_dispose_is_not_reported_as_a_clean_teardown(tmp_path, once):
    """`src/revl/placement.py` reads `DOWN` as proof that the LIFO unwind
    finished. When a dispose is not confirmed inside its bound the runner must
    not say `DOWN` (nor a residue verdict): one scrubbed FATAL line on stderr
    naming the fiber, and a non-zero exit, so the conductor reports the child
    as exited without saying DOWN."""
    binary = _build(tmp_path, doc=_STUCK_UNDO_DOC)
    spec = tmp_path / "stuck.json"
    spec.write_text(json.dumps({**_SPEC, "probe": [], "once": once}))
    done = subprocess.run([binary, str(spec)], capture_output=True, text=True,
                          timeout=300, stdin=subprocess.DEVNULL,
                          env={**os.environ, "REVL_TEARDOWN_GRACE": "1"})
    both = done.stdout + done.stderr
    assert "MARK STUCK" in both, both
    assert "dispose NOT confirmed" in both, both
    assert "] DOWN" not in done.stdout, done.stdout
    assert "NO-RESIDUE" not in done.stdout, done.stdout
    fatal = [line for line in done.stderr.splitlines() if "] FATAL " in line]
    assert len(fatal) == 1 and "Impl" in fatal[0], done.stderr
    assert done.returncode == 1, both


def test_the_teardown_bound_follows_revl_teardown_grace(tmp_path):
    """The conductor's stranded-teardown report tells a user to raise
    REVL_TEARDOWN_GRACE, so the runner's own dispose bound reads it (seconds)
    and keeps the conductor's 30s only as the default. With a never-settling inverse
    and a 1 second grace the runner gives up well before that default."""
    binary = _build(tmp_path, doc=_STUCK_UNDO_DOC)
    spec = tmp_path / "grace.json"
    spec.write_text(json.dumps({**_SPEC, "probe": [], "once": False}))
    started = time.monotonic()
    done = subprocess.run([binary, str(spec)], capture_output=True, text=True,
                          timeout=300, stdin=subprocess.DEVNULL,
                          env={**os.environ, "REVL_TEARDOWN_GRACE": "1"})
    elapsed = time.monotonic() - started
    assert done.returncode == 1, done.stdout + done.stderr
    assert "dispose NOT confirmed" in done.stdout + done.stderr
    assert elapsed < 4.0, elapsed
