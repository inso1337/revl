// revl cordis/TypeScript backend demo (docs/backend-ir.md §Acceptance item 2).
//
// Loads PgDatabase + UserCache from the emitted module, exercises
// cache.put/get, swaps the Database provider at runtime for a second provider
// instance, then unloads everything.  Prints an event log showing R2/R3
// ordering and asserts R1 (LIFO) + R4 (no residue).
//
// Run: node demo.ts   (erasable-syntax TypeScript; needs unflagged type
// stripping, so Node >= 23.6 or the >= 22.18 LTS backport. CI's
// `backend-typescript` job pins node-version 22, which resolves to the
// latest 22.x and clears that floor.)

import { Context, FiberState } from 'cordis'
import { PgDatabase, UserCache } from './golden/user_cache.ts'
import {
  assertNoResidue,
  fiberStateName,
  hostLog,
  onHostEvent,
  snapshotRuntime,
} from './runtime.ts'

const log: string[] = []
const note = (line: string) => log.push(line)

let failures = 0
function check(requirement: string, condition: boolean, detail: string): void {
  const verdict = condition ? 'OK  ' : 'FAIL'
  if (!condition) failures++
  note(`check | ${verdict} ${requirement} — ${detail}`)
}

/** Index of the first hostLog entry matching `pattern`, from `from`. */
function hostIndex(pattern: string, from = 0): number {
  for (let i = from; i < hostLog.length; i++) {
    if (hostLog[i].includes(pattern)) return i
  }
  return -1
}

const ctx = new Context()

onHostEvent((entry) => note(`host  | ${entry}`))
ctx.on('internal/status', (fiber, oldState) => {
  note(`fiber | ${fiber.name}: ${fiberStateName(oldState)} -> ${fiberStateName(fiber.state)}`)
})

const baseline = snapshotRuntime(ctx)

note('demo  | load PgDatabase (primary)')
const primary = ctx.plugin(PgDatabase, { url: 'pg://primary' })
await primary

note('demo  | load UserCache')
const cache = ctx.plugin(UserCache)
await cache

note('demo  | cache.put(alice, 42); cache.get(alice)')
ctx.cache.put('alice', '42')
check('R1', ctx.cache.get('alice') === '42', 'cache.get returns the stored value')

// --- R2/R3: swap the Database provider at runtime -------------------------
note('demo  | swap provider: dispose primary')
await primary.dispose()

// R3 — withdrawal ordering: the dependent (UserCache) must fully deactivate
// (its map dropped) before the provider's own pool closes.
const dropAt = hostIndex('.drop')
const closeAt = hostIndex('pool#1(pg://primary).close')
check('R3', dropAt !== -1 && closeAt !== -1 && dropAt < closeAt,
  `dependent teardown (hostLog[${dropAt}]) precedes provider pool.close (hostLog[${closeAt}])`)
check('R2', cache.state === FiberState.PENDING,
  'UserCache deactivated when its requirement was withdrawn')

note('demo  | swap provider: load PgDatabase (replica), second instance')
const replica = ctx.plugin(PgDatabase, { url: 'pg://replica', pool_size: 4n })
await replica
await cache.await()

check('R2', cache.state === FiberState.ACTIVE,
  'UserCache reactivated against the replacement provider')
// `Opt[Str]` is bare `value | undefined` on this tier (runtime.ts `MapHandle.get`,
// emit.py: "Map.get answers undefined when absent: exactly the Opt None case"),
// so the absent key reads as `undefined`. This assertion said `=== null`, which was
// correct until cabbd931 dropped the runtime's `?? null` — nothing ran the demo, so
// it rotted from that day. Deliberately `=== undefined` and not `== null`: on this
// tier `null` is NOT None (a `null` answer makes `None == None` false through
// revlEq, which is what cabbd931 fixed), so the loose form would wave that
// regression through instead of catching it.
check('R2', ctx.cache.get('alice') === undefined,
  'reactivated cache runs its effects afresh (new store)')

note('demo  | cache.put(bob, 7) against the replica-backed cache')
ctx.cache.put('bob', '7')
check('R1', ctx.cache.get('bob') === '7', 'cache works against replacement provider')

// --- unload everything ----------------------------------------------------
note('demo  | unload everything (dispose replica, then UserCache fiber)')
const unloadStart = hostLog.length
await replica.dispose()
await cache.dispose()

// R1 — LIFO recovery: the put-effect (insert bob) accumulated while ACTIVE is
// reverted (remove bob) before the activation-time effects (map drop), and the
// dependent's teardown precedes the provider's pool.close.
const removeAt = hostIndex('.remove(bob)', unloadStart)
const drop2At = hostIndex('.drop', unloadStart)
const close2At = hostIndex('pool#2(pg://replica).close', unloadStart)
check('R1', removeAt !== -1 && drop2At !== -1 && removeAt < drop2At,
  `method-effect undo (hostLog[${removeAt}]) precedes body-effect undo (hostLog[${drop2At}])`)
check('R3', drop2At !== -1 && close2At !== -1 && drop2At < close2At,
  `dependent fully deactivates (hostLog[${drop2At}]) before provider reverts its pool (hostLog[${close2At}])`)

// R4 — no residue: cordis introspection + host resource registry match the
// pre-load baseline exactly.
try {
  assertNoResidue(ctx, baseline)
  check('R4', true, 'runtime snapshot identical to baseline; no live host resources')
} catch (error) {
  check('R4', false, String(error))
}

// R5 is a property of the emitted code: provisions go through ctx.provide and
// withdrawal is the runtime's own disposer (see tests/semantics.test.ts).

console.log('revl cordis/TypeScript backend demo — event log')
console.log('='.repeat(64))
for (const [i, line] of log.entries()) {
  console.log(String(i).padStart(3), line)
}
console.log('='.repeat(64))
console.log(failures === 0 ? 'all demo checks passed' : `${failures} demo check(s) FAILED`)
process.exit(failures === 0 ? 0 : 1)
