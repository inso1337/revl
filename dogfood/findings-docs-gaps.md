# Findings — agent/docs-gaps (docs batch closing probe rounds 1-2)

This batch closed the agent-facing documentation gaps reported by
agent/uxprobe2 (see findings-uxprobe2.md on agent/uxprobe2). Every claim
written into the guide was verified by compiling a scratch .rvl first.

## Gaps closed

1. **Provide-methods take no purity modifiers** — now stated in guide
   "Rules that bite" with a fragment showing the plain-`fn` form. (Closes
   uxprobe2 R1: bare parse error, no hint.)
2. **Extern acquire `undo <expr>` syntax** — verbatim compiling example added
   to guide "Host blocks", plus a per-classification slot table. (Closes R3:
   six-attempt syntax discovery.) Verified: the slot sits between the return
   type and the first `= @backend { … }` body.
3. **Lifecycle + fault-test DSL** — new guide section "Driving a live
   composition" with complete compiling examples for `lifecycle test`
   (load/with/call/unload/no_residue) and `fault test … for Component`
   (`fail at step N`; only `fail at …` and `assert …` parse in the body),
   linking docs/syntax-2.0.md §7.1, docs/fault-tests.md, docs/holes.md.
   (Closes R7.) README's fault-test one-liner now includes `for <Component>`.
4. **Language small print** — new "Small print" box: function-scoped lets
   (verified: a let inside `if` is visible after the block), newline-only
   statement separation (`;` refused), no string escapes (`"a\nb".length()
   == 4`, verified by execution), activation bodies are effect-forms only.

## Learned while verifying (parser + scratch programs)

- The extern compensation slot exists for **both** `acquire` (optional, next
  to the required `undo`) and `emission`; `pure` refuses both, and `emission`
  refuses `undo` outright ("emissions are one-way boundary crossings"). The
  guide's slot table records exactly this.
- `emit X compensate Y` is a statement form; the fragment added to Host
  blocks compiles inside a provide-method scaffold.
- Shadowing a function-scoped `let` is refused ("`x` is already declared in
  this function"), which is what makes function-wide scoping safe to state.

## Recommendation (repeat of probe round 2)

Add docs/holes.md and docs/fault-tests.md to the standard agent-facing
allowlist. Both features are now surfaced from the guide, but the deep specs
still live in docs an agent brief may exclude; the exclusion cost two probe
rounds real cycles.

## Verification

tests/test_doc_examples.py: 95 passed, 1 skipped — all new fences compile as
complete programs or parse as fragments under the gate. Full suite: see
commit-time run below.
