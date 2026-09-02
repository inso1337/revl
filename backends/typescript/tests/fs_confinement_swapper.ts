// The competing writer `fs_confinement_escapes.test.ts` races the witnessed
// write against (roadmap item 422 F4). Not a `.test.ts` file, so vitest's
// `include` glob does not collect it; it is loaded only as a worker module.
//
// It swaps the leaf between "a real file inside the workspace" and "a symlink
// pointing outside", which is exactly the check-to-syscall window the pre-fix
// module lost: `resolveWithin` saw the real file, and the separate name-based
// `writeFileSync` that followed it found the symlink and wrote through it.
// `unlinkOnly` drops the symlink half, leaving the vanish/recreate race that
// tests the "a write never lies" branch instead.
import * as fs from 'node:fs'
import { workerData } from 'node:worker_threads'

const { target, victim, deadline, unlinkOnly } = workerData as {
  target: string
  victim: string
  deadline: number
  unlinkOnly?: boolean
}

while (Date.now() < deadline) {
  try { fs.unlinkSync(target) } catch { /* already gone */ }
  if (!unlinkOnly) {
    try { fs.symlinkSync(victim, target) } catch { /* lost the race */ }
    try { fs.unlinkSync(target) } catch { /* already gone */ }
  }
  try { fs.writeFileSync(target, 'v1') } catch { /* lost the race */ }
}
