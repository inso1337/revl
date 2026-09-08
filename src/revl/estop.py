"""The operator E-Stop's shared vocabulary — roadmap item 443.

`docs/design/443-estop.md` is the reasoning of record. This module holds the
three things that must read IDENTICALLY everywhere the halt is observed, so
the CLI (`revl estop`), the multi-process conductor (`revl run --placement`)
and the py runtime (`backends/python/runtime.py`) cannot drift apart on them:

  * where the latch file is (`latch_path`);
  * what an armed latch means, including a malformed one (`read_latch`);
  * which tiers actually HONOR the latch (`TIERS_WITH_ESTOP`).

The third is the honest half. The tiers that still have no E-Stop seam keep
their cooperative teardown, and a conductor that halted a placement without
saying which of its processes were merely KILLED would be reporting a stop it
did not perform.
"""

from __future__ import annotations

import json
import os

#: The ambient latch path, equivalent to `--estop-latch FILE`.
LATCH_ENV = "REVL_ESTOP_LATCH"

#: The tiers whose runtime checks the latch at every boundary-crossing seam:
#: the py reference tier (item 443), the go and rust tiers (issue #122), and now
#: the java tier (issue #122, per docs/design/443-estop-tier-contract.md), whose
#: placement runners read the latch, refuse a new crossing at the accept and
#: dispatch seams, record what is in flight, and — via an idle watcher — print
#: their inventory and die where they stand rather than being SIGKILLed. Both
#: java runners honor it: the JDK-17 stub `PlacementRunner` and the reactive
#: cordis4j `RealPlacementRunner` (backends/java/placement/Estop.java).
#: A component on any OTHER tier (wasm, and — until #769 lands — node/ts) keeps
#: its cooperative teardown and has no runtime E-Stop seam: the only halt
#: available for it is a SIGKILL, which unwinds nothing and leaves its residue
#: UNKNOWN. That is honest and visible rather than a silently degraded halt, and
#: `_estop_halt_report` names every such component individually. wasm cannot run
#: a watcher (no process, no clock) and is instead reported STATICALLY from its
#: compile-time teardown section, a separate population from this set
#: (docs/design/443-estop-tier-contract.md, "Per-tier decisions").
TIERS_WITH_ESTOP = frozenset({"py", "go", "rust", "java"})

#: The tiers reported STATICALLY rather than by honoring the latch at runtime
#: (docs/design/443-estop-tier-contract.md, "Per-tier decisions"). A wasm
#: instance has no process, no clock and no file access, so it cannot run the
#: E2 crossing seam or the E6 watcher a honoring tier does — its embedder is
#: the halt authority, and halting it is dropping the instance without running
#: its teardown. But its inventory is knowable from OUTSIDE, at compile time:
#: the `revl:teardown` custom section (item 243 Slice 2b) enumerates every
#: activation-registered `transactional`/`compensation` descriptor, so the
#: conductor or embedder prints the instance's inventory on its behalf with
#: those entries as `estop-stranded` under the `static` population. That is a
#: strict improvement over UNKNOWN — the SIGKILL population every other
#: seamless tier falls in — for the one tier that cannot watch, and it costs no
#: runtime seam. A wasm host that later exposes a clock and a latch read can
#: move to `TIERS_WITH_ESTOP` under the same E1–E8; nothing here forecloses it.
TIERS_STATIC_ESTOP = frozenset({"wasm"})

#: The three E-Stop populations a tier's halt can be reported in, the label
#: `tier_estop_status` returns and the disposition the conductor/embedder uses.
ESTOP_HONORING = "honoring"
ESTOP_STATIC = "static"
ESTOP_UNKNOWN = "unknown"


def tier_estop_status(tier: str) -> str:
    """Which E-Stop population `tier` is reported in — the honest three-way
    split of docs/design/443-estop-tier-contract.md.

      * ``"honoring"`` — the runtime reads the latch at every crossing seam,
        halts at its own seams and names its own in-flight inventory
        (`TIERS_WITH_ESTOP`: py, go, rust, java);
      * ``"static"`` — the runtime cannot watch, but its inventory is projected
        from its compile-time teardown section (`TIERS_STATIC_ESTOP`: wasm);
      * ``"unknown"`` — no seam and no static projection, so a halt is a
        SIGKILL and the residue is UNKNOWN (node/ts until #769, and any tier
        with neither honoring nor a static section).

    A `static` tier is deliberately DISTINCT from both: it neither honors the
    latch (it runs no seam) nor is UNKNOWN (its residue IS named, from the
    module rather than from the dead runtime)."""
    if tier in TIERS_WITH_ESTOP:
        return ESTOP_HONORING
    if tier in TIERS_STATIC_ESTOP:
        return ESTOP_STATIC
    return ESTOP_UNKNOWN


def _static_stranded_record(*, component: str | None, method: str | None,
                            seq, entry_kind: str, reason: str) -> dict:
    """One `estop-stranded` residue fact projected STATICALLY, in the merged
    residue schema (docs/design/teardown-contract.md), tagged with the
    ``population: "static"`` marker so a reader can tell it apart from an entry
    a honoring runtime named from a live frame.

    It is `not-attempted` with a null `attempted.call`, exactly like the
    runtime's own `estop-stranded` (`backends/python/runtime.py`,
    `_estop_record`): the halt ran nothing, and this projection knows the entry
    exists but not the runtime argument/witness values the section deliberately
    does not carry (backends/wasm/emit.py, `_teardown_section`), so `method`
    and `referent` are honestly left null rather than faked."""
    return {
        "kind": "estop-stranded",
        "state": "unresolved",
        "population": "static",
        "component": component,
        "method": method,
        "seq": seq,
        "entry": entry_kind,
        "attemptedFlag": False,
        "attempted": None,
        "outcome": "not-attempted",
        "referent": None,
        "error": {
            "type": "estop",
            "message": (f"operator halt: {reason} — this instance was dropped "
                        "without running its teardown; the entry is known "
                        "statically from `revl:teardown` and is still owed"),
        },
        "hint": "replayed by `revl recover --wal <file>` from its WAL descriptor",
    }


def static_halt_inventory(entries, *, name: str, reason: str = "operator halt",
                          operator: str = "unknown", at=None) -> dict:
    """Project a compile-time teardown descriptor's `entries` into a halt
    inventory in the same shape a honoring runner prints (its `estop_report`),
    so the conductor or embedder can merge it BY NAME with no second channel
    (docs/design/443-estop-tier-contract.md, the wasm row).

    `entries` is the `revl:teardown` section's `entries` list — each
    ``{"seq": int, "entry": "transactional"|"compensation", "dispatch": int}``.
    Every one becomes an `estop-stranded (static)` record: the instance was
    dropped, its teardown never ran, and the obligation is still owed. There is
    no `estop-ambiguous` record here, and one is never invented — a wasm
    crossing goes through a HOST import, so the host's own in-flight registry
    (E4) names the at-most-one ambiguous crossing, not this static projection,
    which leaves `inFlight` empty by construction."""
    reason = reason or "operator halt"
    stranded = [
        _static_stranded_record(
            component=name, method=None, seq=entry.get("seq"),
            entry_kind=entry.get("entry") or "crossing", reason=reason)
        for entry in (entries or [])
    ]
    return {
        "process": name,
        "halted": True,
        "verdict": "halted",
        "population": "static",
        "reason": reason,
        "operator": operator or "unknown",
        "at": at,
        "activations": ([{"component": name, "stranded": len(stranded)}]
                        if stranded else []),
        "inFlight": [],
        "stranded": stranded,
        "resumable": False,
        "reconcile": "revl recover --wal <file>",
    }


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
    applies this same rule. The same reasoning covers a latch we cannot READ:
    an EACCES on a latch whose permissions changed, or an EISDIR on a path an
    operator turned into a directory, is an existing-but-unreadable latch, not
    an absent one, so it too reads as HALTED. Only a genuinely absent latch
    (`FileNotFoundError`) reads as not-halted; every other `OSError` fails
    CLOSED, because an operator halt no reader can inspect must never be
    mistaken for the absence of a halt."""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError:
        return None
    except OSError:
        return _unreadable()
    except (ValueError, TypeError):
        return _unreadable()
    return record if isinstance(record, dict) else _unreadable()


def _unreadable() -> dict:
    return {"halted": True, "reason": "operator halt (unreadable latch)",
            "operator": "unknown"}
