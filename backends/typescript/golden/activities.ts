// Test-only stub of the activity module the emitted Temporal workflow imports
// (`import type * as activities from './activities'`). In a real deployment the
// activities are host implementations that live outside revl; this stub exposes
// the faithful signatures the golden (golden/temporal_booktrip.ts) proxies and
// destructures, so `tsc --noEmit` can typecheck the workflow against a concrete
// activity shape. Not emitted by revl.
type SagaReport = { outstanding: Record<string, unknown>[]; worldRemaining: number; proof: string }

export function flightsCancel(key: string): Promise<string> { return Promise.resolve(key) }
export function flightsReserve(itinerary: string): Promise<string> { return Promise.resolve(itinerary) }
export function paymentsCharge(card: string, total: bigint): Promise<string> { return Promise.resolve(`${card}:${total}`) }
export function paymentsRefund(card: string, total: bigint): Promise<string> { return Promise.resolve(`${card}:${total}`) }
export function settle(card: string, total: bigint): Promise<string> { return Promise.resolve(`${card}:${total}`) }
export function recordResidue(report: SagaReport): Promise<void> { void report; return Promise.resolve() }
