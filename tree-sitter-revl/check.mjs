#!/usr/bin/env node
// Corpus honesty check — the conformance gate for tree-sitter-revl.
//
// Runs `tree-sitter parse` over every examples/*.rvl, examples/rejections/*.rvl
// and selfhost/*.rvl file in the parent revl checkout and fails if any
// NON-EXEMPT file produces an ERROR (or MISSING) node. This is the "kept honest
// by the corpus" gate: the same corpus the reference parser (src/revl/parser.py)
// and selfhost/parser.rvl agree on.
//
// Exemptions are named explicitly below with a reason. An exemption is only
// legitimate when the REFERENCE parser also rejects the file at PARSE time — a
// tree-sitter LR grammar cannot express the same context-sensitive rule. No
// file is silently skipped: every file is parsed and its status printed.

import { execFileSync } from 'node:child_process';
import { readdirSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve, relative } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, '..');

// Files the reference parser itself rejects at parse time with a construct that
// is not expressible as a context-free rule. Keyed by repo-relative path.
const EXEMPT = new Map([
  [
    'examples/rejections/t19_union_type.rvl',
    'revl has no union types: `type X = List[Row] | Str`. The reference ' +
      'parser rejects this at parse time (parser.py type_decl). A `|`-separated ' +
      'list of type applications is not part of revl syntax, so the grammar ' +
      'accepts variant cases (`Name(payload)`) but not type-applications as ' +
      'variant members.',
  ],
  [
    'examples/rejections/v2_provide_emission_fn.rvl',
    'a provide-method carries no purity modifier: it is a plain `fn`, and ' +
      'emission-ness is inherited from the service (G4). The reference parser ' +
      'rejects `emission fn` inside `provide` at parse time (expected fn, found ' +
      "emission), so the grammar's provide_method (plain `fn`) errors here too.",
  ],
  [
    'examples/rejections/lifecycle_no_swap.rvl',
    'revl has no `swap` statement: the reference parser rejects `swap A -> B` ' +
      'at parse time ("there is no `swap` statement", parser.py). A hot-swap is ' +
      'driven by the harness re-admitting an edited source against the running ' +
      'system, not by an in-language statement, so the grammar carries no ' +
      '`swap` node and the bare word is an ERROR here — matching the refusal.',
  ],
  // The `foreign_*` corpus: constructs from other languages the reference
  // parser refuses AT PARSE TIME by name (item 384, `_reject_foreign_keyword`
  // and the shape guards). They are genuinely-foreign SYNTAX, not context-
  // sensitive semantic rejections, so an LR grammar has no rule to match them
  // and its ERROR mirrors the reference's own parse-time refusal.
  [
    'examples/rejections/foreign_def.rvl',
    'revl has no `def` — functions are `fn` (reference rejects `def` at parse).',
  ],
  [
    'examples/rejections/foreign_lambda.rvl',
    'revl has no `lambda` — closures are `=>` arrows (parse-time refusal).',
  ],
  [
    'examples/rejections/foreign_elif.rvl',
    'revl has no `elif` — it is `else if` (reference rejects `elif` at parse).',
  ],
  [
    'examples/rejections/foreign_for_in.rvl',
    'revl iterates with `for (x of xs)`, not `for x in xs` (parse-time refusal).',
  ],
  [
    'examples/rejections/foreign_cstyle_for.rvl',
    'revl has no C-style `for (init; cond; step)` loop (parse-time refusal).',
  ],
  [
    'examples/rejections/foreign_increment.rvl',
    'revl has no `++` increment operator (reference rejects it at parse).',
  ],
  [
    'examples/rejections/foreign_kwargs.rvl',
    'revl has no keyword arguments `f(name=v)` (parse-time refusal).',
  ],
  [
    'examples/rejections/foreign_python_ternary.rvl',
    'revl has no Python `a if c else b` ternary — it is `c ? a : b` (parse-time).',
  ],
  [
    'examples/rejections/foreign_slice.rvl',
    'revl has no slice syntax `xs[a:b]` (reference rejects it at parse).',
  ],
  [
    'examples/rejections/foreign_string_dict.rvl',
    'revl records use identifier keys, not string keys `{"k": v}` (parse-time).',
  ],
  [
    'examples/rejections/foreign_tuple.rvl',
    'revl has no tuples `(a, b)` (reference rejects the tuple form at parse).',
  ],
]);

function rvlFiles() {
  const dirs = ['examples', 'examples/rejections', 'selfhost'];
  const out = [];
  for (const d of dirs) {
    const abs = join(repo, d);
    if (!existsSync(abs)) continue;
    for (const f of readdirSync(abs)) {
      if (f.endsWith('.rvl')) out.push(join(abs, f));
    }
  }
  return out.sort();
}

function hasError(file) {
  let sexp;
  try {
    sexp = execFileSync('npx', ['tree-sitter', 'parse', '-q', file], {
      cwd: here,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
    });
  } catch (e) {
    // tree-sitter exits non-zero when the tree contains an error; capture it
    sexp = (e.stdout || '') + (e.stderr || '');
  }
  return /\(ERROR|\(MISSING|MISSING /.test(sexp);
}

const files = rvlFiles();
let clean = 0;
let failed = 0;
let exemptErrored = 0;
let exemptClean = 0;
const failures = [];

for (const file of files) {
  const rel = relative(repo, file);
  const err = hasError(file);
  const exempt = EXEMPT.has(rel);
  if (exempt) {
    if (err) {
      exemptErrored++;
      console.log(`  EXEMPT (errors, expected)  ${rel}`);
    } else {
      exemptClean++;
      console.log(`  EXEMPT (now parses clean)  ${rel}`);
    }
    continue;
  }
  if (err) {
    failed++;
    failures.push(rel);
    console.log(`  ERROR                      ${rel}`);
  } else {
    clean++;
    console.log(`  ok                         ${rel}`);
  }
}

console.log('');
console.log(
  `corpus: ${clean} clean, ${failed} unexpected error(s), ` +
    `${EXEMPT.size} exemption(s) (${exemptErrored} errored as expected, ` +
    `${exemptClean} now clean), ${files.length} files total`,
);

if (failed > 0) {
  console.error(`\nFAIL: ${failed} non-exempt file(s) produced ERROR nodes:`);
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log('\nPASS: every non-exempt corpus file parses with zero ERROR nodes.');
