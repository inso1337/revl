# Applyable fixes (`fix_code`)

A rejection already carries its *prose* fix: `revl.diagnostics.classify()` puts
`record["fix"]` beside the guarantee, so an agent reads a rule, a call chain,
and a sentence describing the repair without a second call. This document is
the next step: turning that sentence into a **concrete, applyable edit** — a
text range and its replacement — for the diagnostics whose rewrite is
unambiguous.

One engine produces the edit; two front doors expose it:

- the LSP quick fix (`textDocument/codeAction`), for a person in an editor;
- the agent payload (`fix_code`), for a tool acting on a structured rejection.

Both are in `src/revl/lsp/fixgen.py`. The LSP wiring is `compute_code_actions`
in `src/revl/lsp/analysis.py`; the agent entry point is `fixgen.fix_code`.

## The stance: verified or nothing

The engine is conservative and honest. It never hands back an edit that would
not actually resolve the diagnostic. A per-code generator proposes a *candidate*
edit; the engine then applies it and re-checks the patched source. The fix is
returned only when the rejection is gone and the edit did not trade it for a
worse one on the same line (`_resolves`). A diagnostic with no mechanical
rewrite — or a candidate that fails to verify — yields **no** code action, and
the prose fix stands as the guidance. No-action always beats a wrong edit.

This is why the same generator both fixes and declines the same code depending
on context (see T2 below): the verify gate, not the generator, has the final
say.

## Codes with an applyable fix

### T2 — `null` has no type

revl has no `null`; absence is `Opt[T]`, and `None` is the absent optional. In
an optional context the rewrite is mechanical — replace the `null` literal with
`None`:

```revl reject T2
fn first() -> Opt[Int] {
  return null
}
```

The quick fix replaces `null` with `None`, and the result checks clean:

```revl
fn first() -> Opt[Int] {
  return None
}
```

Where the context is **not** optional (`let x: Int = null`), `None` would only
trade the null-safety rejection for a type mismatch on the same line. The
generator still proposes the rewrite, but the verify gate rejects it, so no
code action is offered — the prose fix (restructure with `match`/`??`) remains
the right guidance.

### A9 — a provide key not in the `provides` clause

A `provide` block whose key is not declared in the component's `provides`
clause is refused under A9. When the component declares exactly one provision,
the block can only have meant that key, so the fix renames the block:

```revl reject A9
service Store {
  fn get(k: Str) -> Str
}
component Cache provides db: Store {
  provide cache {
    fn get(k: Str) -> Str { return k }
  }
}
```

The quick fix renames `provide cache` to `provide db`; the renamed block is
then checked against `db`'s service (A6) before the fix is offered, and the
result is clean:

```revl
service Store {
  fn get(k: Str) -> Str
}
component Cache provides db: Store {
  provide db {
    fn get(k: Str) -> Str { return k }
  }
}
```

With zero or several declared provisions the intended key is ambiguous, so no
rename is safe and no action is emitted.

## The agent payload

`fix_code(rejection, source)` takes a structured rejection (the
`diagnostics.classify` record an agent already holds) plus the source it came
from, and returns the applyable edit:

```json
{
  "ok": true,
  "code": "T2",
  "title": "Replace `null` with `None` (absence is `Opt[T]`)",
  "edits": [
    {"range": {"start": {"line": 1, "character": 9},
               "end":   {"line": 1, "character": 13}},
     "newText": "None"}
  ]
}
```

When no safe mechanical fix exists it answers `{"ok": false, "code", "reason"}`;
the prose `fix` from the rejection is still the guidance. The rejection carries
a code and a line but no span, so the ranged diagnostic is recomputed from the
source and matched to the rejection, then run through the one shared engine —
the LSP code action and the agent payload carry byte-identical edits. (An MCP
verb over `fix_code` is left for later; this item ships the engine.)

## Adding a code

A new fixable code is one entry in `_GENERATORS`, its generator, and a proof in
`tests/test_fixgen.py`. The generator returns a candidate `Fix`; the engine and
both front doors need no change. The proof is end to end: the diagnostic, the
generated edit, and a re-check of the *patched* source showing the rejection is
gone — the same bar every code here already clears.
