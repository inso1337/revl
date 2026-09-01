// Test-only ambient shims for the Temporal TypeScript SDK. revl deliberately
// does NOT vendor @temporalio/workflow (docs/design/253-temporal-target.md §5:
// the SDK/cluster integration is out of Slice 1). These faithful minimal
// signatures let `tsc --noEmit` typecheck the emitted golden
// (golden/temporal_booktrip.ts) for shape correctness without the real package,
// so an emit regression still fails the backend-typescript gate. Not emitted by
// revl.
declare module "@temporalio/workflow" {
  export function proxyActivities<A>(options: unknown): A
  export function setHandler(def: unknown, handler: (...args: unknown[]) => unknown): void
  export function defineQuery<T>(name: string): { readonly __query: T; readonly name: string }
  export class ApplicationFailure extends Error {
    static create(options: unknown): ApplicationFailure
  }
}
