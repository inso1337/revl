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
