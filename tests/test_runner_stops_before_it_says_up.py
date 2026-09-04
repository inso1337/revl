"""`[name] UP` must not be printable before the stop handlers are installed.

The conductor's `--once` path waits for every child's `[name] UP` line and then
calls `stop_all` immediately (`placement.run_placement`), so the SIGTERM can
land microseconds after that print. While `_process_runner.run` printed `UP`
*before* `loop.add_signal_handler(SIGTERM, ...)`, a signal arriving in that
window hit the DEFAULT disposition and killed the child outright: no LIFO
unwind, no inverses replayed, no residue proof, no `DOWN`.

The failure is silent as well as wrong. By the time `_stop_all` looks, the
child has already exited, so `proc.poll()` is not None, the kill path that
records a stranded child (issue 239) is never reached, and a placement that
tore down nothing exits 0. The window belongs to the LAST process to boot --
every earlier one is still being waited on -- which is why it was a consumer,
with an inverse and a residue proof still to run, that lost it.

Asserted on the ORDER IN THE SOURCE rather than by racing a real child,
because racing it is precisely what cannot be done reliably: the window is
microseconds wide and a test that lost the race would pass against the bug.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import _process_runner  # noqa: E402


def test_the_stop_handlers_are_installed_before_the_up_line():
    src = inspect.getsource(_process_runner.run)
    handler = src.index("add_signal_handler")
    up = src.index('print(f"[{name}] UP"')
    assert handler < up, (
        "`[name] UP` is printed before SIGTERM is handled: the conductor stops "
        "the placement the instant it reads that line, so a signal in the "
        "window kills the child before it can unwind")


def test_both_stop_signals_are_covered():
    """SIGINT too: an operator's Ctrl-C on an interactive placement is the same
    event, and a teardown that only survives one of the two is half a
    guarantee."""
    src = inspect.getsource(_process_runner.run)
    handler_block = src[:src.index('print(f"[{name}] UP"')]
    assert "signal.SIGTERM" in handler_block
    assert "signal.SIGINT" in handler_block


# --------------------------------------------------------------------------
# The same guarantee on the ts, go and java tiers (issue 290).
#
# The py fix above (#226) left the identical window open on three more
# runners: each printed `[<name>] UP` before installing its stop disposition,
# and the `--once` conductor calls `stop_all` the instant it reads that line,
# so a stop landing in the window hit the DEFAULT disposition (node: SIGTERM
# kills; go: SIGTERM kills; JVM: no shutdown hook registered means the JVM
# dies without running one) and killed the child outright.
#
# Since #283 that death is at least AUDIBLE -- `_stop_all` reports any child
# that exited without ever saying `DOWN` as stranded -- but audible is not
# fixed, and the guarantee is the ordering.
#
# ASSERTED ON THE ORDER IN THE SOURCE, for the same reason the py test above
# is: the window is microseconds wide, and a test that lost the race would
# pass against the bug. This is the documented exception to issue 280's "do
# not assert a call site appears in the source" rule -- do not "fix" these
# into behavioural tests, because the behavioural test cannot be written.
#
# What issue 280 is actually right about is that a bare substring search is
# satisfied by a MENTION: the symbol appearing in a comment (these runners
# carry long comments that name the very constructs being ordered) or inside
# a string literal makes the comparison hold wherever the real call sits. So
# every search below runs against source with comments STRIPPED, and matches
# a whole statement anchored to its own line -- something only the real
# construct can be. `test_the_ordering_search_cannot_be_satisfied_by_a_comment`
# pins that property directly.
# --------------------------------------------------------------------------

import re  # noqa: E402

BACKENDS = ROOT / "backends"

TS_RUNNER = BACKENDS / "typescript" / "placement_runner.ts"
GO_RUNNER = BACKENDS / "go" / "placement_runner" / "main.go"
JAVA_STUB_RUNNER = BACKENDS / "java" / "placement" / "PlacementRunner.java"
JAVA_REAL_RUNNER = BACKENDS / "java" / "placement" / "RealPlacementRunner.java"
RUST_RUNNER_DIR = BACKENDS / "rust" / "placement_runner"


def strip_comments(src: str) -> str:
    """Blank out `//` and `/* */` comments in C-family source (ts/go/java/rs).

    String literals are walked, not stripped: the `UP` line being searched for
    IS a string literal, so removing them would remove the target. Walking them
    is still necessary so a `//` inside a string does not start a comment.
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


def only_offset(pattern: str, code: str, what: str) -> int:
    """Offset of the one and only line matching `pattern`.

    Requiring exactly one match is half the non-vacuity: a pattern that starts
    matching something else (a second call site, a moved line left behind)
    fails loudly here rather than silently comparing the wrong pair.
    """
    hits = list(re.finditer(pattern, code, re.MULTILINE))
    assert len(hits) == 1, (
        f"expected exactly one {what} in the runner source, found {len(hits)}"
        f" for /{pattern}/")
    return hits[0].start()


def assert_stop_installed_before_up(path, handler_re, up_re, handler_what):
    code = strip_comments(path.read_text(encoding="utf-8"))
    handler = only_offset(handler_re, code, handler_what)
    up = only_offset(up_re, code, "`UP` print")
    assert handler < up, (
        f"{path.name} prints `[name] UP` before installing {handler_what}: the "
        "conductor stops the placement the instant it reads that line, so a "
        "stop arriving in the window hits the default disposition and kills "
        "the child before it can unwind (G7) or prove no residue (R4)")


def test_ts_installs_its_stop_handlers_before_the_up_line():
    """`teardown` closes over `let stopping`, so the whole definition had to
    move above the print, not just the two `process.on` calls: a handler
    installed while that binding was still in its temporal dead zone would
    throw instead of tearing down."""
    code = strip_comments(TS_RUNNER.read_text(encoding="utf-8"))
    up = only_offset(r"^[ \t]*console\.log\(`\[\$\{name\}\] UP`\)$", code,
                     "`UP` print")
    for sig in ("SIGTERM", "SIGINT"):
        handler = only_offset(rf"^[ \t]*process\.on\('{sig}', teardown\)$", code,
                              f"process.on('{sig}') registration")
        assert handler < up, (
            f"placement_runner.ts prints `[name] UP` before handling {sig}")
    # and the closure it reads is initialised above the handlers, not left in
    # the TDZ the reorder had to solve for.
    stopping = only_offset(r"^let stopping = false$", code, "`stopping` binding")
    assert stopping < up


def test_go_installs_its_stop_handler_before_the_up_line():
    """`signal.Notify` could not move up alone: it feeds channels the `select`
    consumes, and the `select` blocks so it must stay below the print. The
    whole rendezvous is hoisted and only the `select` is left behind; both
    channels are buffered, so a signal in the old window is parked and the
    `select` returns from it at once."""
    assert_stop_installed_before_up(
        GO_RUNNER,
        r"^\t*signal\.Notify\(sig, syscall\.SIGTERM, syscall\.SIGINT\)$",
        r'^\t*fmt\.Printf\("\[%s\] UP\\n", name\)$',
        "signal.Notify(SIGTERM, SIGINT)")


def test_java_stub_runner_installs_its_shutdown_hook_before_the_up_line():
    """The hook body reads `stubRef`, so the effectively-final capture of
    `stub` moves up with the registration; only the blocking `latch.await()`
    stays below the print."""
    assert_stop_installed_before_up(
        JAVA_STUB_RUNNER,
        r"^[ \t]*Runtime\.getRuntime\(\)\.addShutdownHook\(",
        r'^[ \t]*System\.out\.println\("\[" \+ name \+ "\] UP"\);$',
        "addShutdownHook registration")


def test_java_real_runner_installs_its_shutdown_hook_before_the_up_line():
    """`events` is an unbounded queue, so a STOP offered in the old window is
    parked and the event loop below takes it on its first iteration."""
    assert_stop_installed_before_up(
        JAVA_REAL_RUNNER,
        r"^[ \t]*Runtime\.getRuntime\(\)\.addShutdownHook\(",
        r'^[ \t]*System\.out\.println\("\[" \+ name \+ "\] UP"\);$',
        "addShutdownHook registration")


def test_rust_has_no_window_to_close():
    """Rust is exempt, and this pins WHY rather than taking it on trust.

    It installs no signal disposition at all: `_stop_all` stops it by CLOSING
    ITS STDIN (`stop_mode == "stdin"`, placement.py). That is a state, not an
    edge -- the pipe stays at EOF -- so the runner's stdin reader observes it
    whenever it gets around to reading, and there is no instant at which a
    stop can be lost. If a rust signal handler is ever added, this test fails
    and the ordering above has to be established there too.
    """
    # Searched RAW here, comments included: the claim is the stronger one that
    # the crate does not so much as NAME a signal API, which needs no stripper
    # (and rust lifetimes would confuse a C-family one).
    for path in sorted(RUST_RUNNER_DIR.rglob("*.rs")):
        code = path.read_text(encoding="utf-8")
        for api in ("signal_hook", "sigaction", "ctrlc", "libc::SIG",
                    "signal::", "set_handler"):
            assert api not in code, (
                f"{path} now installs a signal disposition ({api}): the rust "
                "runner is no longer exempt from issue 290, and the handler "
                "must go up before its `UP` print")
    manifest = (RUST_RUNNER_DIR / "Cargo.toml").read_text(encoding="utf-8")
    for crate in ("signal-hook", "ctrlc", "libc", "nix"):
        assert crate not in manifest

    placement = (ROOT / "src" / "revl" / "placement.py").read_text(encoding="utf-8")
    assert '"stdin"' in placement


def test_the_ordering_search_cannot_be_satisfied_by_a_comment():
    """The non-vacuity pin for every assertion above.

    Issue 280's finding was a source assertion satisfied by a MENTION -- the
    symbol sitting in an explanatory comment, so the comparison held wherever
    the real call was. These runners are exactly that hazard: each carries a
    long comment above the print that names the construct being ordered. This
    proves the stripper removes such a mention (so it cannot stand in for the
    call) while leaving real code, including the `UP` string literal, intact.
    """
    fake = (
        "// process.on('SIGTERM', teardown) is installed below\n"
        "/* signal.Notify(sig, syscall.SIGTERM, syscall.SIGINT) */\n"
        "console.log(`[${name}] UP`)\n"
        "process.on('SIGTERM', teardown)\n"
    )
    code = strip_comments(fake)
    assert code.count("process.on('SIGTERM', teardown)") == 1
    assert "signal.Notify" not in code
    assert "console.log(`[${name}] UP`)" in code
    # the mention was the FIRST occurrence in the raw text; had it survived,
    # a naive search would have reported the handler as already ordered.
    assert fake.index("process.on('SIGTERM', teardown)") < fake.index("UP")
    assert code.index("process.on('SIGTERM', teardown)") > code.index("UP")
    # a `//` inside a string is not a comment, and is not eaten.
    assert strip_comments('const u = "http://x" // c\n').startswith(
        'const u = "http://x"')
