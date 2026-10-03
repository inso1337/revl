"""The ts placement runner's uncaught-failure channel sits behind the redaction
funnel (issue #814, the ts half of advisory GHSA-4x4q-296m-9x9x L1).

`log()` funnels every line this process prints (item 815) and `bridge.ts`
funnels the reply it sends back across a seam, but the failures those two expect
are not the only ones a boot can raise: a ref hash check, a serve setup, an
inverse during teardown — anything the load path and the probe path do not catch
— used to escape as an UNCAUGHT EXCEPTION, and node prints that itself: the
error, its message and the full stack, straight to stderr, unfunnelled. The
conductor merges that verbatim (`placement.py::pump` spawns children with
`stderr=subprocess.STDOUT`), and a message and a stack quote whatever the failing
frame held. The py driver closed this in the same issue
(`_process_runner.main`'s catch-all), as did the java runners and the go runner.

WHAT IS COVERED HERE, CHANNEL BY CHANNEL. Each row names the site in
`placement_runner.ts` that would have to be un-wired for that test to fail,
except the shape tests, which pin a property rather than a redaction:

  the funnel           test_the_funnel_is_installed_before_anything_can_fail
                       move the `process.on` pair below the spec load -> FAIL
  the FATAL line       test_the_fatal_line_is_written_to_fd_2_through_the_redactor
                       `redactText(detail)` -> `detail` -> FAIL
  `exit`, not unwind   test_the_fatal_path_exits_instead_of_unwinding_into_teardown
                       call `teardown()` -> FAIL (it prints `DOWN`, the
                       conductor's clean-teardown signal, for a process that
                       died mid-boot — E7)
  both dispositions    test_both_uncaught_exception_and_unhandled_rejection_are_registered
  the label            test_the_label_is_hoisted_so_a_failure_before_the_spec_prints_one
  the registry         test_the_receiver_is_what_registers_the_value
                       this one pins WHY the live trigger has to be shaped the
                       way it is (see below), not the funnel itself
  the whole channel    test_an_uncaught_failure_prints_one_redacted_line
                       live node: the real emitted registry, the real runner
  the second channel   test_an_unhandled_rejection_takes_the_same_funnel
  non-vacuity (a)      test_with_the_redaction_unwired_the_value_appears
                       `redactText(detail)` -> `detail`, on a copy of the runner
  non-vacuity (b)      test_with_the_funnel_unregistered_node_prints_the_raw_failure
                       the SHIPPED runner, with a preload that shadows
                       `process.on` so the two registrations do not take

THE TRIGGER, AND WHY IT HAS TO BE THIS SHAPE. The ts registry is populated at
CALL time only — `host.markSecret(...)` at the head of a provide method whose
service declared a `Secret[T]` parameter, `host.secretResult(...)` around a host
call that declared one (`emit.py:1492`, `:4376`, `:4394`). Unlike the java tier
there is no load-time registration of a `Secret[T]` config field, so a boot that
fails before any provide method runs has nothing registered to leak: this
channel can only ever disclose a value the running process already knows, which
is why the value has to arrive through the seam's declared receiver first, and
why the live tests below run a probe before the failure.

The failing call is a host body that throws from a LATER macrotask than the call
that reached it (`setTimeout`), the shape a driver error takes when the socket
errors after `connect` returned. The probe's own `try` has already completed by
then, so nothing on the call path catches it and the process-level disposition
is all that is left — which is exactly the channel this funnel owns.

The one window it cannot cover is the module graph itself: an `import` that
fails is instantiated before any statement of the runner runs, and no handler can
be installed yet. Nothing is registered at that point either, so there is nothing
for it to disclose, and a `runtime.ts` that does not parse is a build error the
conductor reports rather than a boot input.

WHY THIS FILE LIVES HERE and not in `tests/`: the `backend-typescript` CI job
runs `pytest backends/typescript/` against a checkout that has just done
`npm ci`, so node and the `cordis` dependency are unconditionally present. Every
assertion is PAIRED (canary absent AND marker present) so none can pass on an
empty line.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import signal
import subprocess
import time
import sys
import warnings
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from revl import compile_source                                    # noqa: E402
from revl.test import _node_can_run_emitted, _node_version          # noqa: E402

_RUNNER = _HERE / "placement_runner.ts"
_RUNTIME = _HERE / "runtime.ts"
# The runner's own relative imports; a copy of the runner needs them beside it.
_DEPENDENCIES = ("bridge.ts", "runtime.ts")

CANARY = "SEKRIT-CANARY-814-TS"
REDACTED_SECRET = "<redacted:secret>"
PUBLIC_USER = "u1"
PROBE = f"vault.show('{CANARY}', '{PUBLIC_USER}')"

# The service declares the credential position, so the emitted provide method
# registers whatever arrives there; the host body it hands the value to fails
# from a later macrotask, with a message that quotes its argument. Nothing the
# author wrote interpolates the secret into the failure.
_DOCUMENT = """
service Vault { emission fn show(token: Secret[Str], user: Str) -> Str }

component Impl provides vault: Vault {
  provide vault {
    fn show(token, user) {
      emit boom(token)
      return "shown " + user
    }
  }
}

extern emission fn boom(key: Secret[Str]) -> Unit = @ts {
  setTimeout(() => { throw new Error("boom " + key) }, 5)
  return
}
"""

# The same document, with the host body rejecting instead of throwing: node's
# default disposition for an unhandled rejection is to raise it as an uncaught
# exception, so this arm exercises the second registration rather than the first.
_DOCUMENT_REJECTS = _DOCUMENT.replace(
    'setTimeout(() => { throw new Error("boom " + key) }, 5)',
    'Promise.reject(new Error("boom " + key))')

needs_node = pytest.mark.skipif(
    not _node_can_run_emitted(_node_version())
    or not (_HERE / "node_modules" / "cordis").exists(),
    reason="needs backends/typescript/node_modules and node >= 22.18 "
           "(`cd backends/typescript && npm ci`)",
)


def _emitter():
    spec = importlib.util.spec_from_file_location("revl_ts_emit_814", _HERE / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _emitted(tmp_path: Path, document: str = _DOCUMENT) -> Path:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ir = compile_source(document)
    module = tmp_path / "mod.ts"
    module.write_text(_emitter().emit(ir, runtime_import=str(_RUNTIME)), encoding="utf-8")
    return module


def _spec(tmp_path: Path, module: Path) -> Path:
    """The placement spec `placement.py` would hand this child.

    `once` is False on purpose: the failure arrives on a later macrotask, and a
    one-shot placement tears down and exits before it can.
    """
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "name": "vault",
        "module": str(module),
        "components": ["Impl"],
        "config": {},
        "provides": ["vault"],
        "proxies": {},
        "probe": [PROBE],
        "once": False,
    }), encoding="utf-8")
    return spec


def _run(spec: Path, runner: Path = _RUNNER, preload: Path | None = None,
         timeout: int = 300, env: dict | None = None) -> "tuple[int, str, str]":
         timeout: int = 300) -> "tuple[int, str, str]":
    argv = ["node"]
    if preload is not None:
        argv += ["--import", str(preload)]
    argv += [str(runner), str(spec)]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL, cwd=_HERE, env=env)
                          stdin=subprocess.DEVNULL, cwd=_HERE)
    return done.returncode, done.stdout, done.stderr


def _strip_comments(src: str) -> str:
    """Blank out `//` and `/* */` comments in C-family source (ts/go/java/rs).

    String literals are walked, not stripped: the `FATAL` line being searched
    for IS a string literal, so removing them would remove the target. Walking
    them is still necessary so a `//` inside a string does not start a comment.
    Comment bodies are replaced with spaces rather than deleted so that
    everything keeps its offset and a failure message can still be read against
    the real file.
    """
    out = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'`":
            quote, j = c, i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == quote:
                    j += 1
                    break
                j += 1
            out.append(src[i:j])
            i = j
        elif src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in src[i:j]))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _only_offset(pattern: str, code: str, what: str) -> int:
    """Offset of the one and only match. Requiring exactly one is half the
    non-vacuity: a pattern that starts matching something else fails loudly here
    rather than silently comparing the wrong pair."""
    hits = list(re.finditer(pattern, code))
    assert len(hits) == 1, (
        f"expected exactly one {what} in the runner source, found {len(hits)}"
        f" for /{pattern}/")
    return hits[0].start()


def _first_offset(pattern: str, code: str, what: str) -> int:
    hits = list(re.finditer(pattern, code))
    assert hits, f"no {what} in the runner source (/{pattern}/)"
    return hits[0].start()


# The sites a boot has to get through, each of which can raise. Nothing may run
# before the funnel that is not the funnel's own definition.
_FALLIBLE_SITES = [
    (r"fs\.readFileSync\(process\.argv\[2\]", "spec read"),
    (r"JSON\.parse\(", "spec parse"),
    (r"await import\(", "emitted-module import"),
    (r"await sha256File\(", "ref pin check"),
    (r"new Context\(", "context construction"),
    (r"ctx\.plugin\(", "component load"),
    (r"serve\(", "seam serve setup"),
]

_FATAL_WRITE = "  fs.writeSync(2, `[${name}] FATAL ${redactText(detail)}\\n`)\n"
_FATAL_EXIT = "  process.exit(1)\n"
_REGISTRATIONS = (
    "process.on('uncaughtException', fatal)\n"
    "process.on('unhandledRejection', fatal)\n")


# ---------------------------------------------------------------------------
# the shape (runs everywhere)
# ---------------------------------------------------------------------------

def test_the_funnel_is_installed_before_anything_can_fail():
    """An uncaught-exception handler only owns the failures raised after it is
    installed. The registrations therefore have to precede every fallible
    statement in the module — including the spec read, which was the FIRST
    statement here and could itself throw, leaving the funnel installed too late
    to see the failure it exists for."""
    code = _strip_comments(_RUNNER.read_text(encoding="utf-8"))
    installed = _only_offset(r"process\.on\('uncaughtException'", code,
                             "uncaught-exception registration")
    for pattern, what in _FALLIBLE_SITES:
        assert installed < _first_offset(pattern, code, what), (
            f"placement_runner.ts reaches the {what} before installing the "
            "uncaught-failure funnel: a failure raised there escapes as node's "
            "own stack trace on stderr, which the conductor merges verbatim")


def test_the_fatal_line_is_written_to_fd_2_through_the_redactor():
    """One line on the channel the conductor merges, through the registry, on a
    write that cannot still be sitting in a buffer when the process exits."""
    source = _RUNNER.read_text(encoding="utf-8")
    assert _FATAL_WRITE in source
    assert _FATAL_EXIT in source
    # a line buffered in a piped stream when `process.exit` runs is DROPPED, so
    # the funnel must not print through `console`.
    assert "console.log(`[${name}] FATAL" not in source
    assert "console.error(`[${name}] FATAL" not in source
    # the detail is the failure's own text, and a message-less failure still
    # prints a readable line rather than `undefined`.
    assert ("const detail = reason instanceof Error ? "
            "`${reason.name}: ${reason.message}` : String(reason)\n") in source


def test_the_fatal_path_exits_instead_of_unwinding_into_teardown():
    """`process.exit`, not a rethrow: the process must not unwind into a
    teardown that prints `DOWN`, the conductor's clean-teardown signal. A
    process that died mid-boot must not claim one — the same "die where it
    stands, non-zero, no DOWN" rule the E-Stop watcher follows (E7)."""
    code = _strip_comments(_RUNNER.read_text(encoding="utf-8"))
    body = code[code.index("function fatal(reason: unknown): void {"):]
    body = body[:body.index("\n}\n")]
    assert "process.exit(1)" in body
    assert "teardown(" not in body
    assert "DOWN" not in body
    # the FATAL line is not routed through `log()`, which is the trace funnel:
    # that one is a sink of its own and would prefix the line differently.
    assert "log('fatal'" not in body and 'log("fatal"' not in body


def test_both_uncaught_exception_and_unhandled_rejection_are_registered():
    """Node's default disposition for an unhandled rejection is to raise it as
    an uncaught exception, so leaving it to the default leaves exactly this
    channel open, one event-loop turn later. Registering both keeps the line
    shape identical whichever way a failure arrives."""
    code = _strip_comments(_RUNNER.read_text(encoding="utf-8"))
    assert _REGISTRATIONS in code
    assert code.count("process.on('uncaughtException'") == 1
    assert code.count("process.on('unhandledRejection'") == 1


def test_the_label_is_hoisted_so_a_failure_before_the_spec_prints_one():
    """The label is read from the spec, which is the first thing that can fail.
    It is therefore hoisted and defaulted, and initialized before the handlers
    are installed: a handler that ran while the binding was still in its
    temporal dead zone would throw instead of reporting."""
    code = _strip_comments(_RUNNER.read_text(encoding="utf-8"))
    hoisted = _only_offset(r"let name = 'proc'", code, "hoisted label")
    installed = _only_offset(r"process\.on\('uncaughtException'", code,
                             "uncaught-exception registration")
    assert hoisted < installed
    # `let`, not `const`: the assignment below it is what makes it the process's
    # own name, and a `const` would be a second binding.
    assert "name = spec.name ?? 'proc'" in code
    assert "const name = spec.name" not in code


def test_the_receiver_is_what_registers_the_value(tmp_path):
    """The trigger's premise, asserted rather than assumed: the ts registry is
    populated by the DECLARED RECEIVER at call time (`host.markSecret` at the
    head of a provide method whose service declared a `Secret[T]` parameter) and
    by a host call that declared one (`host.secretResult`). There is no
    load-time registration of a config field, so a failure that happens before
    any provide method runs has nothing registered to disclose."""
    module = _emitted(tmp_path).read_text(encoding="utf-8")
    assert "markSecret(token)" in module
    assert module.count("markSecret(") == 1
    # registered at the HEAD of the receiver, so it is already in the registry
    # by the time the body reaches the call that fails later.
    assert module.index("markSecret(token)") < module.index("boom(token)")
    # the other population site, `host.secretResult(...)`, is for a host call
    # that declared a secret RETURN, which this document does not have.
    assert "secretResult(" not in module


# ---------------------------------------------------------------------------
# the live channel: one node process, the real registry, the real runner
# ---------------------------------------------------------------------------

@needs_node
def test_an_uncaught_failure_prints_one_redacted_line(tmp_path):
    spec = _spec(tmp_path, _emitted(tmp_path))
    code, out, err = _run(spec)

    # the run really raised the failure under test, on a later macrotask than
    # the probe that registered the value: without this the absence assertions
    # below would be vacuous.
    assert code == 1, (out, err)
    assert f"probe | vault.show('{REDACTED_SECRET}', '{PUBLIC_USER}')" in out, out
    assert "[vault] UP" in out, out
    assert err.count("FATAL") == 1, err

    # the credential is nowhere on either stream, and the marker took its place
    assert CANARY not in out + err, (out, err)
    assert err.strip() == f"[vault] FATAL Error: boom {REDACTED_SECRET}"

    # a process that died on a later macrotask printed no teardown line: it
    # never reached one, and it must not claim one.
    assert "DOWN" not in out, out


@needs_node
def test_an_unhandled_rejection_takes_the_same_funnel(tmp_path):
    """The same document with the host body rejecting instead of throwing."""
    spec = _spec(tmp_path, _emitted(tmp_path, _DOCUMENT_REJECTS))
    code, out, err = _run(spec)

    assert code == 1, (out, err)
    assert "[vault] UP" in out, out
    assert err.count("FATAL") == 1, err
    assert CANARY not in out + err, (out, err)
    assert err.strip() == f"[vault] FATAL Error: boom {REDACTED_SECRET}"
    assert "DOWN" not in out, out


@needs_node
def test_with_the_redaction_unwired_the_value_appears(tmp_path):
    """The non-vacuity arm: the same run, one edit to the funnel.

    The copy's relative imports are symlinks into this directory, so node's
    realpath resolution gives the copy and the emitted module the SAME
    `runtime.ts` instance — which is what the paired assertion on the probe line
    below measures.
    """
    source = _RUNNER.read_text(encoding="utf-8")
    assert "redactText(detail)" in source
    work = tmp_path / "unwired"
    work.mkdir()
    (work / "placement_runner.ts").write_text(
        source.replace("redactText(detail)", "detail"), encoding="utf-8")
    for name in _DEPENDENCIES:
        (work / name).symlink_to(_HERE / name)
    (work / "node_modules").symlink_to(_HERE / "node_modules")

    spec = _spec(tmp_path, _emitted(tmp_path))
    code, out, err = _run(spec, runner=work / "placement_runner.ts")

    # the funnel is still installed, still reached, still one line...
    assert code == 1, (out, err)
    assert err.count("FATAL") == 1, err
    # ...and the value is now on it, verbatim.
    assert CANARY in err, err
    assert REDACTED_SECRET not in err, err
    # The SAME run still redacts on the probe line, which is what proves the
    # registry is shared with the emitted module and that the edit above — not a
    # missing registration — is what un-redacted the FATAL line.
    assert REDACTED_SECRET in out, out


@needs_node
def test_with_the_funnel_unregistered_node_prints_the_raw_failure(tmp_path):
    """The non-vacuity arm for the CHANNEL: the shipped runner, unmodified,
    with `process.on` shadowed so neither registration takes. What is left is
    what this funnel replaced — node's own disposition, the error, the module,
    the source line and the stack, all of it on stderr with the value in it."""
    preload = tmp_path / "suppress.mjs"
    preload.write_text(
        "const realOn = process.on.bind(process)\n"
        "process.on = (event, listener) =>\n"
        "  event === 'uncaughtException' || event === 'unhandledRejection'\n"
        "    ? process\n"
        "    : realOn(event, listener)\n",
        encoding="utf-8")

    spec = _spec(tmp_path, _emitted(tmp_path))
    code, out, err = _run(spec, preload=preload)

    assert code == 1, (out, err)
    assert CANARY in err, err
    assert REDACTED_SECRET not in err, err
    # the whole stack, not one line: this is the text the conductor used to
    # merge verbatim.
    assert f"Error: boom {CANARY}" in err, err
    assert len([line for line in err.splitlines() if line.strip()]) > 1, err


# ---------------------------------------------------------------------------
# an unknown component is fatal (issue #2252, the ts half of #2200)
# ---------------------------------------------------------------------------

_UNDO_DOC = """
service Vault { fn show(user: Str) -> Str }
service Other { fn peek(user: Str) -> Str }

extern pure fn mark(what: Str) -> Str = @ts {
  process.getBuiltinModule("node:fs").writeSync(1, "MARK " + what + "\\n")
  return what
}

component Impl provides vault: Vault {
  let slot = effect mark("first-applied") undo mark("first-INVERSE-RAN")

  provide vault {
    fn show(user) { return "shown " + user }
  }
}

component Second provides other: Other {
  let slot = effect mark("second-applied") undo mark("second-INVERSE-RAN")

  provide other {
    fn peek(user) { return "peeked " + user }
  }
}
"""


@needs_node
def test_an_unknown_component_is_fatal_and_disposes_what_booted(tmp_path):
    """The spec names `Ghost`, which the bundle lacks, after `Impl` and `Second`,
    which it has. Before the fix `plug(ctx, undefined)` died on a TypeError that
    named nothing and left both undisposed. The run must exit 1 with exactly one
    FATAL line naming `Ghost`, claim neither `UP` nor `NO-RESIDUE` nor `DOWN`,
    and have really torn the booted components down, last first: their inverses
    ran, not just a log line."""
    module = _emitted(tmp_path, _UNDO_DOC)
    spec = tmp_path / "ghost.json"
    spec.write_text(json.dumps({
        "name": "vault", "module": str(module),
        "components": ["Impl", "Second", "Ghost"],
        "config": {}, "provides": ["vault", "other"], "proxies": {}, "probe": [],
        "once": True,
    }), encoding="utf-8")
    code, out, err = _run(spec)
    assert code == 1, (code, out, err)
    fatal = [ln for ln in err.splitlines() if "FATAL unknown component Ghost" in ln]
    assert len(fatal) == 1, err
    assert "TypeError" not in err and "isolate" not in err, err
    for claim in ("UP", "NO-RESIDUE", "DOWN"):
        assert f"] {claim}" not in out, out
    assert "MARK first-applied" in out and "MARK second-applied" in out, out
    assert out.index("MARK second-applied") < out.index("MARK second-INVERSE-RAN"), out
    assert out.index("MARK first-applied") < out.index("MARK first-INVERSE-RAN"), out
    # last first: Second's inverse and dispose line precede Impl's
    assert out.index("MARK second-INVERSE-RAN") < out.index("MARK first-INVERSE-RAN"), out
    lines = out.splitlines()
    disposed = [ln for ln in lines if "swap" in ln and "dispose" in ln]
    labels = [ln.split("|")[1].strip() for ln in disposed]
    assert labels == ["Second", "Impl"], (labels, out)
    assert not any("NOT confirmed" in ln for ln in disposed), out


_HANG_DOC = """
service Vault { fn show(user: Str) -> Str }

extern pure fn stall(what: Str) -> Str = @ts {
  return new Promise(() => {})
}

component Impl provides vault: Vault {
  let slot = effect stall("applied") undo stall("never")

  provide vault {
    fn show(user) { return "shown " + user }
  }
}
"""


@needs_node
def test_a_dispose_that_never_settles_still_reports_and_exits_1(tmp_path):
    """A dispose that holds no handle must not let the event loop drain: node
    would exit 13 with its own unredacted warning. `REVL_TEARDOWN_GRACE=1` keeps the
    process alive for the teardown bound, then the line says the dispose was not confirmed."""
    module = _emitted(tmp_path, _HANG_DOC)
    spec = tmp_path / "hang.json"
    spec.write_text(json.dumps({
        "name": "vault", "module": str(module), "components": ["Impl", "Ghost"],
        "config": {}, "provides": ["vault"], "proxies": {}, "probe": [],
        "once": True,
    }), encoding="utf-8")
    code, out, err = _run(spec, env={**os.environ, "REVL_TEARDOWN_GRACE": "1"})
    assert code == 1, (code, out, err)
    assert len([ln for ln in err.splitlines()
                if "FATAL unknown component Ghost" in ln]) == 1, err
    assert "NOT confirmed: timed out" in out, out
    assert "unsettled" not in err and "Warning" not in err, err


@needs_node
@pytest.mark.parametrize("once", [True, False], ids=["once", "held"])
def test_an_unconfirmed_dispose_is_fatal_and_claims_no_down(tmp_path, once):
    """Issue #2339: a dispose that does not settle inside the teardown bound is
    not a finished unwind, so the run must not say it was. `REVL_TEARDOWN_GRACE=1`
    shortens the bound to one second. Both the once-mode teardown and the held
    one (ended by SIGTERM, as the conductor's `stop_all` does) must name the
    fiber on one FATAL line on stderr, exit 1, and print neither a residue
    verdict nor `DOWN`, which `placement.py::_stop_all` reads as proof."""
    module = _emitted(tmp_path, _HANG_DOC)
    spec = tmp_path / "unconfirmed.json"
    spec.write_text(json.dumps({
        "name": "vault", "module": str(module), "components": ["Impl"],
        "config": {}, "provides": ["vault"], "proxies": {}, "probe": [],
        "once": once,
    }), encoding="utf-8")
    env = {**os.environ, "REVL_TEARDOWN_GRACE": "1"}
    proc = subprocess.Popen(
        ["node", str(_RUNNER), str(spec)], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=_HERE,
        env=env)
    head = ""
    try:
        if not once:
            # the held run tears down only when told to: wait for UP, then stop it
            for line in proc.stdout:
                head += line
                if "[vault] UP" in line:
                    break
            proc.send_signal(signal.SIGTERM)
        rest, err = proc.communicate(timeout=120)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    out = head + rest

    assert proc.returncode == 1, (proc.returncode, out, err)
    assert "[vault] UP" in out, out
    assert "dispose NOT confirmed" in out, out
    assert "] DOWN" not in out, out
    assert "NO-RESIDUE" not in out and "RESIDUE-LEFT" not in out, out
    fatal = [ln for ln in err.splitlines() if "] FATAL" in ln]
    assert len(fatal) == 1, err
    assert "teardown incomplete" in fatal[0] and "Impl" in fatal[0], err
    assert "unsettled" not in err and "Warning" not in err, err


# A correct inverse that outlasts the old 5 s default (issue #2356).
_LONG_DOC = """
service Vault { fn show(user: Str) -> Str }

extern pure fn nap(what: Str) -> Str = @ts {
  return new Promise((resolve) => setTimeout(() => resolve(what), what === 'late' ? 6000 : 0))
}

component Impl provides vault: Vault {
  let slot = effect nap("applied") undo nap("late")

  provide vault {
    fn show(user) { return "shown " + user }
  }
}
"""


@needs_node
def test_a_slow_correct_inverse_ends_in_down_with_the_default_bound(tmp_path):
    """With REVL_TEARDOWN_GRACE unset the runner waits as long as the conductor
    does (30 s less a margin), so a 6 s inverse is a clean teardown (#2356)."""
    module = _emitted(tmp_path, _LONG_DOC)
    spec = tmp_path / "long.json"
    spec.write_text(json.dumps({
        "name": "vault", "module": str(module), "components": ["Impl"],
        "config": {}, "provides": ["vault"], "proxies": {}, "probe": [],
        "once": True,
    }), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "REVL_TEARDOWN_GRACE"}
    proc = subprocess.Popen(
        ["node", str(_RUNNER), str(spec)], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=_HERE,
        env=env)
    started = time.monotonic()
    try:
        out, err = proc.communicate(timeout=120)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, (proc.returncode, out, err)
    assert "] DOWN" in out, (out, err)
    assert "FATAL" not in err, err
    assert elapsed >= 5.5, elapsed  # the inverse really outlasted the old 5 s bound
