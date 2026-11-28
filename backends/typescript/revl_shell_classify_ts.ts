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
// `emission`. A backslash escape, a newline, and a backslash-newline LINE
// CONTINUATION (which the shell deletes while joining the words) are handled
// specially by the scanner. Mirrors py `_METACHARS`.
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
 * newline / dangling escape / unbalanced quote / a double-quoted expansion), or
 * `null` if the command is free of unquoted shell features. The load-bearing
 * safety scan — a metacharacter that is part of a quoted or escaped *argument*
 * (literal data) is allowed through, while one acting as a shell operator is
 * caught. Being purely textual it cannot be fooled by anything the shell would
 * expand — because it refuses everything the shell would expand, INCLUDING
 * inside double quotes, where `$` and the backtick are still features (only
 * `'...'` suppresses them).
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
        // A backslash-NEWLINE is NOT an escaped newline: it is a line
        // continuation, and the shell DELETES the pair (`"a\<nl>b"` is the word
        // `ab`), while the tokenizer below keeps a literal newline — so the
        // operand would not be the word the command names. Refuse. Mirrors py
        // `_first_unquoted_metachar`.
        if (i + 1 < n && cmd[i + 1] === '\n') return '\\n'
        // POSIX: inside double quotes a backslash escapes ONLY `$`, backtick,
        // `"` and `\` — and for `$`/backtick the shell CONSUMES it, so `"a\$b"`
        // is the word `a$b` while the tokenizer below (shlex's
        // `escapedquotes='"'` rule) keeps the backslash and yields `a\$b`. The
        // plan would name a path the command never mentions. Refuse.
        if (i + 1 < n && (cmd[i + 1] === '$' || cmd[i + 1] === '`')) {
          return 'escaped-expansion'
        }
        i += 2
        continue
      }
      if (c === '"') {
        inDouble = false
        i += 1
        continue
      }
      // DOUBLE quotes do NOT make `$` or the backtick literal — only single
      // quotes do. Inside `"..."` the shell still performs command substitution
      // (`"$(id)"`, `"`id`"`), parameter expansion (`"$HOME/x"`, `"${SRC}"`) and
      // arithmetic, so the operand the tokenizer records is not the path the
      // command will use. Refusing here is the whole point of the scan: `mv
      // "$(id)" dst` runs a command whose target the text does not name, and
      // auto-approving it as `witnessed` (reversible by construction) would
      // lower an op on a different path than the one that runs. Every OTHER
      // metacharacter IS literal inside double quotes, so `mv "a;b" c` still
      // lowers. Mirrors py `_first_unquoted_metachar`.
      if (c === '$' || c === '`') return 'expansion'
      i += 1
      continue
    }
    // unquoted context
    if (c === '\\') {
      if (i + 1 >= n) return '\\'
      // A backslash-NEWLINE is a line continuation: the shell deletes the pair
      // and joins the words (`mv a\<nl>b c` runs `mv ab c`), a join that also
      // crosses quotes (`mv 'x'\<nl>'y' c` runs `mv xy c`). The tokenizer below
      // cannot express either, so the plan would name a path the command never
      // mentions. Refuse.
      if (cmd[i + 1] === '\n') return '\\n'
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
    // shlex's whitespace set: space, tab, CR, LF. VT and FF are word
    // characters (py agrees), so `rm a<VT>b` is one operand on both tiers.
    if (c === ' ' || c === '\t' || c === '\n' || c === '\r') {
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

/** Python `str.isspace()`'s exact code-point set: Unicode White_Space PLUS
 * U+001C..U+001F (file/group/record/unit separators), and NOT U+FEFF. JS
 * `String.prototype.trim()` removes U+FEFF and NOT U+001C..U+001F, so the two
 * tiers disagreed on exactly those characters (issue #3265: `"\x1c"` was
 * "empty command" on py and `unrecognized command` on ts, `"\ufeff"` the
 * reverse). The classifier must strip with the PY set, because the py
 * classifier is the oracle the parity test compares against. */
function isPySpace(cp: number): boolean {
  return (cp >= 0x09 && cp <= 0x0d)          // TAB..CR
    || (cp >= 0x1c && cp <= 0x20)             // FS..US and space
    || cp === 0x85 || cp === 0xa0 || cp === 0x1680
    || (cp >= 0x2000 && cp <= 0x200a)
    || cp === 0x2028 || cp === 0x2029 || cp === 0x202f
    || cp === 0x205f || cp === 0x3000
}

/** `str.strip()` for the py oracle's whitespace set (issue #3265). Astral code
 * points are never in the set, so a lone trailing low surrogate is simply not
 * whitespace and stops the scan. */
function pyStrip(s: string): string {
  let lo = 0
  while (lo < s.length) {
    const cp = s.codePointAt(lo)!
    if (!isPySpace(cp)) break
    lo += cp > 0xffff ? 2 : 1
  }
  let hi = s.length
  while (hi > lo && isPySpace(s.charCodeAt(hi - 1))) hi -= 1
  return s.slice(lo, hi)
}

/** Classify one command line (what would otherwise go to `sh -c`). Total and
 * pure: never throws, never does IO. Returns a plan record identical in shape
 * and content to the py `classify` for the same input. */
export function classify(cmd: unknown): ShellPlan {
  if (typeof cmd !== 'string') {
    return emission(String(cmd), 'command is not a string')
  }

  const stripped = pyStrip(cmd)  // py `str.strip()`'s set, not JS trim() (issue #3265)
  if (!stripped) return emission(cmd, 'empty command')

  // (1) Safety scan FIRST.
  const meta = firstUnquotedMetachar(cmd)
  if (meta !== null) {
    if (meta === 'unbalanced-quote') return emission(cmd, 'unbalanced quotes')
    if (meta === '\\') return emission(cmd, 'dangling backslash escape')
    if (meta === '\\n') return emission(cmd, 'command spans multiple lines')
    if (meta === 'expansion') return emission(cmd, 'shell expansion inside double quotes')
    if (meta === 'escaped-expansion') {
      return emission(cmd, 'escaped expansion inside double quotes')
    }
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
  if (!(name in ARITY)) return emission(cmd, `unrecognized command ${pyReprStr(name)}`)

  // (3) Split operands from flags.
  const operands: string[] = []
  for (const tok of tokens.slice(1)) {
    if (tok.startsWith('-')) {
      return emission(cmd, `flag ${pyReprStr(tok)} changes semantics; not classifiable`)
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

  return emission(cmd, `unhandled command ${pyReprStr(name)}`)
}

// `repr`-style single-quoting to match py's `!r`/`{meta!r}` reason strings byte
// for byte (so the py<->ts reason parity holds). Covers the ASCII cases the
// classifier actually emits (metachars and command/flag tokens).
function reprChar(c: string): string {
  return pyReprStr(c)
}

// Python `str.isprintable()` for the categories that can be enumerated without
// the full Unicode tables: Cc, the BMP Cf list, Zs except U+0020, Zl, Zp, Cs
// (surrogates) and Co. (Cn - unassigned code points - is NOT enumerable here;
// such a point is treated as printable where python escapes it. No token in
// the parity corpus is unassigned; issue #3265's boundary chars are Cc/Cf/Zs,
// all covered above.)
function isPyPrintable(cp: number): boolean {
  if (cp === 0x20) return true
  if (cp <= 0x1f || (cp >= 0x7f && cp <= 0x9f)) return false            // Cc
  if (cp === 0xa0 || cp === 0x1680 || (cp >= 0x2000 && cp <= 0x200a)     // Zs
      || cp === 0x2028 || cp === 0x2029 || cp === 0x202f
      || cp === 0x205f || cp === 0x3000) return false
  if (cp === 0xad || (cp >= 0x600 && cp <= 0x605) || cp === 0x61c        // Cf
      || cp === 0x6dd || cp === 0x70f || cp === 0x8e2 || cp === 0x180e
      || (cp >= 0x200b && cp <= 0x200f) || (cp >= 0x202a && cp <= 0x202e)
      || (cp >= 0x2060 && cp <= 0x2064) || (cp >= 0x2066 && cp <= 0x206f)
      || cp === 0xfeff || (cp >= 0xfff9 && cp <= 0xfffb)) return false
  if ((cp >= 0xd800 && cp <= 0xdfff) || (cp >= 0xe000 && cp <= 0xf8ff)) return false // Cs, Co
  return true
}

// Escape one code point the way python `repr()` does: backslash and the chosen
// quote always; TAB/LF/CR with their letter forms; every other non-printable
// as \xXX / \uXXXX / \UXXXXXXXX.
function pyEscape(ch: string, quote: string): string {
  const cp = ch.codePointAt(0)!
  if (cp === 0x5c) return "\\"
  if (cp === quote.charCodeAt(0)) return "\\" + quote
  if (cp === 0x09) return "\\t"
  if (cp === 0x0a) return "\\n"
  if (cp === 0x0d) return "\\r"
  if (isPyPrintable(cp)) return ch
  if (cp < 0x100) return "\\x" + cp.toString(16).padStart(2, "0")
  if (cp < 0x10000) return "\\u" + cp.toString(16).padStart(4, "0")
  return "\\U" + cp.toString(16).padStart(8, "0")
}

// Python `repr()` of a string (issue #3265: the reason strings are compared
// byte-for-byte across tiers, and repr('\ufeff') is "'\\ufeff'" on py, so the
// ts side must escape non-printables the same way, not emit the raw char).
function pyReprStr(s: string): string {
  // Python repr prefers single quotes; a `'` in the string switches to double
  // quotes when no `"` is present.
  const hasSingle = s.includes("'")
  const hasDouble = s.includes('"')
  const quote = hasSingle && !hasDouble ? '"' : "'"
  let body = ""
  for (const ch of s) body += pyEscape(ch, quote)
  return quote + body + quote
}

// Install on import for the `@ts` classify body to delegate to.
;(globalThis as unknown as { __revlShellClassify?: typeof classify }).__revlShellClassify = classify
