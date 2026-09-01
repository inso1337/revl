# Dogfood findings — agent/ux-probe

Agent-experience probe: rate-limited caching composition built from scratch
using only agent-facing docs (`docs/guide-ai-agents.md`, `README.md`,
`docs/stdlib-2.0.md`, `docs/arithmetic.md`, `docs/function-types.md`,
`docs/generics.md`, `docs/capabilities.md`, `examples/`). Branch:
`agent/ux-probe`.

## 1. Refusal log

#### R1 (cycle 2) — `emission fn` inside a `provide` block
Snippet refused:
```revl
provide cache {
  fn get(key) = store.get(key)
  emission fn put(key, value) { ... }
}
```
Diagnostic verbatim:
```
error: examples/uxprobe_cache.rvl:19: expected fn, found 'emission'
```
Verdict: **friction**. The refusal is correct — the guide's model is
"a service declaration bounds its providers; a provider may be purer than
declared", so the provider inherits the service's purity and re-declaring it
is meaningless. But:
- the diagnostic is a bare parser message with no hint, no code, no
  `revl explain` anchor — the exact opposite of the guide's promise that
  "each names the guarantee and the fix";
- no agent-facing doc states whether `emission fn` is *allowed-but-redundant*
  or *forbidden* inside `provide`. The guide's only provide examples use bare
  `fn`. One sentence in guide-ai-agents.md ("provide-methods take no purity
  modifiers") would have prevented this cycle entirely.

#### R2 (cycle 5) — `assert call …` nested in a lifecycle test
Snippet refused:
```revl
assert call limiter.allow("beta") == true
```
Diagnostic verbatim:
```
error: examples/uxprobe_cache.rvl:62: expected a lifecycle statement, found 'limiter'
```
Verdict: **friction**. Correct refusal — lifecycle bodies take a fixed
statement grammar and `call` results must be bound with `let` before an
`assert`. But the message reads like a parser confusion ("found 'limiter'")
rather than the rule ("bind `call` to a `let`, then assert"). I fixed it by
guessing the `let gated = call …; assert gated == true` shape from the
sibling lines, not from the message.

#### R3 (deliberate rejection experiment) — plain-declared `get` whose body emits
Snippet refused (dogfood/scratch-rejection.rvl):
```revl
service Cache {
  fn get(key: Str) -> Opt[Str]   // deliberately plain
  emission fn put(key: Str, value: Str)
}
component BadCache requires db: Database provides cache: Cache {
  provide cache {
    fn get(key) {
      emit db.execute(`SELECT ${key}`)
      return None
    }
    ...
```
Diagnostic verbatim:
```
error: dogfood/scratch-rejection.rvl:15: `Cache.get` is declared plain, but this implementation reaches `db.execute`
  a service declaration bounds what its providers may do — mark it `emission fn get(...)` in service `Cache`, or move the irreversible call out of this method (G4)
  why `Cache.get` is emission:
    get -> db.execute   (emission `Database.execute`)
      get         dogfood/scratch-rejection.rvl:15  provision `cache`
      db.execute  dogfood/scratch-rejection.rvl:5  emission `Database.execute`
```
Verdict: **caught-bug** — the dream. This is precisely the failure mode the
guide says is the corpus's largest bucket, and the diagnostic is exemplary:
it names the operation, states the upper-bound rule in one line, gives two
concrete rewrites, and carries the full `why` derivation with source
locations. `revl explain G4` then restates the guarantee. Nothing to fix on
my side; this is the diagnostic every other refusal should aspire to.

#### R4 (docs-gap probe) — plain `test` block driving a composition
Snippet refused (dogfood/scratch-plain-test.rvl):
```revl
test "round trip via plain test block" {
  call cache.put("k", "v")
  ...
```
Diagnostic verbatim:
```
error: dogfood/scratch-plain-test.rvl:18: `call` is only allowed in a `lifecycle test` body
  a plain `test` block is pure (syntax-2.0 §7); write `lifecycle test "name" { ... }` to drive a composition (§7.1)
```
Verdict: **caught-bug** at the checker, **gap** in the docs. The refusal is
right and its hint names the exact rewrite. But note where I learned the
existence of `load` / `call` / `unload` / `assert no_residue` /
`lifecycle test`: NOT from guide-ai-agents.md (which only shows pure-fn
tests under `revl test`) — from reading examples/lifecycle_cache.rvl. The
entire component-testing DSL is undocumented in agent-facing docs; the hint
even cites syntax-2.0 §7.1 for it.

## 2. Friction log

- `[slow]` `syntax-2.0.md` — the document the guide cites for every grammar
  detail ("see syntax-2.0.md §3.3", "§4b.1", "§7.1") — was NOT on the
  allowed-materials list for this probe. An outside agent following only
  guide-ai-agents.md hits citations to a doc it may or may not have; every
  stratum-3 question bottoms out in a doc outside the "agent-facing" set.
  The allowed list itself is a docs-gap: either syntax-2.0.md is agent-facing
  or the guide should inline what it cites.
- `[blocker]` `revl test` from the repo-root venv fails with
  `ModuleNotFoundError: No module named 'cordis'`, surfaced as three test
  FAILs ("0 of 3 passed"), not as a preflight error naming the remedy. The
  working interpreter is a *second* venv at `backends/python/.venv`, created
  by `sh backends/python/setup.sh` — a fact documented only in that script's
  header comments; the agent guide never connects `revl test` to cordis-py.
- `[slow]` The lifecycle-test DSL (`lifecycle test`, `load X with {…}`,
  `call key.op(...)`, `unload X`, `assert no_residue`) appears nowhere in
  agent-facing docs — only in examples/lifecycle_cache.rvl and in a
  diagnostic hint citing syntax-2.0 §7.1. I built the round-trip test by
  imitating an example, which worked, but examples are not a spec.
- `[slow]` No signature spec for host objects: stdlib-2.0.md mentions the
  Map family's method names only inside a checker-mechanics aside; nothing
  states what `Map.new()` returns or that values are `Str`. Copied
  tenants.rvl blind and got lucky.
- `[nit]` Provide-methods take no purity modifiers (`emission fn put` inside
  `provide` is a bare parser error); one sentence in the guide would save a
  cycle (see R1).
- `[nit]` `revl compile` dumps full IR JSON on success with no documented
  quiet/emit-to-file flag in the agent guide; every compile check needs
  `>/dev/null 2>err` plumbing.

## 3. What revl gave you

1. **The G4 upper-bound check caught the seeded bug exactly** (R3 above).
   I declared `get` plain, hid an emission inside its provider body, and the
   checker refused with the rule, two concrete rewrites, and a sourced
   derivation (`get -> db.execute`, both locations). In TS/Python this
   compiles, runs, and silently makes every cache read irreversible.
2. **`assert no_residue` proved teardown for free.** Both components acquire
   state via `effect … undo …` (the Map store; three seeded allowlist keys),
   and the py-tier runtime verified after unload that nothing was left. That
   is the test I would otherwise hand-write per component (and forget on the
   third one); here it is one line, and it also caught nothing because there
   was nothing to catch — which is the point.
3. **`revl audit` gives a composition-level honesty report at zero cost.**
   One command: load order (providers first), each component's requires /
   provides / boundary — mine came back `none — fully revertible (G8)` —
   plus a distributability verdict per service. Free architecture review.
4. **Host-method checking was quietly on duty.** Every `store.get/insert/
   remove/drop` call was validated against the Map host family's surface
   (stdlib-2.0.md documents this exists); a typo'd method name would have
   been refused at compile, not crashed the runtime mid-lifecycle-test.

## 4. Time-to-green

Compile→refuse→fix cycles: **3** (R1 parser refusal, R2 lifecycle-statement
refusal, and one tooling cycle: `revl test` → cordis ImportError →
setup.sh → correct venv). Two further refusals (R3, R4) were deliberate
probes, not debugging. First-pass compile rate on non-experimental code:
lines written before first successful compile of each increment stayed at
~10–20 lines; only two increments ever failed.

Longest single stall: **the cordis runtime environment** (~4 commands +
reading `backends/python/setup.sh`). `revl test` reported
`ModuleNotFoundError: No module named 'cordis'` as a *test failure*
("FAIL … 0 of 3 passed") rather than a tooling preflight error, from a venv
that has no reason to contain cordis. What would have shortened it to zero:
(a) `revl test` detecting the missing runtime and printing the exact remedy
(`sh backends/python/setup.sh`, then use `backends/python/.venv/bin/python
-m revl test`) instead of a stack-shaped FAIL; or (b) one paragraph in
guide-ai-agents.md's check→run loop section saying which interpreter must
run `revl test`.

Second-longest: inferring the `Map` host-object surface. stdlib-2.0.md names
the family (`new/get/insert/remove/drop`) in prose about checker mechanics
but never gives signatures or the value type; I copied the idiom from
examples/tenants.rvl and it worked first try — good example, missing spec.
