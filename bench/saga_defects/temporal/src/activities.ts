// The activities of the checkout and migrator sagas: the operator's side of the
// Temporal boundary. Every external effect a workflow has lives here.
//
// Each activity records itself in the journal and marks the entry failed when
// it throws, so the harness can tell a compensation that ran from one that
// raised. Only functions are exported: the worker treats every export as an
// activity.
import * as state from './state'

function call<T>(name: string, args: unknown[], body: () => T): T {
  const entry: state.Call = { name, args, ok: true }
  state.journal.push(entry)
  try {
    return body()
  } catch (error) {
    entry.ok = false
    throw error
  }
}

// ---- checkout: an inventory hold, a payment authorization, a settlement

export async function reserve(sku: string, qty: number): Promise<void> {
  call('reserve', [sku, qty], () => {
    state.inventory.set(sku, qty)
  })
}

export async function release(sku: string, qty: number): Promise<void> {
  call('release', [sku, qty], () => {
    state.inventory.delete(sku)
  })
}

export async function charge(order: string, cents: number): Promise<void> {
  call('charge', [order, cents], () => {
    state.payments.set(order, cents)
  })
}

// A refund subtracts from the held amount, so half a refund leaves half behind.
export async function refund(order: string, cents: number): Promise<void> {
  call('refund', [order, cents], () => {
    const held = state.payments.get(order) ?? 0
    if (held === cents) {
      state.payments.delete(order)
    } else {
      state.payments.set(order, held - cents)
    }
  })
}

// The settlement is an emission: it stands once made.
export async function settle(order: string, cents: number): Promise<void> {
  call('settle', [order, cents], () => {
    state.settled.push(order)
  })
}

// The compensation of the settlement. It records that the settlement is owed a
// reversal; the settlement itself stands.
export async function reverse(order: string, cents: number): Promise<void> {
  call('reverse', [order, cents], () => {
    state.reversed.push(order)
  })
}

// ---- migrator: an advisory lock, a job, a migration log row

export async function lockAcquire(id: number): Promise<void> {
  call('lockAcquire', [id], () => {
    state.locks.add(id)
  })
}

export async function lockRelease(id: number): Promise<void> {
  call('lockRelease', [id], () => {
    state.locks.delete(id)
  })
}

export async function jobRun(name: string): Promise<void> {
  call('jobRun', [name], () => undefined)
}

export async function logInsert(id: number): Promise<void> {
  call('logInsert', [id], () => {
    state.migrationLog.add(id)
  })
}

// The compensation of the log row: it deletes the row the emission wrote.
export async function logDelete(id: number): Promise<void> {
  call('logDelete', [id], () => {
    state.migrationLog.delete(id)
  })
}

// ---- the failure point the harness injects, after step `step` has completed

export async function faultPoint(step: number, failAfter: number | undefined): Promise<void> {
  call('faultPoint', [step], () => {
    if (failAfter === step) {
      throw new Error(`injected fault after step ${step}`)
    }
  })
}
