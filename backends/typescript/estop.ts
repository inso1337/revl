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
