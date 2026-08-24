# findings-mocks — roadmap item 60: auto-mocks, `revl test --mock-requires`

Branch `wave60-mocks` off `origin/devwip` @ 8a20a63. Delivers the py-tier
mock world: every unmet `requires` a lifecycle test leaves is filled by a
generated in-memory provider whose responses come from the item-37
generators (typed, seeded, deterministic); an `emission` operation is
recorded-not-crossed and the report says what would have been emitted.
`src/revl/mocks.py` + CLI wiring (`__main__.py`, `test.py`), suite
`tests/test_auto_mocks.py`, doc `docs/auto-mocks.md`.

The worktree arrived with an interrupted agent's `src/revl/mocks.py` (456
lines, well-documented) and its `__main__.py`/`test.py` wiring, uncommitted.
Assessment: functionally complete in design; one real bug found and fixed
(`_strip_to_base` dropped the `tests` section, so the emitted base module
lost the `_revl_i64`/`_revl_i32`/`_revl_div` helpers the driver's expression
renderer references — a test-body Int expression NameError'd; fix: keep
`tests` in the base module, strip only fault/prop sections, mirroring the
fault runner which emits the full document). Everything else held up under
probing: zero-setup boot, typed responses, recorded-not-crossed emissions,
cross-run reproducibility, real-provider precedence, shared mocks across
consumers, reload, async ops, config, residue detection.

## 1. Refusal log

Every `revl compile` rejection hit while writing the mock-world fixtures.

1. `_scratch/db_app.rvl:13: call to emission db.execute must be marked emit (G4)` —
   called `db.execute(sql)` bare in a provide method. Verdict: **caught-bug** —
   the rule is the point (an emission crossing must be visible at the call
   site); I had simply forgotten the marker.
2. `_scratch/db_app.rvl:12: Api.query is declared plain, but this implementation reaches db.execute` —
   a plain service fn that reaches an emission must itself be declared
   `emission fn`. Verdict: **caught-bug** — and the diagnostic's `why
   Api.query is emission:` chain (query → db.execute, with file/line per hop)
   is the best refusal I hit all run.
3. `_scratch/typed_app.rvl:17: provision api is missing method audit declared by service Api` —
   I declared `emission fn audit` in the service but omitted the method from
   the provider. Verdict: **caught-bug** — the checker enforces the service
   declaration as an upper bound on its providers in both directions.
4. `_scratch/typed_app.rvl:31: audit implements Api.audit, which returns Unit, but this body never returns a value` —
   wrote `fn audit(msg) { }`; a `Unit` operation must end with an explicit
   `return None`. Verdict: **friction** — correct rule, but see cost ledger:
   the Unit literal spelling is not documented anywhere I could find.
5. `_scratch/typed_app.rvl:31: expected an expression, found ')'` — wrote
   `return ()` for Unit. Verdict: **friction** — there is no `()` literal;
   `None` is the spelling, discoverable only by grepping examples.
6. `_scratch/async_app.rvl:13/14: expected an expression, found 'await'` —
   `return await fetch.fetch(url)` and `let r = await fetch.fetch(url)` both
   refused: `await` is a *statement*, not an expression, even in async
   provide-method bodies. Verdict: **friction** — syntax-2.0 §5 says async
   methods "may `await` host async values" but never says the awaited value
   cannot be bound; I had to read parser.py to learn the shape.

## 2. Friction log

- [slow] G4 emission transitivity took two compile→refuse cycles to absorb
  (mark the call site, then mark the declaring fn). The second diagnostic
  explains itself well; the *rule* just needs one more sentence in the docs.
- [slow] `Unit`-returning provide methods: no documented literal. `return ()`
  refused; `return None` (no docs) accepted. Cost two cycles.
- [slow] `await` statement-only in method bodies — the awaited value cannot
  be bound, so the natural "call an async mocked op and use its result"
  shape is inexpressible; fixtures had to await-and-discard. Syntax-2.0 §5
  is silent on this.
- [nit] A `revl fragment` fence that compiles standalone is *rejected* by
  tests/test_doc_examples.py ("it is a program, so drop the marker"). I had
  to read the gate's docstring to learn the convention; the gate's error
  message reprints the rules, so it self-explains once hit.
- [nit] Design constraint discovered only in source: a lifecycle test's
  `call key.op(…)` is checked against the *document's* loaded providers
  (lower.py `_lower_lifecycle_call`), so mock world drives the consumer
  through its own provided keys — you cannot `call db.query()` a requires
  key directly even though a mock exists for it. Not a blocker (the natural
  scenario works), but it is a real boundary of the feature and now
  documented in docs/auto-mocks.md §3.
- [nit] My own shell probes kept masking exit codes behind `| tail`; re-ran
  twice before noticing. Tooling, not revl.

## 3. What revl gave you

- The G4 checker stopped me three separate times from shipping a
  provider that reaches an emission without saying so — each refusal came
  with a precise file:line and a hop-by-hop `why X is emission:` trace. This
  is the service-declaration-as-upper-bound guarantee doing exactly the work
  the roadmap promises, and it shaped the mock-world fixtures into legal,
  boundary-honest programs.
- `assert no_residue` caught a *mock* left loaded: `provisions: [] ->
  ['api', 'kv'] (R4)` — the mock registered as a first-class provision and
  the residue check named it. That single failure proved the mock world is
  not a rubber stamp: mocks plug, resolve, and must be unplugged like any
  provider.
- The item-37 generators gave every response for free: `Int`/`Bool`/`Str`/
  `Float`/`List`/`Opt`/`Map`/`Result`, user records and ADTs — the mock
  module invents zero value-synthesis code (`fault._gen_random` reused
  verbatim), and a generator miss degrades to the type's structural zero
  instead of crashing a test.
- The reference tier's runtime seams (`plug`/`provide`/`Frame`) meant the
  mock is ~40 lines of glue: a runtime-constructed component of the exact
  shape the emitter renders for a real provider, so it reverts with no
  residue by construction.
- Reproducibility fell out of the existing `random.Random(seed)` contract:
  two full CLI runs of one document are byte-identical, asserted in the
  suite.

## 4. Time-to-green

~7 compile→refuse→fix cycles across the fixture-writing phase (see refusal
log), then one genuine debugging stall: the `NameError: name '_revl_i64' is
not defined` inside the mock-world driver. Found fast — `_uses_bounded_int`
gates the helper on whole-IR scanning, and stripping the tests section had
removed the trigger — but it is the kind of cross-boundary invariant (the
driver evaluates renderer output against a stripped module) that only
surfaces at runtime. The fix is structural: keep `tests` in the base module
(the fault runner's precedent), now pinned by a regression test.

## 5. Cost ledger

- `diagnostic` — 2 cycles: the G4 emission-transitivity rule (message told
  me how to fix the call site but not that the declaring fn must be
  re-marked).
- `docs-gap` — 2 cycles: Unit literal spelling (`return None`; `()` refused;
  nothing in syntax-2.0.md).
- `docs-gap` — 2 cycles: `await` statement-only in provide methods; the
  §5 prose reads as if the awaited value is usable.
- `docs-gap` — 1 cycle: `revl fragment`-that-is-really-a-program rejection
  (gate is self-explaining, but a sentence in docs/conformance.md "Doc
  examples are compiled" would have preempted it).
- `env` — 0 wasted cycles: the worktree's `backends/python/.venv` is a
  symlink to the main checkout's, so the cordis-gated tier ran without
  re-running setup.sh.

The single change that would have cut the most cost: two sentences in
syntax-2.0.md §5 — the Unit return spelling and the statement-only `await`
— which removes four of the seven cycles.
