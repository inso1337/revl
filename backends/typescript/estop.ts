// The operator E-Stop's shared vocabulary on the ts tier — roadmap item 443,
// issue #122. The node twin of `src/revl/estop.py`.
//
// `docs/design/443-estop.md` is the reasoning of record. Item 443 landed the
// halt on the py reference tier: a latch file, a crossing seam that refuses
// once it is armed, and an in-flight inventory. The five non-py tiers kept
// their cooperative teardown and had NO E-Stop, so a placement halt SIGKILLed
// them and reported their residue UNKNOWN. This module is the first half of
// the node tier honoring the latch: the latch reader, and the check the
// crossing seam consults so it stops dispatching NEW crossings the instant an
// operator arms it (`bridge.ts::serve`).
//
// The three things here MUST read IDENTICALLY to the py reader
// (`src/revl/estop.py::read_latch`, `backends/python/runtime.py::_latch_record`)
// so the two tiers cannot drift on what an armed — or a malformed — latch means:
//
//   * where the latch file is (`latchPath`);
//   * what an armed latch means, including a malformed one (`readLatch`);
//   * whether a halt is in force right now (`estopEngaged`).

import fs from 'node:fs'

/** The ambient latch path, equivalent to `--estop-latch FILE`. The conductor
 *  (`src/revl/placement.py`) exports this to every child regardless of tier;
 *  before this module a node child inherited it and ignored it. */
export const LATCH_ENV = 'REVL_ESTOP_LATCH'

/** What a latch-honoring child prints when the latch trips: its own in-flight
 *  inventory, on one line, so the conductor merges it without a second
 *  channel. Kept identical to `estop.py::HALTED_LINE`. */
export const HALTED_LINE = 'HALTED'

/** The latch record an operator armed. `halted` is implicit — the file's mere
 *  presence is the halt — and the rest is carried into the report by name. */
export interface LatchRecord {
  halted?: boolean
  reason?: string
  operator?: string
  [key: string]: unknown
}

/** The latch file to act on: an explicit path, else `<wal>.estop`, else the
 *  ambient `REVL_ESTOP_LATCH`. Mirrors `estop.py::latch_path` — deriving it
 *  from the WAL is not a convenience but the durable rendezvous the
 *  reconciliation path (`revl recover --wal`) already names. */
export function latchPath(
  latch: string | null = null,
  wal: string | null = null,
  env = true,
): string | null {
  if (latch) return latch
  if (wal) return `${wal}.estop`
  if (env) return process.env[LATCH_ENV] || null
  return null
}

/** The halt an operator wrote at `path`, or null when the latch is absent.
 *
 *  A latch that EXISTS but does not parse still reads as HALTED. Failing open
 *  on a malformed emergency stop is the one failure mode this feature exists
 *  to prevent, so every reader — the py runtime seam, the CLI, the conductor,
 *  and now this — applies the same rule. A latch the OS refuses to open at all
 *  (a missing file, a permission error) reads as absent, matching
 *  `estop.py::read_latch` (`FileNotFoundError`/`OSError` -> None). */
export function readLatch(path: string | null): LatchRecord | null {
  if (!path) return null
  let text: string
  try {
    text = fs.readFileSync(path, 'utf8')
  } catch {
    // ENOENT or any other OS-level read failure: the latch is not readable, so
    // it is not a halt. (A malformed BUT readable latch is handled below.)
    return null
  }
  let record: unknown
  try {
    record = JSON.parse(text)
  } catch {
    return unreadable()
  }
  return record !== null && typeof record === 'object' && !Array.isArray(record)
    ? (record as LatchRecord)
    : unreadable()
}

function unreadable(): LatchRecord {
  return {
    halted: true,
    reason: 'operator halt (unreadable latch)',
    operator: 'unknown',
  }
}

/** Whether a halt is in force on the latch this process watches. `bridge.ts`'s
 *  serve seam consults this on each incoming crossing: the cost is one
 *  `readFileSync` per crossing WHILE a latch is armed, and nothing at all when
 *  none is — the default — because `latchPath` short-circuits to null. */
export function estopEngaged(path: string | null = latchPath()): boolean {
  return readLatch(path) !== null
}

/** One boundary crossing that is still executing, as the accept seam records it
 *  (`bridge.ts::inFlightCrossings`). A crossing in this set when the button is
 *  hit is the ambiguous one: its at-most-once attempt may or may not have
 *  landed (item 440). */
export interface Crossing {
  seq: number
  key: string
  method: string
  direction: 'accept' | 'dispatch'
}

/** The halt inventory this process prints when the latch trips its seams, in
 *  the merged residue schema the conductor's report reads
 *  (`src/revl/placement.py::_estop_halt_report`), byte-compatible with the shape
 *  the py runner emits (`backends/python/runtime.py`).
 *
 *  A crossing still executing when the operator armed the latch is AMBIGUOUS —
 *  its at-most-once attempt may or may not have landed (item 440) — which is the
 *  designed outcome of an operator halt, not an edge case. This tier keeps no
 *  witnessed-inverse ledger, so `stranded` is empty and HONESTLY so: the halt
 *  reports what it can name (the crossings in flight) rather than inventing a
 *  book it does not keep, and the conductor never reads that empty list as
 *  `nothing was owed` because the tier still reports the ambiguous crossings it
 *  had. */
export function estopInventory(
  process: string,
  crossings: Crossing[],
  record: LatchRecord | null,
): Record<string, unknown> {
  const inFlight = crossings.map((crossing) => ({
    kind: 'estop-ambiguous',
    state: 'unresolved',
    component: crossing.key,
    method: crossing.method,
    seq: crossing.seq,
    entry: 'crossing',
    direction: crossing.direction,
    attemptedFlag: true,
    outcome: 'unknown',
  }))
  return {
    process,
    verdict: 'halted',
    reason: record?.reason ?? 'operator halt',
    operator: record?.operator ?? 'unknown',
    activations: [],
    inFlight,
    stranded: [],
    resumable: false,
  }
}

/** The single line a latch-honoring child prints when the button is hit:
 *  `[name] HALTED {inventory}`. The conductor parses it off stdout by the
 *  `HALTED_LINE` prefix (`src/revl/placement.py::pump`) and merges the inventory
 *  into the halt report without a second channel — the exact contract the py
 *  runner already meets. */
export function estopHaltLine(
  process: string,
  crossings: Crossing[],
  record: LatchRecord | null,
): string {
  return `[${process}] ${HALTED_LINE} ${JSON.stringify(estopInventory(process, crossings, record))}`
}
