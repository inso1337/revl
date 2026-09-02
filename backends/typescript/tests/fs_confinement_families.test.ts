// The single-choke-point claim on the TS tier, maintained as a test rather than
// a comment, the peer of tests/test_fs_confinement_families.py.
//
// `revl_fs_ts.ts` used to say the py analog's contract was "preserved verbatim":
// symlinks resolved before the membership check, the guard applied to the
// inverse path too, the sidecars inside the root. It was not, and the gap was
// invisible because every existing test asserted the choke point on exactly the
// paths that DID route through it. Three families reached a syscall around it,
// an inverse's SOURCE endpoint, the sidecar DIRECTORIES, and (what realpath
// cannot see at all) hardlink aliases, and a fourth window sat between the
// check and the syscall.
//
// The claim is now an enumeration, `PATH_FAMILIES`, and this suite is what keeps
// it true:
//
//   * `the path families are enumerated` pins the family set and the entry
//     points each family names, so widening the guard's surface is a deliberate
//     edit in two places rather than a quiet import;
//   * the scans below parse the REAL `revl_fs_ts.ts` with the TypeScript
//     compiler API (the ts peer of python's `ast` scan, not a regex) and refuse
//     anything that is not a listed guard fed a guarded path. A fifth path
//     family cannot be added silently: a raw `fs.*` mutation outside the listed
//     syscall-time implementations fails the scan, and so does an unlisted
//     helper call inside an entry point.
//
// The scan reads the shipped module, not a fixture, so it fails the moment an
// entry point regresses.
import { describe, expect, it } from 'vitest'
import * as fs from 'node:fs'
import * as path from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'
import { PATH_FAMILIES, READ_HELPERS, SYSCALL_PATH_ARGS } from '../revl_fs_ts.ts'
import * as host from '../revl_fs_ts.ts'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const MODULE = path.join(HERE, '..', 'revl_fs_ts.ts')
const REPO = path.resolve(HERE, '..', '..', '..')
const FS_RVL = path.join(REPO, 'stdlib', 'fs.rvl')

const SOURCE = ts.createSourceFile(
  MODULE, fs.readFileSync(MODULE, 'utf-8'), ts.ScriptTarget.ESNext, true)

/** Every `node:fs` member that can change the filesystem (or hand out the fd
 * that will). Deliberately over-broad: an fs call that is NOT here and NOT a
 * read is a call this scan has never seen, and the module-wide assertion below
 * treats every `fs.*` call outside the listed implementations as suspect. */
const MUTATING_FS = new Set([
  'writeFileSync', 'appendFileSync', 'renameSync', 'unlinkSync', 'rmSync',
  'rmdirSync', 'mkdirSync', 'mkdtempSync', 'copyFileSync', 'linkSync',
  'symlinkSync', 'truncateSync', 'ftruncateSync', 'writeSync', 'openSync',
  'closeSync', 'chmodSync', 'fchmodSync', 'utimesSync', 'futimesSync',
  'createWriteStream', 'cpSync', 'writev', 'writevSync',
])

/** The ONLY functions that are not a `syscall-time` implementation and may
 * still reach a mutating `fs.*` call. Both are private helpers of a listed
 * family: `sidecarDirReal` is how family 2 creates its directories, `openLeaf`
 * is how `openConfinedWrite` opens the leaf. Pinned here on purpose, a third
 * name appearing in this list is a review decision, not an edit. */
const MUTATION_HELPERS = new Set(['sidecarDirReal', 'openLeaf'])

/** The namespace imports an entry point may not call into directly. */
const HOST_NAMESPACES = new Set(['fs', 'path'])

const FAMILY_GUARDS = new Set([
  ...PATH_FAMILIES['named-endpoint'],
  ...PATH_FAMILIES['sidecar-directory'],
  ...PATH_FAMILIES['inverse-source'],
])
const LISTED = new Set([
  ...Object.values(PATH_FAMILIES).flatMap((e) => [...e]),
  ...READ_HELPERS,
])

function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1)
}

function walk(node: ts.Node, visit: (n: ts.Node) => void): void {
  visit(node)
  ts.forEachChild(node, (child) => walk(child, visit))
}

function functionsByName(): Map<string, ts.FunctionDeclaration> {
  const out = new Map<string, ts.FunctionDeclaration>()
  for (const statement of SOURCE.statements) {
    if (ts.isFunctionDeclaration(statement) && statement.name) {
      out.set(statement.name.text, statement)
    }
  }
  return out
}

const FUNCTIONS = functionsByName()

/** The per-extern entry points `stdlib/fs.rvl` imports from this module. Read
 * out of fs.rvl rather than hard-coded, so a NEW witnessed extern (or a new
 * inverse) is pulled into every scan below the moment it is declared. */
function entryPointNames(): string[] {
  const text = fs.readFileSync(FS_RVL, 'utf-8')
  const names = [...text.matchAll(/= @ts ref (\w+) from "[^"]*revl_fs_ts\.ts"/g)]
    .map((m) => m[1])
  return [...new Set(names)].sort()
}

const ENTRY_POINTS = entryPointNames()

/** Every call in `node`, as (callee spelling, call node). A callee spelled
 * `x.y` is reported as `x.y`; a bare `f(...)` as `f`. */
function calls(node: ts.Node): Array<[string, ts.CallExpression]> {
  const out: Array<[string, ts.CallExpression]> = []
  walk(node, (n) => {
    if (!ts.isCallExpression(n)) return
    const callee = n.expression
    if (ts.isIdentifier(callee)) out.push([callee.text, n])
    else if (ts.isPropertyAccessExpression(callee)
      && ts.isIdentifier(callee.expression)) {
      out.push([`${callee.expression.text}.${callee.name.text}`, n])
    }
  })
  return out
}

// ===========================================================================
// The enumeration itself
// ===========================================================================

describe('the path families are enumerated', () => {
  it('is a closed set with a named guard each', () => {
    // Adding one means editing `PATH_FAMILIES` and this assertion together,
    // which is the point: the other three families were added to the code
    // (hardlinks, the sidecar dirs, an inverse's source) without anyone editing
    // a table, because there was none.
    expect(new Set(Object.keys(PATH_FAMILIES))).toEqual(new Set([
      'named-endpoint',      // a path an op was handed
      'sidecar-directory',   // the reversal machinery's own directories
      'inverse-source',      // what an inverse renames FROM
      'syscall-time',        // the mutation itself
    ]))
    for (const [family, entries] of Object.entries(PATH_FAMILIES)) {
      expect(entries.length, `family ${family} names no guard`).toBeGreaterThan(0)
      for (const entry of entries) {
        expect(typeof (host as unknown as Record<string, unknown>)[entry],
          `${family} names ${entry}, which is not a callable in the guard`)
          .toBe('function')
      }
    }
  })

  it('declares which syscall-time arguments are paths', () => {
    // so the argument scan below can never silently skip one
    expect(new Set(Object.keys(SYSCALL_PATH_ARGS)))
      .toEqual(new Set(PATH_FAMILIES['syscall-time']))
  })

  it('every listed entry point has a `raw` implementation in the module', () => {
    // The table is bound to the code: a name in `PATH_FAMILIES` with no
    // `raw<Name>` function is a table entry pointing at nothing, and a
    // `raw<Name>` with no table entry is an unlisted way to reach the
    // filesystem. Module init already throws on the first; this catches both.
    for (const entry of [...LISTED]) {
      expect(FUNCTIONS.has(`raw${capitalize(entry)}`),
        `\`${entry}\` is listed but has no raw${capitalize(entry)} implementation`)
        .toBe(true)
    }
    for (const name of FUNCTIONS.keys()) {
      if (!name.startsWith('raw')) continue
      const entry = name.slice(3, 4).toLowerCase() + name.slice(4)
      expect(LISTED.has(entry),
        `\`${name}\` looks like a guard implementation but \`${entry}\` is in no `
        + 'family and is no read helper')
        .toBe(true)
    }
  })

  it('every listed entry point is wrapped total', () => {
    for (const entry of [...LISTED]) {
      const fn = (host as unknown as Record<string, unknown>)[entry] as
        { isTotalGuard?: boolean }
      expect(fn.isTotalGuard, `guard entry point \`${entry}\` is not total`)
        .toBe(true)
    }
  })
})

// ===========================================================================
// The scans
// ===========================================================================

describe('the scan of revl_fs_ts.ts', () => {
  it('reaches every entry point stdlib/fs.rvl imports from this module', () => {
    // If the module ever stops exporting one, the scans below would silently
    // cover less than they claim.
    expect(ENTRY_POINTS.length).toBeGreaterThanOrEqual(8)
    for (const name of ENTRY_POINTS) {
      expect(FUNCTIONS.has(name),
        `stdlib/fs.rvl refs \`${name}\`, which revl_fs_ts.ts does not declare`)
        .toBe(true)
    }
  })

  it('no entry point performs a raw filesystem mutation', () => {
    // The old entry points called `fs.writeFileSync` / `replace(...)` directly,
    // which is exactly how the inverse-source and sidecar-directory families
    // reached a syscall without passing a guard.
    for (const name of ENTRY_POINTS) {
      for (const [callee] of calls(FUNCTIONS.get(name)!)) {
        const [object] = callee.split('.')
        expect(HOST_NAMESPACES.has(object) && callee.includes('.'),
          `\`${name}\` calls \`${callee}\`; an entry point may reach the `
          + 'filesystem only through the guard')
          .toBe(false)
      }
    }
  })

  it('every helper an entry point calls is a listed guard or read helper', () => {
    // No unlisted helper. A new module-level entry point is a new way to reach
    // the filesystem, so it has to be declared in a family (or as a read
    // helper) before an entry point may use it.
    for (const name of ENTRY_POINTS) {
      for (const [callee] of calls(FUNCTIONS.get(name)!)) {
        if (callee.includes('.')) continue          // e.asError(), etc.
        if (!FUNCTIONS.has(callee)) continue        // not a module function
        expect(LISTED.has(callee),
          `\`${name}\` calls unlisted module helper \`${callee}\``).toBe(true)
      }
    }
  })

  it('every path handed to a syscall came from a family 1-3 guard', () => {
    // The load-bearing one: every path reaching a mutation was BOUND from a
    // family 1-3 guard in the same entry point.
    //
    // This is the assertion the old code could not have passed:
    // `replace(w.preimage, target)` feeds a mutation a raw witness field, which
    // is item 422 F1 exactly. Written as a data-flow check rather than a
    // call-name check, so it stays true for a helper not yet invented.
    for (const name of ENTRY_POINTS) {
      const fn = FUNCTIONS.get(name)!
      // names bound from a guard call: `const target = resolveWithin(p)`
      const guarded = new Set<string>()
      walk(fn, (n) => {
        if (!ts.isVariableDeclaration(n) || !n.initializer) return
        if (!ts.isIdentifier(n.name)) return
        const init = n.initializer
        if (ts.isCallExpression(init) && ts.isIdentifier(init.expression)
          && FAMILY_GUARDS.has(init.expression.text)) {
          guarded.add(n.name.text)
        }
      })
      // ...and from a plain assignment, for the `let x; try { x = guard() }` shape
      walk(fn, (n) => {
        if (!ts.isBinaryExpression(n)) return
        if (n.operatorToken.kind !== ts.SyntaxKind.EqualsToken) return
        if (!ts.isIdentifier(n.left)) return
        const right = n.right
        if (ts.isCallExpression(right) && ts.isIdentifier(right.expression)
          && FAMILY_GUARDS.has(right.expression.text)) {
          guarded.add(n.left.text)
        }
      })

      for (const [callee, call] of calls(fn)) {
        const indices = SYSCALL_PATH_ARGS[callee]
        if (indices === undefined) continue
        for (const index of indices) {
          expect(index, `\`${name}\` calls \`${callee}\` without its path `
            + `argument ${index}`).toBeLessThan(call.arguments.length)
          const arg = call.arguments[index]
          const ok = ts.isIdentifier(arg) && guarded.has(arg.text)
          expect(ok,
            `\`${name}\` passes an UNGUARDED path to \`${callee}\` at argument `
            + `${index} (${arg.getText(SOURCE).slice(0, 60)}); every path `
            + `reaching a syscall must be bound from one of `
            + `${[...FAMILY_GUARDS].sort().join(', ')}`)
            .toBe(true)
        }
      }
    }
  })

  it('a mutating fs call appears only in a listed syscall-time implementation', () => {
    // The module-wide half: the guard itself is allowed to call `node:fs`, but
    // only inside the implementations the table names (plus the two pinned
    // private helpers). This is what makes a fifth family impossible to add
    // quietly, a new function that mutates the filesystem fails here until it
    // is listed in `PATH_FAMILIES` and named `raw<Entry>`.
    const allowed = new Set([
      ...PATH_FAMILIES['syscall-time'].map((e) => `raw${capitalize(e)}`),
      ...MUTATION_HELPERS,
    ])
    for (const [name, fn] of FUNCTIONS) {
      for (const [callee] of calls(fn)) {
        if (!callee.startsWith('fs.')) continue
        const member = callee.slice(3)
        if (!MUTATING_FS.has(member)) continue
        expect(allowed.has(name),
          `\`${name}\` calls the mutating \`${callee}\`, but it is not a listed `
          + 'syscall-time implementation nor a pinned mutation helper; list it '
          + 'in PATH_FAMILIES (and in this test) before it may touch the '
          + 'filesystem')
          .toBe(true)
      }
    }
  })

  it('the deprecated __revlFs seam publishes no unconfined mutator', () => {
    // The seam used to export `replace`, `remove`, `writeFile`, `mkdirOne`,
    // `rmdir` and `snapshot` unguarded. An exported unconfined `replace` IS
    // item 422 F1: a rename whose source is whatever the caller names.
    const seam = (globalThis as { __revlFs?: Record<string, unknown> }).__revlFs
    expect(seam).toBeTruthy()
    for (const gone of ['replace', 'remove', 'writeFile', 'mkdirOne', 'rmdir',
      'snapshot', 'exists', 'isFile', 'isDir']) {
      expect(seam![gone], `the __revlFs seam still publishes \`${gone}\``)
        .toBeUndefined()
    }
    for (const [name, value] of Object.entries(seam!)) {
      if (typeof value !== 'function') continue
      if (name === 'workspaceRoot') continue          // a read, and the anchor
      if (name.endsWith('Error')) continue            // the error classes
      expect(LISTED.has(name),
        `the __revlFs seam publishes \`${name}\`, which is in no family`)
        .toBe(true)
    }
  })
})
