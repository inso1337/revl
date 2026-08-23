// Resolve cordis from the ONE install the TS backend already ships
// (backends/typescript/node_modules) instead of adding a second node_modules.
// We link it in as tools/residue-probe/node_modules so a bare `import 'cordis'`
// from this dir resolves. The link is gitignored; this recreates it on demand.

import { existsSync, symlinkSync, lstatSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const relTarget = join('..', '..', 'backends', 'typescript', 'node_modules')
const backendNodeModules = join(here, relTarget)
const link = join(here, 'node_modules')

export function ensureDeps() {
  let linked = false
  try {
    linked = lstatSync(link).isSymbolicLink() || existsSync(join(link, 'cordis'))
  } catch {
    linked = false
  }
  if (linked) return

  if (!existsSync(join(backendNodeModules, 'cordis'))) {
    throw new Error(
      'cordis is not installed. Run `npm install` in backends/typescript first ' +
        '(this probe reuses that single install; it does not add its own).',
    )
  }
  symlinkSync(relTarget, link, 'dir')
}
