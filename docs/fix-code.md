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

One engine produces the edit; three front doors expose it:

- the LSP quick fix (`textDocument/codeAction`), for a person in an editor;
- the agent payload (`fix_code`), for a tool acting on a structured rejection;
- the MCP verb `revl_apply_repair` (issue #2855), which applies the verified
  edit a refusal carries and returns the repaired candidate with its re-check.

All three are in `src/revl/lsp/fixgen.py`. The LSP wiring is
`compute_code_actions` in `src/revl/lsp/analysis.py`; the agent entry point is
`fixgen.fix_code`; the MCP store and apply path is
`src/revl/mcp/refusal_repair.py`.

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

### G1, G2 and R2: the verified candidate repairs

A capability or admission refusal carries a `repairs` list beside `fix`
(issue #2759): every mechanical way out the engine could verify, in a fixed
order, rather than one sentence of advice. Three refusal kinds are covered,
and they share the G4 entry vocabulary above (`kind`, `action`, and a verified
`fix_code`) plus one new field, `widens_authority`:

| refusal | kinds |
| --- | --- |
| G1 `requirement` - a body reaches a key its component does not declare | `add-requires`, `remove-call` |
| G2 `linking` - two components install the same provision key | `rename-key` |
| R2 `admission` - a `requires` binds a service outside the granted set | `route-via-granted-service` |

`fixgen.generate_repairs` is the engine. A generator emits candidate seeds;
the engine applies each seed to the refused text and re-checks the patch, and
keeps only the candidates that clear the refusal and introduce no new
diagnostic. `diagnostics.report` runs it when it holds the compiled text and
the caller's verifier, exactly as it does for the G4 `site-undo` edit, and
attaches the survivors to the record. An empty list means the engine ran and
nothing verified, which is different from a record produced without the text
(the field is then absent).

Each check is a full compile, so one `report` call spends at most
`diagnostics.REPAIR_COMPILE_BUDGET` (8) verification compiles across all its
records, the single-edit checks included: the G4 `site-undo` check and the
T2, A9 and REVL `fix_code` check (issue #2855) each spend one compile per
record. A record
is checked whole or not at all: its candidates are reserved from the budget
together, narrowing ones checked first, and a record whose candidates do not
all fit is not checked. That first record closes the budget, so every later
record is unchecked too, even a smaller one. An unchecked G1/G2/R2 record keeps
no list, as if the text were absent; an unchecked G4 record keeps its prose
repairs with no `fix_code`, as when the verifier cannot check the patch; and an
unchecked T2, A9 or REVL record gets no `fix_code`, so no `repairId` either.

The authority rule is the safety half. A candidate that enlarges the reach the
source *declares* is marked `widens_authority: true`, omitted unless the
caller passes `widens_authority=True`, and never ordered first. `add-requires`
is always widening: a new `requires` row enlarges the authority the component
declares, whether a sibling component or an ambient provider satisfies it, so
it is opt-in. `remove-call`, `rename-key` and `route-via-granted-service` are
always narrowing. Survivors are ordered narrowest authority first, then by the
smallest edit, then by kind, so the list is deterministic and a later learned
ordering can only reorder verified entries.

Verified means the patch clears the refusal and introduces no new diagnostic,
not that the rewrite preserves behaviour. `rename-key` renames the second
provider of the contested key, so a call site that reached that provider
through the original key now reaches the surviving one; the author picks
between the two providers by deciding which one keeps the key. The record's
`action` says which provider is renamed. A covered code is only repairable in
the modes its generator understands, so G1's `binding` mode and G2's "differs
from the running manifest" refusal keep the record they always had rather than
an empty list.

`revl explain G1` and `revl explain G2` print their candidate kinds. R2 stays
in the evolution curriculum's reserved set (issue #2028), so `revl explain R2`
still refuses it on purpose; its record carries the same list.

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
the LSP code action and the agent payload carry byte-identical edits.

The same engine backs the G4 `site-undo` repair above, so the edit a G4 record
carries in `repairs[i].fix_code` is byte-identical to the one
`fix_code(rejection, source)` returns for that refusal, and to the LSP quick
fix.

## The MCP front door: `revl_apply_repair` (issue #2855)

An MCP `revl_check` refusal carries the verified edit too: the record's
`fix_code` (or, for G4, the repair entry's) is the closed-loop result of the
same engine, and the refusal also returns `repairId`, a session-scoped name for
that edit. `revl_apply_repair {"repairId": ...}` applies it to the very text
the check compiled and returns the repaired candidate with its re-check, so a
client turn is not spent reconstructing the edit or re-sending the text. It is
explicit and read-only: the candidate comes back as `repaired` (the text the
check compiled, so with any terse punctuation the check completed), the
re-check as `check` (`revl_check`'s own answer to it: a pass, or the next
refusal, which mints its own id when that class is mechanical), and nothing is
installed. An id the session never minted is
`UNKNOWN_REPAIR`; one minted before a load, swap, rollback or undo moved the
unit of work is `STALE_REPAIR`. See
[mcp-reference.md](mcp-reference.md#revl_apply_repair).

## Adding a code

A new fixable code is one entry in `_GENERATORS`, its generator, and a proof in
`tests/test_fixgen.py`. The generator returns a candidate `Fix`; the engine and
both front doors need no change. A new code in the candidate-repair list is an
entry in `_REPAIR_GENERATORS`, its generator, a kind tuple in
`diagnostics.REPAIR_KINDS`, and a proof in `tests/test_repairs_2759.py`. The proof is end to end: the diagnostic, the
generated edit, and a re-check of the *patched* source showing the rejection is
gone — the same bar every code here already clears.
