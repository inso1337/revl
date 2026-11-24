// The fake world the activities act on, and the journal of what they did.
//
// A Temporal saga test runs the workflow against activities backed by a fake
// service, then reads the fake's state. This module is that fake. It lives in
// one module so the worker (which runs the activities) and the harness (which
// reads the outcome) see the same objects: both run in one process.
//
// Three stores are reversible resources and count as residue when they are not
// empty after a failed saga: the stock held for the order, the payment
// authorization, and the advisory lock. Two are irreversible emissions and do
// not: the settlement ledger and the migration log. An emission stands once it
// has happened; what the saga owes for it is a compensation, not an inverse.

export interface Call {
  name: string
  args: unknown[]
  ok: boolean
}

export const inventory = new Map<string, number>()
export const payments = new Map<string, number>()
export const locks = new Set<number>()
export const settled: string[] = []
export const reversed: string[] = []
export const migrationLog = new Set<number>()
export const journal: Call[] = []

export function reset(): void {
  inventory.clear()
  payments.clear()
  locks.clear()
  settled.length = 0
  reversed.length = 0
  migrationLog.clear()
  journal.length = 0
}

export function snapshot(): Record<string, unknown> {
  return {
    inventory: [...inventory.entries()],
    payments: [...payments.entries()],
    locks: [...locks],
    settled: [...settled],
    reversed: [...reversed],
    migrationLog: [...migrationLog],
    journal: journal.map((call) => ({ ...call })),
  }
}
