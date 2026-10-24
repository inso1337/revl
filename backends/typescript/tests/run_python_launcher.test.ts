import { spawnSync } from 'node:child_process'
import { chmodSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { describeRunFailure } from '../scripts/run-python'

const launcher = join(__dirname, '..', 'scripts', 'run-python.ts')

// A stand-in interpreter: prints its own name and arguments, exits with $FAKE_STATUS.
function fakePython(): string {
  const path = join(mkdtempSync(join(tmpdir(), 'run-python-')), 'fakepy')
  writeFileSync(path, '#!/bin/sh\necho "fakepy $@"\nexit ${FAKE_STATUS:-0}\n')
  chmodSync(path, 0o755)
  return path
}

function launch(env: Record<string, string>, ...args: string[]) {
  return spawnSync(process.execPath, [launcher, ...args], {
    encoding: 'utf-8',
    env: { ...process.env, ...env },
  })
}

describe('scripts/run-python.ts', () => {
  it('runs the interpreter REVL_PYTHON names, with the remaining args', () => {
    const result = launch({ REVL_PYTHON: fakePython() }, 'regen.py', '--flag')
    expect(result.status).toBe(0)
    expect(result.stdout).toBe('fakepy regen.py --flag\n')
  })

  it('exits with the status the child exited with', () => {
    const result = launch({ REVL_PYTHON: fakePython(), FAKE_STATUS: '7' }, 'regen.py')
    expect(result.status).toBe(7)
  })

  it('names the interpreter and the cause when it cannot be started', () => {
    const result = launch({ REVL_PYTHON: '/nonexistent/python' }, 'regen.py')
    expect(result.status).toBe(1)
    expect(result.stderr).toContain('could not run /nonexistent/python regen.py')
    expect(result.stderr).toContain('ENOENT')
  })
})

describe('describeRunFailure', () => {
  it('is null for a child that exited, whatever its status', () => {
    expect(describeRunFailure('py', ['a.py'], { error: undefined, status: 3, signal: null })).toBeNull()
  })

  it('names the signal when the child was killed', () => {
    const message = describeRunFailure('py', ['a.py'], { error: undefined, status: null, signal: 'SIGTERM' })
    expect(message).toContain('SIGTERM')
  })
})
