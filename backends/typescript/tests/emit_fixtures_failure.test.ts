import { spawnSync } from 'node:child_process'
import { describe, expect, it } from 'vitest'
import { describeEmitFailure, emitFixture } from '../scripts/emit-fixtures'

describe('emit-fixtures failure message', () => {
  it('names the spawn error when the interpreter cannot be spawned', () => {
    const result = spawnSync('revl-no-such-interpreter-2892', ['emit.py'], { encoding: 'utf-8' })
    const message = describeEmitFailure('user_cache.ir.json', result)
    expect(message).toContain('user_cache.ir.json')
    expect(message).toContain('could not run emit.py')
    expect(message).toContain('ENOENT')
    expect(message).not.toContain('null')
  })

  it('reports the exit status and stderr when emit.py exits non-zero', () => {
    const result = spawnSync(
      process.execPath,
      ['-e', 'process.stderr.write("boom\\n"); process.exit(3)'],
      { encoding: 'utf-8' },
    )
    const message = describeEmitFailure('user_cache.ir.json', result)
    expect(message).toContain('exit status 3')
    expect(message).toContain('boom')
    expect(message).not.toContain('null')
  })

  it('emitFixture throws the spawn error for a missing interpreter', () => {
    expect(() => emitFixture('user_cache.ir.json', 'unused_2892.ts', 'revl-no-such-interpreter-2892'))
      .toThrow(/could not run emit\.py for user_cache\.ir\.json: ENOENT/)
  })

  // On Windows a killed child reports an exit status, not a signal.
  it.skipIf(process.platform === 'win32')('reports the signal when emit.py is killed', () => {
    const result = spawnSync(
      process.execPath,
      ['-e', 'process.kill(process.pid, "SIGTERM")'],
      { encoding: 'utf-8' },
    )
    expect(describeEmitFailure('x.ir.json', result)).toContain('SIGTERM')
  })
})
