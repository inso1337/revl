"""The placement runner's own console channels sit behind the redaction funnel
(issue #814; advisory GHSA-4x4q-296m-9x9x L1/M1 extension).

`runtime._record` funnels the host trace and `bridge.seam_failure` funnels the
wire reply, but this process's OWN console lines sat outside both: a probe
result, a probe error quoting its argument, the E-Stop inventory's stranded
`repr(resource)` entries, and (the finding itself) an uncaught exception's
traceback to stderr, which the conductor merges verbatim. Every channel this
fix covers now reads the SAME registry the runtime marks, and the failure
paths run the two-stage funnel (own arguments first, then secrets) the wire
reply has had since item 421 F5.

WHAT IS COVERED HERE, CHANNEL BY CHANNEL. Each row names the sink in
`_process_runner.py` that would have to be un-funnelled for that test to fail,
except the last, which pins a byte-identity property rather than a redaction:

  `log()` probe RESULT   test_a_probe_result_is_scrubbed_by_the_log_funnel
                         live placement; un-funnel `log()`'s own print -> FAIL
  `log()` probe ERROR    test_the_probe_error_line_does_not_carry_its_argument
                         live placement; un-funnel the `_redact_call` in the
                         probe loop's `except` -> FAIL
  `main()` FATAL         test_main_catch_all_prints_one_redacted_line
                         un-funnel the `_redact_call` in the catch-all -> FAIL
  `main()` BootRefused   test_the_boot_refusal_line_is_funnelled
                         un-funnel the `_funnel_line` in the catch-all -> FAIL
  `_estop_watch` HALTED  test_the_estop_inventory_is_funnelled
                         un-funnel the `_funnel_line` -> FAIL
  the OTHER threads    test_a_failure_on_a_thread_prints_one_redacted_line
                         `run()` starts `revl-estop` and `revl-control`; a raise
                         on either never reaches `main()`. Drop the
                         `threading.excepthook` install -> the runtime prints
                         the value verbatim and this test fails on the canary
  `_thread_fatal`      test_the_thread_hook_prints_one_redacted_line
                         the hook's own unit arm: un-funnel `_fatal_line` -> FAIL
  `_funnel_line` itself  test_funnel_line_scrubs_a_registered_secret
                         never a site test: proves the sink the four routed
                         prints share, on the channel that has no other canary
  UP / DOWN / REPOINTED  test_the_parsed_protocol_lines_survive_the_funnel
  / residue report       not a redaction test: the conductor matches `UP` and
                         `DOWN` BYTE FOR BYTE, so this pins that routing them
                         through `_funnel_line` changed nothing it can see.

The three live tests drive `revl run --once` end to end and are skipped when the
cordis runtime is absent, exactly like the neighbouring item-421 and seam-2
files. They are the only tests here that exercise the REAL `run()` loop: two are
redaction tests (the probe RESULT and the probe ERROR) and the third pins byte
identity of the parsed protocol lines. Every other test here is a unit test and
says so in its own docstring.

Every assertion is PAIRED (canary absent AND marker present) so none can pass
on an empty line.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import _process_runner as runner  # noqa: E402

CANARY = "SEKRIT-RUNNER-CANARY-814"
REDACTED = "<redacted:secret>"
REDACTED_ARG = "<redacted:arg>"


def _cordis_python() -> str | None:
    """The interpreter that can BOOT a composition, or None.

    `REVL_PY` first (ci/placement_smoke.sh's own override), then this
    interpreter when the suite is already running under the runtime venv (the
    case in a worktree, where the repo-root venv path does not exist), and only
    then the repo-root venv."""
    override = os.environ.get("REVL_PY")
    if override and Path(override).exists():
        return override
    if importlib.util.find_spec("cordis") is not None:
        return sys.executable
    local = ROOT / "backends" / "python" / ".venv" / "bin" / "python"
    return str(local) if local.exists() else None


CORDIS_PY = _cordis_python()
needs_cordis = pytest.mark.skipif(
    CORDIS_PY is None,
    reason="needs the cordis-py runtime (backends/python/.venv/bin/python)")


@pytest.fixture(autouse=True)
def _fresh_registry():
    confidential = runner._funnel()
    confidential.forget_secret_values()
    yield
    confidential.forget_secret_values()


def test_redact_scrubs_a_registered_secret():
    runner._funnel().register_secret_value(CANARY)
    out = runner._redact(f"probe => {{'token': '{CANARY}'}}")
    assert CANARY not in out
    assert REDACTED in out


def test_redact_leaves_ordinary_text_alone():
    runner._funnel().register_secret_value(CANARY)
    text = "[c] probe| db.query | => [{'id': 1}]"
    assert runner._redact(text) == text


def test_redact_call_scrubs_arguments_first_then_secrets():
    runner._funnel().register_secret_value(CANARY)
    out = runner._redact_call(f"KeyError: '{CANARY}'", [CANARY])
    # the ARGUMENT stage wins over the registry stage (the more specific fact)
    assert CANARY not in out
    assert REDACTED_ARG in out
    assert REDACTED not in out


def test_redact_call_reports_a_held_secret_as_a_secret():
    runner._funnel().register_secret_value(CANARY)
    out = runner._redact_call(f"upstream refused token {CANARY}", ["user-1234"])
    assert CANARY not in out
    assert REDACTED in out


def test_funnel_line_scrubs_a_registered_secret(capsys):
    """The sink itself, on the channel that has no other canary: `main()`'s
    `BootRefused` line and the residue report are the same call."""
    runner._funnel().register_secret_value(CANARY)
    runner._funnel_line(f"[only] REPOINTED {CANARY} -> /run/db.sock")
    out = capsys.readouterr().out
    assert CANARY not in out, out
    assert out == f"[only] REPOINTED {REDACTED} -> /run/db.sock\n", out


def test_funnel_line_is_identity_for_the_four_parsed_lines(capsys):
    """`_funnel_line` composes the lines `placement.pump` parses. For a line
    holding no registered secret it must emit the SAME bytes (exact compare
    `text == f"[{name}] UP"`), so the emitted line is captured, not `_redact`'s
    return value."""
    runner._funnel().register_secret_value(CANARY)
    for line in ("[only] UP",
                 "[only] DOWN",
                 "[only] REPOINTED db -> /run/db.sock",
                 "[only] residue no residue | registry=0 provisions=[] "
                 "disposables=1/1"):
        runner._funnel_line(line)
        assert capsys.readouterr().out == line + "\n", line


class _Raising:
    def get(self, key):
        raise KeyError(key)


def test_eval_probe_hands_parsed_arguments_back():
    args: list = []
    with pytest.raises(KeyError):
        runner._eval_probe("vault.get('alice')", {"vault": _Raising()},
                           args_out=args)
    assert args == ["alice"]


def test_eval_probe_default_keeps_old_signature():
    # `_eval_probe`'s other callers (test_distribute.py) pass two args.
    args: list = []
    with pytest.raises(ValueError, match="not a key this process holds"):
        runner._eval_probe("other.query('x')", {"vault": _Raising()}, args)
    assert args == []  # the parse never reached the literals


def test_the_probe_error_composition_is_two_stage():
    """UNIT test of the shape the probe loop's `except` composes.

    It spells the loop's own two lines back out, so it survives a change to
    `run()` and cannot stand in for the live test below; the earlier version of
    this file presented it as coverage of `run()`'s stage, which is what the
    review measured as false. It is kept because it still pins the ORDER of the
    two stages (argument before registry) without needing the cordis runtime.
    """
    runner._funnel().register_secret_value(CANARY)
    args: list = []
    try:
        runner._eval_probe("vault.get('alice')", {"vault": _Raising()},
                           args_out=args)
    except Exception as exc:  # noqa: BLE001 - the runner's own catch shape
        detail = "ERROR " + runner._redact_call(f"{type(exc).__name__}: {exc}", args)
    else:  # pragma: no cover - the probe above always raises
        raise AssertionError("the probe did not raise")
    assert "alice" not in detail, detail
    assert REDACTED_ARG in detail, detail
    assert REDACTED not in detail, detail


def test_main_catch_all_prints_one_redacted_line(tmp_path, monkeypatch, capsys):
    """`main()`'s last unguarded channel: an exception `run()` does not catch
    used to escape as a bare traceback to stderr (merged verbatim by the
    conductor). Now: one funnelled line, non-zero exit, no traceback.

    The exception is raised BY THE MONKEYPATCHED `run()` and carries the canary
    in its message, so the canary really is in the text the catch-all has to
    scrub. The earlier version of this test raised from the load path with a
    missing file path instead, which put the PATH in the message and left the
    canary registered but absent from the text, so the assertion held with the
    funnel deleted. Do not go back to that shape.
    """
    runner._funnel().register_secret_value(CANARY)

    async def _boom(spec, spec_path=None):
        raise ValueError(f"serve setup failed while opening {CANARY}")

    monkeypatch.setattr(runner, "run", _boom)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({"name": "boom"}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["_process_runner.py", str(spec_path)])
    with pytest.raises(SystemExit) as excinfo:
        runner.main()
    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err, err
    assert "FATAL" in err, err
    # redact, do not delete: the type and the sentence around the value stay
    assert "ValueError" in err, err
    assert "serve setup failed" in err, err
    assert CANARY not in err, err
    assert REDACTED in err, err


def test_the_boot_refusal_line_is_funnelled(tmp_path, monkeypatch, capsys):
    """`main()`'s `BootRefused` line (item 337 Seam 2) names every refused key,
    and a refused key is composed from the spec, so it is the other line that
    reaches a console from `main()` rather than from `run()`'s `log()`. It is
    funnelled on stdout, next to the `REFUSED` line that caused it."""
    runner._funnel().register_secret_value(CANARY)

    async def _boom(spec, spec_path=None):
        raise runner.BootRefused(f"refused boot seam for provider '{CANARY}'")

    monkeypatch.setattr(runner, "run", _boom)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({"name": "seam2"}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["_process_runner.py", str(spec_path)])
    with pytest.raises(SystemExit) as excinfo:
        runner.main()
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert "refused boot seam for provider" in captured.out, captured.out
    assert CANARY not in captured.out, captured.out
    assert REDACTED in captured.out, captured.out


class _EstopExit(Exception):
    """Stand-in for `os._exit`, which would take pytest down with it."""


class _Latch:
    """The one call `_estop_watch` makes into the runtime module."""

    def __init__(self, record):
        self.record = record
        self.calls = 0

    def estop_from_latch(self):
        self.calls += 1
        return self.record


def test_the_estop_inventory_is_funnelled(monkeypatch, capsys):
    """M1, the child half of the E-Stop: the printed inventory carries
    `stranded`, whose entries are `repr` of live resources, so a connection
    string with its password in it is the shape at issue. The line is printed
    by `_estop_watch`, which is NOT inside `run()` and therefore funnels
    itself."""
    runner._funnel().register_secret_value(CANARY)
    # two `repr`s of live objects that hold the value: a connection string with
    # the password in its userinfo, and a payload that quotes it plainly
    stranded = [f"<PgPool dsn=postgres://revl:{CANARY}@db.invalid/app>",
                f"<DeliveryTag payload={CANARY}>"]
    record = {"reason": "operator pressed it", "operator": "alice",
              "activations": [{"id": "a1"}], "inFlight": [], "stranded": stranded}

    def _exit(code):
        raise _EstopExit(code)

    monkeypatch.setattr(runner.os, "_exit", _exit)
    with pytest.raises(_EstopExit) as excinfo:
        runner._estop_watch("svc", _Latch(record), poll=0.01)
    assert excinfo.value.args == (75,)  # `_ESTOP_EXIT`: crash-shaped, no unwind

    out = capsys.readouterr().out
    assert "HALTED" in out, out
    assert CANARY not in out, out
    assert REDACTED in out, out
    # both entries are still there, scrubbed in place rather than dropped
    assert '"<DeliveryTag payload=<redacted:secret>>"' in out, out
    assert "postgres://revl:<redacted:secret>@db.invalid/app" in out, out
    # the inventory is still an inventory: the non-secret fields survive, so a
    # reader can still tell what was in flight when the button was pressed
    assert '"verdict": "halted"' in out, out
    assert '"reason": "operator pressed it"' in out, out
    assert '"resumable": false' in out, out
    assert json.loads(out.split("HALTED ", 1)[1])["process"] == "svc", out


# ---------------------------------------------------------------------------
# live: a real placement, the real `run()` loop
# ---------------------------------------------------------------------------

# One process, three provide methods, each aimed at a different sink:
#   `get`  raises `KeyError` quoting its ARGUMENT (the probe-error stage)
#   `arm`  calls a `Secret[Str]` extern, registering the value process-wide
#   `echo` returns an ORDINARY string that happens to BE that value, so the
#          probe RESULT is what carries a registered secret to the console
APP = """
extern pure fn as_text() -> Str = @py { return "SEKRIT-RUNNER-CANARY-814" }

extern pure fn raise_key(k: Str) -> Str = @py { raise KeyError(k) }

extern emission[vault.mint] fn mint_token(u: Str) -> Secret[Str]
  = @py { return "SEKRIT-RUNNER-CANARY-814" }

service Vault {
  fn get(k: Str) -> Str
  fn echo() -> Str
  emission fn arm(u: Str) -> Int
}

component LocalVault provides vault: Vault {
  provide vault {
    fn get(k) {
      return raise_key(k)
    }
    fn echo() {
      return as_text()
    }
    fn arm(u) {
      let t = emit mint_token(u)
      return 1
    }
  }
}
"""

PLACEMENT_HEAD = '[processes.only]\ncomponents = ["LocalVault"]\n'


def _run_placement(tmp_path, probe: list) -> str:
    """Boot one placement through the conductor and return its whole trace."""
    source = tmp_path / "funnel.rvl"
    source.write_text(APP, encoding="utf-8")
    toml = tmp_path / "funnel.toml"
    toml.write_text(PLACEMENT_HEAD + f"probe = {json.dumps(probe)}\n",
                    encoding="utf-8")
    result = subprocess.run(
        [CORDIS_PY, "-m", "revl", "run", str(source),
         "--placement", str(toml), "--once"],
        capture_output=True, text=True, timeout=300,
        stdin=subprocess.DEVNULL, cwd=str(source.parent),
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    trace = result.stdout + result.stderr
    assert result.returncode == 0, trace
    return trace


def _probe_line(trace: str, marker: str):
    """The one probe line carrying `marker`, split into its three fields.

    A helper rather than three one-liners because the line format is
    `[name] <channel:6>| <subject:16>| <detail>`: the SUBJECT is padded to a
    fixed width, so a test that split on `" | "` would silently pick the wrong
    field (or none at all) once a subject is shorter than 16 characters.
    """
    hits = [ln for ln in trace.splitlines() if "] probe" in ln and marker in ln]
    assert len(hits) == 1, trace
    parts = hits[0].split("|")
    assert len(parts) == 3, hits[0]
    return hits[0], parts[1], parts[2]


@needs_cordis
def test_a_probe_result_is_scrubbed_by_the_log_funnel(tmp_path):
    """The probe RESULT is the channel the review measured as uncovered: with
    `log()`'s own print made raw, this run prints `=> 'SEKRIT-RUNNER-CANARY-814'`.

    `arm` runs first and calls a `Secret[Str]` extern, so the value is in the
    process's registry by the time `echo` returns it inside an ordinary string,
    which is the shape issue #814 is about: the process was trusted with the
    value and a sink it never analysed printed it.
    """
    trace = _run_placement(tmp_path, ["vault.arm('alice')", "vault.echo()"])

    line, subject, detail = _probe_line(trace, "vault.echo")
    assert "] probe | vault.arm('alice')| => 1" in trace, trace
    assert subject.strip() == "vault.echo()", line
    # the RESULT is the crossing, and it is scrubbed...
    assert CANARY not in detail, line
    assert REDACTED in detail, line
    assert detail == " => '<redacted:secret>'", line
    # ...and so is the whole trace, which the conductor merges verbatim
    assert CANARY not in trace, trace
    # the control: an ordinary probe result is still verbatim, so the redaction
    # did not simply blank the console
    assert "=> 1" in trace, trace


@needs_cordis
def test_the_probe_error_line_does_not_carry_its_argument(tmp_path):
    """The in-process half of the probe-error stage, on the REAL `run()` loop.

    The canary is the probe's own LITERAL argument and is not registered, so
    the registry stage cannot catch it: only the two-stage `_redact_call` in
    `run()`'s probe `except` can. Delete that stage and the detail reads
    `ERROR KeyError: 'SEKRIT-RUNNER-CANARY-814'` and this test fails.

    The SUBJECT is the placement's declared probe text, which is composition
    data the author wrote and is quoted verbatim by design (the same stance the
    item-421 tests take for `cache.put('alice')`), so the assertions below are
    made against the DETAIL.
    """
    trace = _run_placement(tmp_path, [f"vault.get('{CANARY}')"])

    line, subject, detail = _probe_line(trace, "vault.get")
    assert subject.strip() == f"vault.get('{CANARY}')", line  # the declared text
    assert CANARY not in detail, line
    assert REDACTED_ARG in detail, line
    assert REDACTED not in detail, line
    assert detail.startswith(" ERROR KeyError: "), line
    # and the leak it would have been: the canary is nowhere else in the trace
    assert trace.count(CANARY) == 1, trace


@needs_cordis
def test_the_parsed_protocol_lines_survive_the_funnel(tmp_path):
    """`UP` and `DOWN` are compared BYTE FOR BYTE (`text == f"[{name}] UP"` by
    `placement.pump`) and `REPOINTED` is regex-matched by `_re_repoint`, so
    routing them through `_funnel_line` had to leave them untouched: a
    registered secret in the registry must not perturb a line that holds none
    of it. The residue report is parsed by nobody; it is on the same funnel and
    is asserted here for the same reason."""
    trace = _run_placement(tmp_path, ["vault.arm('alice')"])
    assert "[only] UP" in trace, trace
    assert "[only] DOWN" in trace, trace
    assert re.search(r"^\[only\] residue no residue \| registry=0 "
                     r"provisions=\[\] disposables=\d+/\d+$", trace, re.M), trace
    # exactly one of each: a duplicated `UP` is as fatal to the handshake as a
    # missing one
    assert trace.count("[only] UP") == 1, trace
    assert trace.count("[only] DOWN") == 1, trace


# ---------------------------------------------------------------------------
# the THREAD channel: `run()` starts `revl-estop` and `revl-control`, and a
# raise on either never reaches `main()`
# ---------------------------------------------------------------------------

ARMED_NAME = "svc"


def test_main_arms_the_thread_hook_before_run_can_start_a_thread():
    """SHAPE. `threading.excepthook` is process-wide, so arming it is only
    correct BEFORE `run()` runs — `run()` is what starts the two threads whose
    failures the hook exists for — and `_FATAL_NAME` has to be set first
    because the hook reads it from another thread. Both orderings are asserted
    on `main()`'s SOURCE, sliced rather than grepped: an install placed after
    the `try` is still "in `main()`" and would pass an `in` check while being
    too late to matter."""
    body = inspect.getsource(runner.main)
    assert body.index('_FATAL_NAME = spec.get("name")') \
        < body.index("threading.excepthook = _thread_fatal"), body
    assert body.index("threading.excepthook = _thread_fatal") \
        < body.index("asyncio.run("), body
    # exactly one install, and no per-thread `Thread(...)`/`threading.Thread`
    # override anywhere: one process-wide funnel, not a set of them
    assert body.count("threading.excepthook =") == 1, body
    assert "setUncaughtExceptionHandler" not in body, body


def test_the_thread_hook_prints_one_redacted_line(monkeypatch, capsys):
    """UNIT, on the hook itself: the failure that reaches `threading.excepthook`
    by DEFAULT prints its traceback verbatim on stderr, which the conductor
    merges (`placement.py::pump` spawns children with `stderr=STDOUT`), and the
    traceback's message quotes whatever the failing frame interpolated. The hook
    is handed a real exception carrying the canary in its message, so the canary
    really is in the text it has to scrub, and `os._exit` is stubbed because the
    real one would take pytest down with it."""
    runner._funnel().register_secret_value(CANARY)
    monkeypatch.setattr(runner, "_FATAL_NAME", ARMED_NAME)
    codes: list[int] = []
    monkeypatch.setattr(runner.os, "_exit", codes.append)
    try:
        raise ValueError(f"estop latch unreadable at {CANARY}")
    except ValueError as exc:
        args = threading.ExceptHookArgs(
            (type(exc), exc, exc.__traceback__, threading.current_thread()))
    runner._thread_fatal(args)

    # no unwind: the process has to die where the thread died, or the teardown
    # prints `DOWN` (E7), which is the conductor's clean-teardown signal
    assert codes == [1]
    err = capsys.readouterr().err
    assert err == (f"[{ARMED_NAME}] FATAL ValueError: "
                   f"estop latch unreadable at {REDACTED}\n"), err
    # redact, do not delete: the type and the sentence around the value stay
    assert CANARY not in err, err
    assert "Traceback" not in err, err


def test_a_systemexit_on_a_thread_is_not_a_failure(monkeypatch, capsys):
    """The default hook ignores `SystemExit`, and so must this one: a thread
    that ends by raising it is asking to be unwound, not reporting a failure.
    Without the guard a plain `raise SystemExit(3)` inside a thread would print
    a FATAL line and kill the process."""
    codes: list[int] = []
    monkeypatch.setattr(runner.os, "_exit", codes.append)
    args = threading.ExceptHookArgs(
        (SystemExit, SystemExit(3), None, threading.current_thread()))
    runner._thread_fatal(args)
    assert codes == []
    assert capsys.readouterr().err == ""


def test_the_thread_hook_shares_the_one_fatal_composer():
    """Both fatal channels — `main()`'s catch-all and the thread hook — have to
    reach the SAME composer, or the second one is free to forget the funnel.
    `_fatal_line` is that composer and there is exactly one `FATAL` line in the
    module, so a channel that spelled its own would be a second one."""
    source = Path(runner.__file__).read_text(encoding="utf-8")
    assert source.count('] FATAL ') == 1, "a second FATAL composer appeared"
    hook = inspect.getsource(runner._thread_fatal)
    assert "_fatal_line(args.exc_value)" in hook, hook
    assert "print(" not in hook, hook
    assert "_fatal_line(" in inspect.getsource(runner.main)


# The live arm's driver. `run()`'s own two threads have no DETERMINISTIC escape
# — the E-Stop latch read fails CLOSED (an unreadable or malformed latch is a
# verdict, not a raise) and `control_reader` swallows a malformed control line —
# so the live test pins the CHANNEL with the runner's real thread BODY
# (`_estop_watch`, the target `run()` starts for `revl-estop`) and a runtime stub
# whose latch read raises. What is under test is the channel, not the stub: the
# armed arm installs the hook with the SAME statement `main()` executes, and the
# unarmed arm is the non-vacuity control that shows the value verbatim.
_THREAD_DRIVER = '''
import sys, threading, time
sys.path.insert(0, {src!r})
from revl import _process_runner as runner


class _UnreadableLatch:
    def estop_from_latch(self):
        raise RuntimeError("latch read failed on " + {canary!r})


runner._funnel().register_secret_value({canary!r})
runner._FATAL_NAME = {name!r}
if sys.argv[1] == "armed":
    threading.excepthook = runner._thread_fatal
threading.Thread(target=runner._estop_watch, args=({name!r}, _UnreadableLatch()),
                 name="revl-estop", daemon=True).start()
time.sleep(2)
'''


def _run_thread_driver(tmp_path, armed: bool):
    """Run the driver out of process: `_thread_fatal` ends in `os._exit`, which
    is the point of it and is also why this arm cannot run inside pytest."""
    script = tmp_path / "thread_driver.py"
    script.write_text(_THREAD_DRIVER.format(src=str(ROOT / "src"), canary=CANARY,
                                            name=ARMED_NAME), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(script), "armed" if armed else "unarmed"],
        capture_output=True, text=True, timeout=120, cwd=str(tmp_path))


def test_a_failure_on_a_thread_prints_one_redacted_line(tmp_path):
    """The finding, on a REAL thread. The default hook's line is
    `Exception in thread revl-estop: Traceback ... RuntimeError: latch read
    failed on SEKRIT-RUNNER-CANARY-814`, and the conductor merges it verbatim.
    With the hook armed: one funnelled line, exit 1, no traceback — and the
    process does NOT unwind, so no `DOWN` is printed."""
    result = _run_thread_driver(tmp_path, armed=True)
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    assert result.stdout == "", result.stdout
    assert result.stderr.count("FATAL") == 1, result.stderr
    assert result.stderr == (f"[{ARMED_NAME}] FATAL RuntimeError: "
                             f"latch read failed on {REDACTED}\n"), result.stderr
    assert CANARY not in result.stderr, result.stderr
    assert "Exception in thread" not in result.stderr, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    assert "DOWN" not in result.stdout + result.stderr


def test_without_the_thread_hook_the_runtime_prints_the_value(tmp_path):
    """NON-VACUITY, the same run with the one install statement deleted. The
    process survives (the default hook does not exit), so this also pins that
    the armed arm's exit code came from the hook and not from the raise."""
    result = _run_thread_driver(tmp_path, armed=False)
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert CANARY in result.stderr, result.stderr
    assert "Exception in thread revl-estop" in result.stderr, result.stderr
    assert "FATAL" not in result.stderr, result.stderr
