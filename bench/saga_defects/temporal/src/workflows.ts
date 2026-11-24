// The clean Temporal TypeScript ports of the revl sagas: the `checkout` saga the
// corpus seeds its defects into, and `examples/migrator.rvl`.
//
// They are written the way Temporal's own saga guidance writes them: a stack of
// compensations, each registered BEFORE the forward activity it undoes (so a
// forward activity that commits and then fails is still compensated), drained
// newest first, continuing past a compensation that raises.
//
// `await a.faultPoint(n, ...)` after each step is the failure point the harness
// injects. It is the counterpart of the `fail` step revl's sweep splices into
// the IR after step n, and it is what makes "fail after step n" the same
// failure model on both sides.
//
// Each row of the corpus is this file with one seeded change; the change is
// data, in `defects.json`. Do not edit the clean port to match a row.
import { proxyActivities } from '@temporalio/workflow'
import type * as activities from './activities'

// One attempt per activity, compensations included: a retry would re-run an
// effect and hide the seeded fault the harness injects.
const a = proxyActivities<typeof activities>({
  startToCloseTimeout: '10 seconds',
  retry: { maximumAttempts: 1 },
})

export interface SagaInput {
  failAfterStep?: number
}

type Compensation = () => Promise<unknown>

// Newest first; one compensation that raises must not strand the rest.
async function unwind(compensations: Compensation[]): Promise<void> {
  for (const compensate of compensations) {
    try {
      await compensate()
    } catch {
      // the activity journal records the failure; keep draining
    }
  }
}

export async function checkout(input: SagaInput = {}): Promise<void> {
  const compensations: Compensation[] = []
  try {
    // step 1: hold the stock
    compensations.unshift(() => a.release('sku-1', 1))
    await a.reserve('sku-1', 1)
    await a.faultPoint(1, input.failAfterStep)

    // step 2: authorize the payment
    compensations.unshift(() => a.refund('order-1', 4200))
    await a.charge('order-1', 4200)
    await a.faultPoint(2, input.failAfterStep)

    // step 3: settle (an irreversible emission, compensated)
    compensations.unshift(() => a.reverse('order-1', 4200))
    await a.settle('order-1', 4200)
    await a.faultPoint(3, input.failAfterStep)
  } catch (error) {
    await unwind(compensations)
    throw error
  }
}

// examples/migrator.rvl: a lock, an await, and an emission with a compensation.
export async function migrator(input: SagaInput = {}): Promise<void> {
  const compensations: Compensation[] = []
  try {
    // step 1: take the advisory lock
    compensations.unshift(() => a.lockRelease(42))
    await a.lockAcquire(42)
    await a.faultPoint(1, input.failAfterStep)

    // step 2: await the migration job
    await a.jobRun('migrations')
    await a.faultPoint(2, input.failAfterStep)

    // step 3: write the migration log row (an emission), compensated
    compensations.unshift(() => a.logDelete(42))
    await a.logInsert(42)
    await a.faultPoint(3, input.failAfterStep)
  } catch (error) {
    await unwind(compensations)
    throw error
  }
}
