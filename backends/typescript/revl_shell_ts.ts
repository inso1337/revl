// Node-tier host support for `stdlib/shell.rvl` on the ts tier (roadmap item
// 369 — the peer of backends/python/revl_shell_host.py, item 252).
//
// Two host bodies back the shell tool's extern surface:
//   * `runOpaque(cmd)` — the honest, irreversible `emission` fallback. A command
//     the pure classifier could not prove fs-local is run verbatim through the
//     system shell (`/bin/sh -c`), exactly as `sh -c` always did: one opaque
//     crossing, no witness, no inverse. The surface item 252 shrinks, not
//     removes — the unrecognized tail stays here, honestly one prompt.
//   * `classify` — re-exported from revl_shell_classify_ts so the `@ts` classify
//     body is a one-line delegation, matching the py `revl_shell_host` re-export.
//
// This module imports node:child_process (for the opaque path), so it is
// node-only; the classifier lives in its own environment-neutral module
// (revl_shell_classify_ts.ts), exactly as py splits revl_shell_classify from
// revl_shell_host. Both are installed on `globalThis.__revlShell` for the `@ts`
// bodies to reach (the ts analog of `backends/python` being on `sys.path`).

import { spawnSync } from 'node:child_process'

import { classify } from './revl_shell_classify_ts.ts'

/** Run `cmd` through the system shell and return its captured stdout — the
 * opaque `emission` path for a command the classifier did not lower. The whole
 * unreduced shell (`shell:true`, no argument parsing, no confinement); reached
 * ONLY for commands the classifier returned an `emission` verdict for. stderr
 * is folded into the returned text so a failing command's diagnostics are
 * visible to the operator who approved the one prompt (peer of py `run_opaque`). */
export function runOpaque(cmd: string): string {
  const completed = spawnSync(cmd, {
    shell: true,
    encoding: 'utf-8',
  })
  let out = completed.stdout ?? ''
  if (completed.stderr) out = out + completed.stderr
  return out
}

export interface RevlShellHost {
  classify: typeof classify
  runOpaque: typeof runOpaque
}

const HOST: RevlShellHost = { classify, runOpaque }

// Install on import (the side effect the shell bodies depend on).
;(globalThis as unknown as { __revlShell?: RevlShellHost }).__revlShell = HOST

export { classify }
