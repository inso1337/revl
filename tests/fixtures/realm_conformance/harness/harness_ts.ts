// cordis (TypeScript) realm-conformance runner (driven by
// tests/test_realm_conformance.py). Same contract as harness_py.py, on the
// real cordis v4 runtime. `__RUNTIME__` is replaced by the test with an
// absolute path to backends/typescript/runtime.ts before execution.
import { Context, FiberState } from 'cordis'
import { plug } from '__RUNTIME__'
import { SharedStoreA } from './a.ts'
import { SharedStoreB } from './b.ts'
import { SharedStoreOther } from './other.ts'

async function main() {
  // (H) two providers of kv in the SAME realm string -> second is refused.
  const root = new Context()
  await plug(root, SharedStoreA)
  let hVerdict = 'BOTH_ACTIVE'
  let detail = ''
  try {
    const b = await plug(root, SharedStoreB)
    if (b.state !== FiberState.ACTIVE) hVerdict = 'REFUSED'
  } catch (e) {
    hVerdict = 'REFUSED'
    detail = String(e).slice(0, 200)
  }

  // (S) two providers in DIFFERENT realm strings -> distinct, independent.
  const root2 = new Context()
  const a2 = await plug(root2, SharedStoreA)
  const o2 = await plug(root2, SharedStoreOther)
  const bothActive = a2.state === FiberState.ACTIVE && o2.state === FiberState.ACTIVE
  await a2.dispose()
  const otherSurvived = o2.state === FiberState.ACTIVE
  const sVerdict = bothActive && otherSurvived ? 'SEPARATE' : 'FAIL'

  console.log(
    'RC_JSON ' +
      JSON.stringify({
        tier: 'cordis',
        H: { verdict: hVerdict, detail },
        S: { verdict: sVerdict },
      }),
  )
}

main().catch((e) => {
  console.log('RC_ERROR ' + String(e))
  process.exitCode = 1
})
