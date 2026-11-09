# Applyable fixes (`fix_code`)

A rejection already carries its *prose* fix: `revl.diagnostics.classify()` puts
`record["fix"]` beside the guarantee, so an agent reads a rule, a call chain,
and a sentence describing the repair without a second call. The fix is the
per-code entry in `diagnostics.FIXES` unless the rejection carries its own
(`RevlError.fix`): a `requires`/`provides` clause written inside a component
body carries the corrected header line, because the per-code sentence for its
old classification (G6, "bind the value with `let`") was the repair for a
different mistake. This document is
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

### G4: the acquire and its ranked repairs

The reversibility refusal (G4) is the one a model-driven author hits most, and
the per-code sentence is deliberately general: "give the mutation an `undo`, or
admit it is irreversible". A missing-undo record now says *which* acquisition it
is and what to do about it, in three additive fields beside `fix`:

```json
{
  "code": "G4",
  "acquire": {"file": "<candidate>.rvl", "line": 6,
              "extern": "Pool", "method": "open"},
  "declaredClass": {"declared": "host", "derived": "mutation"},
  "repairs": [
    {"kind": "site-undo",
     "action": "bind the acquisition and spell its one inverse: ...",
     "fix_code": {"title": "Add the acquisition's inverse: `undo pool.close()`",
                  "edits": [ ... ]}},
    {"kind": "witnessed", "action": "declare the acquisition as an `extern witnessed fn` ..."},
    {"kind": "service-emission", "action": "move the crossing behind a service operation declared `emission` ..."}
  ]
}
```

`acquire` names the exact position (file and line, from the record itself, so a
raise site cannot disagree with the diagnostic) and the extern and method the
call reaches; a bare extern call has `method: null`. `declaredClass` pairs the
class the declaration gives (`pure`, `acquire`, `emission`, `local`,
`witnessed`, or `host` for the host-stub family surface; `null` when no
declaration resolves) with the class the checker derived for this call
(`mutation` when it needs an inverse, `unknown` when nothing resolves).
`repairs` is ordered by preference and each entry carries a `kind` and the
`action` a person or a model can enact: `site-undo`, `declare-inverse`,
`witnessed`, `plain-call`, `reclassify`, `service-emission`.

Only the repair whose edit the engine can determine, `site-undo` (the declared
inverse or the host family's release on the bound handle), carries a
`fix_code`. It is built and verified exactly like any other code action: the
engine applies the candidate edit and re-checks the patched source, and the
field is absent when the candidate does not resolve the refusal (an unbound
acquisition has no handle to release). The prose repairs stay prose: they cannot
be checked mechanically, so they are listed with no `fix_code` rather than
given an edit the engine did not verify.

`diagnostics.report` attaches the verified `fix_code` when it is handed the
text that compiled (`report(error, sources={filename: text})`), which is what
`revl_check` does for an inline candidate and for an edited buffer riding as a
`modules` overlay. It never reads `error.filename` from disk: a multi-file
compile can overlay edited text on a real path, so the file there may be
pre-edit text that never compiled. A compile the caller describes only by disk
paths carries the prose repairs. The record itself is unchanged in shape either
way, and the MCP `revl_check` answer carries it as classified.

### G4: an `undo` that does not release the bound handle

A host acquisition has exactly one inverse, its family's release applied to
the handle it bound. An `undo` that names the family instead of the handle is
refused, and the message spells the right one:

```revl reject G4
service Flags {
  fn enabled(name: Str) -> Opt[Str]
}
component FeatureFlags provides flags: Flags {
  let store = effect Map.new() undo Map.drop()
  provide flags {
    fn enabled(name) = store.get(name)
  }
}
```

The message reads "the `undo` of `let store = effect Map.new(...)` must
release THAT handle: write `undo store.drop()`". The quick fix replaces the
call after `undo` with the inverse the message names, and the result is clean:

```revl
service Flags {
  fn enabled(name: Str) -> Opt[Str]
}
component FeatureFlags provides flags: Flags {
  let store = effect Map.new() undo store.drop()
  provide flags {
    fn enabled(name) = store.get(name)
  }
}
```

Only the one-line shape is rewritten: the `let`, the acquisition and a single
trailing `undo <call>` on the refused line. An acquisition that spans lines, a
line with a second `undo`, or a comment on the line gets no action. This and
the `site-undo` repair above share the G4 entry: a refusal with this message
goes to this generator, every other G4 refusal to `site-undo`.

### SYNTAX: `emission fn` in a `provide` block

Provider methods are plain `fn`: whether an operation is an emission comes
from the service declaration, so writing `emission` again on the provider
method is a syntax error ("expected fn, found 'emission'"):

```revl reject SYNTAX
service Database {
  fn query(sql: Str) -> Int
  emission fn execute(sql: Str) -> Int
}
component Db provides db: Database {
  provide db {
    fn query(sql: Str) -> Int { return 1 }
    emission fn execute(sql: Str) -> Int { return 2 }
  }
}
```

The quick fix deletes the `emission ` before `fn` on the refused line; the
service declaration keeps its own:

```revl
service Database {
  fn query(sql: Str) -> Int
  emission fn execute(sql: Str) -> Int
}
component Db provides db: Database {
  provide db {
    fn query(sql: Str) -> Int { return 1 }
    fn execute(sql: Str) -> Int { return 2 }
  }
}
```

This is the only SYNTAX refusal with a generator, and it is gated on that
message: a top-level `emission fn`, refused as "expected a top-level
declaration", gets nothing. A refused line with zero or several `emission fn`
spellings declines too.

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

The same engine backs the G4 `site-undo` repair above, so the edit a G4 record
carries in `repairs[i].fix_code` is byte-identical to the one
`fix_code(rejection, source)` returns for that refusal, and to the LSP quick
fix.

## Adding a code

A new fixable code is one entry in `_GENERATORS`, its generator, and a proof in
`tests/test_fixgen.py`. The generator returns a candidate `Fix`; the engine and
both front doors need no change. The proof is end to end: the diagnostic, the
generated edit, and a re-check of the *patched* source showing the rejection is
gone — the same bar every code here already clears.
