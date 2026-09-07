"""The operator E-Stop's shared vocabulary — roadmap item 443.

`docs/design/443-estop.md` is the reasoning of record. This module holds the
three things that must read IDENTICALLY everywhere the halt is observed, so
the CLI (`revl estop`), the multi-process conductor (`revl run --placement`)
and the py runtime (`backends/python/runtime.py`) cannot drift apart on them:

  * where the latch file is (`latch_path`);
  * what an armed latch means, including a malformed one (`read_latch`);
  * which tiers actually HONOR the latch (`TIERS_WITH_ESTOP`).

The third is the honest half. Five of revl's six tiers have no E-Stop seam,
and a conductor that halted a placement without saying which of its processes
were merely KILLED would be reporting a stop it did not perform.
"""

from __future__ import annotations

import json
import os

#: The ambient latch path, equivalent to `--estop-latch FILE`.
LATCH_ENV = "REVL_ESTOP_LATCH"

#: The tiers whose runtime checks the latch at every boundary-crossing seam and
#: can name its in-flight inventory when the button is hit.
#:
#: `py` was the reference tier (item 443). `node` joins it here (issue #122,
#: docs/design/443-estop-ts-tier.md): its crossing seams refuse a new crossing
#: the instant the latch is armed (`backends/typescript/bridge.ts`), the
#: placement runner watches the latch even while idle and names what was in
#: flight (`backends/typescript/placement_runner.ts`), and the latch reader is
#: the byte-for-byte twin of `read_latch` below (`backends/typescript/estop.ts`).
#:
#: A component on any OTHER tier (`rust`, `go`, `java`, `wasm`) keeps its
#: cooperative teardown and has no E-Stop: the only halt available for it is a
#: SIGKILL, which unwinds nothing and leaves its residue UNKNOWN. That is honest
#: and visible rather than a silently degraded halt, and `_estop_halt_report`
#: names every such component individually (docs/design/443-estop.md,
#: "Per-tier status").
TIERS_WITH_ESTOP = frozenset({"py", "node"})

#: What the py runner prints when the latch trips: its own in-flight
#: inventory, on one line, so the conductor can merge it into the halt report
#: without a second channel.
HALTED_LINE = "HALTED"


def latch_path(latch: str | None = None, wal: str | None = None,
               env: bool = True) -> str | None:
    """The latch file to act on: `--latch`, else `<wal>.estop`, else the
    ambient `REVL_ESTOP_LATCH`.

    Deriving it from the WAL is not a convenience: the WAL is the durable
    rendezvous the reconciliation path already uses (`revl recover --wal`), so
    a halt and its reconciliation name the same session with one argument."""
    if latch:
        return latch
    if wal:
        return f"{wal}.estop"
    if env:
        return os.environ.get(LATCH_ENV) or None
    return None


def read_latch(path: str | None) -> dict | None:
    """The halt an operator armed at `path`, or None when the latch is absent.

    A latch that exists but does not parse still reads as HALTED. Failing open
    on a malformed emergency stop is the one failure mode this feature exists
    to prevent, so every reader — the runtime seam, the CLI and the conductor —
    applies this same rule."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    except (ValueError, TypeError):
        return _unreadable()
    return record if isinstance(record, dict) else _unreadable()


def _unreadable() -> dict:
    return {"halted": True, "reason": "operator halt (unreadable latch)",
            "operator": "unknown"}
