// Run a python script with the interpreter this tier uses everywhere else.
//
//     node scripts/run-python.ts <script.py> [args...]
//
// `npm run golden` used to spawn a bare `python3`, which does not exist on a
// Windows install that ships `python.exe` only and ignores `REVL_PYTHON`. This
// launcher takes the choice from `pythonInterpreter()` (the one rule shared
// with `emit-fixtures.ts`), inherits stdio and exits with the child's status.
import { spawnSync, type SpawnSyncReturns } from 'node:child_process'
import { pathToFileURL } from 'node:url'
import { pythonInterpreter } from './emit-fixtures.ts'

type Outcome = Pick<SpawnSyncReturns<Buffer>, 'error' | 'status' | 'signal'>

// What went wrong when the child did not exit normally, or null when it did.
// A failed spawn (no such interpreter) has a null status, so the spawn error is
// the only reason there is.
export function describeRunFailure(python: string, args: string[], result: Outcome): string | null {
  if (result.error) {
    const code = (result.error as NodeJS.ErrnoException).code
    return `could not run ${python} ${args.join(' ')}: ${code ?? result.error.name}: ${result.error.message}`
  }
  if (result.status === null) return `${python} ${args.join(' ')} was killed by signal ${result.signal}`
  return null
}

// The exit status this launcher should end with.
export function runPython(
  args: string[],
  env: NodeJS.ProcessEnv = process.env,
  platform: NodeJS.Platform = process.platform,
): number {
  const python = pythonInterpreter(env, platform)
  const result = spawnSync(python, args, { stdio: 'inherit', env })
  const failure = describeRunFailure(python, args, result)
  if (failure) {
    console.error(failure)
    return 1
  }
  return result.status ?? 1
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exit(runPython(process.argv.slice(2)))
}
