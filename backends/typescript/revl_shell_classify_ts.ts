// Pure shell-to-witnessed classifier for `stdlib/shell.rvl` on the ts tier
// (roadmap item 369 — the faithful ts peer of
// backends/python/revl_shell_classify.py, item 252).
//
// The classifier is PURE, TOTAL and TWO-VALUED and — being tier-agnostic —
// yields the SAME plan record as the py classifier for the same command text
// (proven by the py<->ts parity test). "cannot classify" is a first-class
// `emission` verdict, never a fallthrough to witnessed. The one asymmetry that
// matters: a false `emission` costs a prompt (annoying); a false `witnessed`
// auto-approves an irreversible effect (dangerous) — so every ambiguity
// resolves to `emission`. See revl_shell_classify.py for the full contract,
// the lowering table, and the refusal rules; this file mirrors it statement for
// statement so the two tiers cannot drift.
//
// No node imports: the classifier touches no IO and no environment, so this
// module is environment-neutral. It installs `globalThis.__revlShellClassify`
// for the `@ts` `classify` extern body to delegate to (the ts spelling of the
// py body's `import revl_shell_classify; return revl_shell_classify.classify`).

/** One lowered catalog op, or the verdict envelope — the plan record shape the
 * `plan_*` accessors read (`{ verdict, command, ops? , reason? }`). Kept as a
 * loose record so it maps to the extern's `Any` return, exactly like the py
 * dict. */
export interface ShellOp {
  op: string
  witnessed: string
  inverse: string
  args: string[]
  create_only?: boolean
}
export interface ShellPlan {
  verdict: 'witnessed' | 'emission'
  command: string
  ops?: ShellOp[]
  reason?: string
}

// Unquoted occurrences of any of these mean a shell feature (pipeline, redirect,
// expansion, glob, subshell, comment, sequence) is in play — an immediate
// `emission`. A backslash escape and a newline are handled specially by the
// scanner. Mirrors py `_METACHARS`.
const METACHARS = new Set('|&;<>(){}$`*?[]~!#'.split(''))

// The commands we know how to lower, and the exact operand arity each requires.
// `null` means "one or more" (a sequence of independent witnessed ops). Mirrors
// py `_ARITY`.
const ARITY: Record<string, number | null> = {
  mv: 2,
  cp: 2,
  rm: null,
  mkdir: null,
  touch: null,
}

/** The first shell metacharacter OUTSIDE any quoted span (or a description of a
 * newline / dangling escape / unbalanced quote), or `null` if the command is
 * free of unquoted shell features. The load-bearing safety scan — a
 * metacharacter that is part of a quoted or escaped argument (literal filename
 * data) is allowed through, while one acting as a shell operator is caught.
 * Peer of py `_first_unquoted_metachar`. */
function firstUnquotedMetachar(cmd: string): string | null {
  let i = 0
  const n = cmd.length
  let inSingle = false // inside '...'  (no escapes, everything literal)
  let inDouble = false // inside "..."  (backslash escapes a few chars)
  while (i < n) {
    const c = cmd[i]
    if (inSingle) {
      if (c === "'") inSingle = false
      i += 1
      continue
    }
    if (inDouble) {
      if (c === '\\') {
        i += 2
        continue
      }
      if (c === '"') inDouble = false
      i += 1
      continue
    }
    // unquoted context
    if (c === '\\') {
      if (i + 1 >= n) return '\\'
      i += 2
      continue
    }
    if (c === "'") {
      inSingle = true
      i += 1
      continue
    }
    if (c === '"') {
      inDouble = true
      i += 1
      continue
    }
    if (c === '\n' || c === '\r') return '\\n'
    if (METACHARS.has(c)) return c
    i += 1
  }
  if (inSingle || inDouble) return 'unbalanced-quote'
  return null
}

/** POSIX-mode argv tokenization, matching `shlex.split(cmd, posix=True)` for
 * the inputs that pass `firstUnquotedMetachar` (i.e. with no unquoted shell
 * operators left to interpret). Splits on unquoted whitespace; single quotes
 * are wholly literal; inside double quotes a backslash escapes only `"` and
 * `\` (else the backslash is literal — shlex's `escapedquotes='"'` rule); an
 * unquoted backslash escapes the next char. Throws on an unbalanced quote (the
 * scanner already rejects those, so this is belt-and-suspenders). */
function shlexSplit(cmd: string): string[] {
  const tokens: string[] = []
  let cur = ''
  let has = false // a token has started (so `''` yields one empty token)
  let i = 0
  const n = cmd.length
  let inSingle = false
  let inDouble = false
  while (i < n) {
    const c = cmd[i]
    if (inSingle) {
      if (c === "'") {
        inSingle = false
      } else {
        cur += c
      }
      i += 1
      continue
    }
    if (inDouble) {
      if (c === '\\') {
        const nxt = i + 1 < n ? cmd[i + 1] : ''
        if (nxt === '"' || nxt === '\\') {
          cur += nxt
          i += 2
          continue
        }
        cur += '\\'
        i += 1
        continue
      }
      if (c === '"') {
        inDouble = false
        i += 1
        continue
      }
      cur += c
      i += 1
      continue
    }
    // unquoted
    if (c === ' ' || c === '\t' || c === '\n' || c === '\r' || c === '\f' || c === '\v') {
      if (has) {
        tokens.push(cur)
        cur = ''
        has = false
      }
      i += 1
      continue
    }
    if (c === '\\') {
      if (i + 1 < n) {
        cur += cmd[i + 1]
        has = true
        i += 2
        continue
      }
      throw new Error('no escaped character')
    }
    if (c === "'") {
      inSingle = true
      has = true
      i += 1
      continue
    }
    if (c === '"') {
      inDouble = true
      has = true
      i += 1
      continue
    }
    cur += c
    has = true
    i += 1
  }
  if (inSingle || inDouble) throw new Error('No closing quotation')
  if (has) tokens.push(cur)
  return tokens
}

function emission(cmd: string, reason: string): ShellPlan {
  return { verdict: 'emission', command: cmd, reason }
}

function op(opName: string, witnessed: string, inverse: string, args: string[]): ShellOp {
  return { op: opName, witnessed, inverse, args: args.slice() }
}

function witnessed(cmd: string, ops: ShellOp[]): ShellPlan {
  return { verdict: 'witnessed', command: cmd, ops }
}

/** Classify one command line (what would otherwise go to `sh -c`). Total and
 * pure: never throws, never does IO. Returns a plan record identical in shape
 * and content to the py `classify` for the same input. */
export function classify(cmd: unknown): ShellPlan {
  if (typeof cmd !== 'string') {
    return emission(String(cmd), 'command is not a string')
  }

  const stripped = cmd.trim()
  if (!stripped) return emission(cmd, 'empty command')

  // (1) Safety scan FIRST.
  const meta = firstUnquotedMetachar(cmd)
  if (meta !== null) {
    if (meta === 'unbalanced-quote') return emission(cmd, 'unbalanced quotes')
    if (meta === '\\') return emission(cmd, 'dangling backslash escape')
    if (meta === '\\n') return emission(cmd, 'command spans multiple lines')
    return emission(cmd, `unquoted shell metacharacter ${reprChar(meta)}`)
  }

  // (2) Tokenize.
  let tokens: string[]
  try {
    tokens = shlexSplit(cmd)
  } catch (exc) {
    return emission(cmd, `unparseable command (${(exc as Error).message})`)
  }
  if (tokens.length === 0) return emission(cmd, 'empty command')

  const name = tokens[0]
  if (!(name in ARITY)) return emission(cmd, `unrecognized command ${reprStr(name)}`)

  // (3) Split operands from flags.
  const operands: string[] = []
  for (const tok of tokens.slice(1)) {
    if (tok.startsWith('-')) {
      return emission(cmd, `flag ${reprStr(tok)} changes semantics; not classifiable`)
    }
    operands.push(tok)
  }

  if (operands.length === 0) return emission(cmd, `${name}: no operands`)

  const arity = ARITY[name]
  if (arity !== null && operands.length !== arity) {
    return emission(cmd, `${name}: expected ${arity} operand(s), got ${operands.length}`)
  }

  // (4) Build the lowering plan against the witnessed catalog.
  if (name === 'mv') {
    const [src, dst] = operands
    return witnessed(cmd, [op('mv', 'move', 'unmove', [src, dst])])
  }
  if (name === 'cp') {
    const [src, dst] = operands
    return witnessed(cmd, [op('cp', 'write', 'restore', [src, dst])])
  }
  if (name === 'rm') {
    return witnessed(cmd, operands.map((p) => op('rm', 'rm', 'unrm', [p])))
  }
  if (name === 'mkdir') {
    return witnessed(cmd, operands.map((p) => op('mkdir', 'mkdir', 'rmdir_if_empty', [p])))
  }
  if (name === 'touch') {
    const ops = operands.map((p) => {
      const e = op('touch', 'write', 'restore', [p])
      e.create_only = true
      return e
    })
    return witnessed(cmd, ops)
  }

  return emission(cmd, `unhandled command ${reprStr(name)}`)
}

// `repr`-style single-quoting to match py's `!r`/`{meta!r}` reason strings byte
// for byte (so the py<->ts reason parity holds). Covers the ASCII cases the
// classifier actually emits (metachars and command/flag tokens).
function reprChar(c: string): string {
  return reprStr(c)
}
function reprStr(s: string): string {
  // Python repr prefers single quotes; a `'` in the string switches to double
  // quotes (none of the classifier's tokens force the escaped-single form).
  const hasSingle = s.includes("'")
  const hasDouble = s.includes('"')
  if (hasSingle && !hasDouble) return `"${s}"`
  const body = s.replace(/\\/g, '\\\\').replace(/'/g, "\\'")
  return `'${body}'`
}

// Install on import for the `@ts` classify body to delegate to.
;(globalThis as unknown as { __revlShellClassify?: typeof classify }).__revlShellClassify = classify
