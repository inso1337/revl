import { spawnSync } from 'node:child_process'
import { describe, expect, it } from 'vitest'
import {
  describeEmitFailure,
  emitFixture,
  emitFixtureOutput,
  pythonInterpreter,
} from '../scripts/emit-fixtures'

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

  it('emitFixtureOutput throws the spawn error for its selected interpreter', () => {
    expect(() => emitFixtureOutput('user_cache.ir.json', 'revl-no-such-interpreter-2917-output'))
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

describe('pythonInterpreter', () => {
  it('uses REVL_PYTHON when set, on any platform', () => {
    expect(pythonInterpreter({ REVL_PYTHON: '/opt/py/bin/py' }, 'linux')).toBe('/opt/py/bin/py')
    expect(pythonInterpreter({ REVL_PYTHON: 'C:\\py\\python.exe' }, 'win32')).toBe('C:\\py\\python.exe')
  })

  it('defaults to python on win32', () => {
    expect(pythonInterpreter({}, 'win32')).toBe('python')
  })

  it('defaults to python3 on posix', () => {
    expect(pythonInterpreter({}, 'linux')).toBe('python3')
    expect(pythonInterpreter({}, 'darwin')).toBe('python3')
  })

  it('treats an empty REVL_PYTHON as unset', () => {
    expect(pythonInterpreter({ REVL_PYTHON: '' }, 'win32')).toBe('python')
    expect(pythonInterpreter({ REVL_PYTHON: '' }, 'linux')).toBe('python3')
    expect(pythonInterpreter({ REVL_PYTHON: '  ' }, 'win32')).toBe('python')
    expect(pythonInterpreter({ REVL_PYTHON: '  ' }, 'linux')).toBe('python3')
  })
})
