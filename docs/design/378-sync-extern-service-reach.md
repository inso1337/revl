# 378: a sync extern that reaches a service

Design note for Cam's feedback #4: a SYNC extern cannot read a service the
composition provides. This is design-first. It changes no parser, typecheck,
lower, or runtime code; it records the shape of the problem, three ways out, a
recommendation, and a staged plan an implementation agent can pick up.

## The problem (Cam's words, grounded)

`author_model_complete` is a document-global sync extern with no component
context, so it cannot `require ProviderSettings`. The boot seam had to shuttle
the provider identity through environment variables. That is the architectural
wart behind the H19e/H23 saga, and it recurs every time mechanism needs
configuration. A sync extern that needs configuration has no way to declare a
dependency on a service the composition provides.

The concrete artifacts (`author_model_complete`, the `ProviderSettings` service,
the env-var boot seam) live in the downstream `revl-harness` repo, not in
`revl-core`. This repo holds the cause and the fix lineage. The cause is a
language rule (A1) plus a structural fact about how externs are emitted. The fix
lineage is items 90, 92, and 342, which already colored the async/arrow boundary
but left the configuration-reach boundary untouched. H19e and H23 are
revl-harness item numbers; the parent finding H19 is recorded here in roadmap
item 342 (`docs/v2.0-roadmap.md:3978`), where the sync tool-surface caller
`ToolRegistry.call` first hit this wall.

## Background: the two forms in the code

### A component provide method CAN require a service

A component declares its dependencies with a `requires` clause. The parser stores
them on the declaration as `(local, service, line)` triples
(`src/revl/parser.py:283-286`, parsed at `parser.py:1335-1365`). Lowering builds
a component `Env` whose `requires` maps each local binding to a service name and
whose `services` maps names to their declarations
(`src/revl/lower.py:401-411`). A method body that reaches `settings.provider()`
lowers through `_component_req_call` (`src/revl/lower.py:3577-3603`), which:

- refuses the reach if the binding was never declared: `"{root} is not a declared
  requirement of {component}"` (`lower.py:3578-3580`);
- resolves the service and method, checks arity and argument types;
- forces an emission method to be marked `emit` at the call site (G4,
  `lower.py:3590-3597`);
- emits `{"kind": "call", "target": {"kind": "req", "name": root}, ...}`.

The `req` target compiles, on the py tier, to `_revl_ctx.<binding>`, a
committed-view attribute on the cordis Context (`backends/python/emit.py:731`,
documented at `emit.py:21`). A component's config block is bound the same way, as
`_revl_config` in body scope (`emit.py:663`, `emit.py:714`), resolved at plug
time from the composition config map.

The load-bearing fact: **the require reach is the cordis Context, and the Context
exists only inside a component activation body.** The whole component body
compiles to one `_revl_ctx.effect(generator)` (`emit.py:14`). Outside that
generator there is no `_revl_ctx`, so there is nothing to read a service from.

### A document-global extern CANNOT

`ExternDecl` (`src/revl/parser.py:335-372`) carries a classification, params,
returns, host bodies, a capability scope (`capabilities`, `parser.py:350`), and
the modifier flags `async_`, `deferred`, and `requires_approval`. It has **no
`requires` clause**. There is no surface to name a service dependency, and the
lowered IR entry mirrors this: `_lower_externs` (`src/revl/lower.py:1753`) builds
`{"name", "class", "params", "returns", "bodies", ...}` plus the flags, and never
a service requirement (`lower.py:1913-1926`).

The emitter is where the gap becomes physical. `_emit_externs`
(`backends/python/emit.py:2057-2081`) renders each extern as a top-level module
function:

```
def author_model_complete(prompt):
    <verbatim @py body>
```

The only names in scope inside that body are the declared parameters and the
verbatim host lines. There is no `_revl_ctx`, no `_revl_config`, no service
handle. So the host body's only channels to the outside are (1) its parameters
and (2) ambient process state. Provider identity is not a parameter, so the
harness reached for ambient process state: environment variables. That is the
env shuttle, and it is the only channel the current extern shape leaves open.

## Why the reach is missing

A document-global extern is not inside a component, so it has no coeffect context
to carry a `require`. The require reach is not a free-floating capability; it is a
handle on the cordis Context, and the Context is threaded only through a component
activation. An extern's host body is emitted at module scope
(`emit.py:2057`), context-free by construction. Giving that body a service handle
means giving it a Context, and giving it a Context means giving it a home
component. This is the honest core of the problem, and every option below is a
different answer to "where does the extern get a Context."

The color lineage is adjacent but orthogonal. A1 (`docs/rejections.md:280-305`,
threat model at `docs/threat-model.md:56`) says a provide method runs while the
component is ACTIVE, where there is no iteration boundary to divert, so a sync
method or extern has no in-flight window and cannot `await`. The declared-sync-
but-async diagnostic that enforces it lives at `src/revl/lower.py:3593-3644`
(`code=A1, category=async-propagation`). Item 90 colored module fns that reach an
async extern (`docs/v2.0-roadmap.md:2993`, fixpoint in
`docs/design/async-extern.md:189-255`); item 92 colored async callback values;
item 342 (just landed, `docs/v2.0-roadmap.md:3978`) added sync/async arrow
polymorphism, monomorphized per call site, so one repair loop serves both a sync
and an async caller. Those three items solved **what color** the crossing carries.
None of them solved **what configuration** the crossing reads. Item 378 is the
configuration-reach axis, independent of color: a sync extern and an async extern
have the exact same "I cannot read a service" problem.

## Options

Three ways to let the mechanism read a service the composition provides, each a
different answer to the Context question.

### (a) Let the extern declare a `require`, threaded from the call site

Surface: an extern gains a `requires` clause naming a service, mirroring a
component's.

```revl sketch
extern emission fn author_model_complete(msgs: List[Msg]) -> Str
    requires settings: ProviderSettings
    = @py {
        # body somehow reaches `settings`
        ...
    }
```

Typing: the enclosing component must itself declare that requirement (or the
reach is refused, the G1/`_component_req_call` refusal generalized to externs).
The obligation is per call site, because a document-global extern can be called
from many components.

Lowering: the extern IR entry grows a `requires` list; the emitter adds a leading
hidden parameter for the resolved service handle; every call site, always inside a
component body with `_revl_ctx` in scope, threads `_revl_ctx.<binding>` in.

A1 and the capability gate: the service method the host body calls must be sync,
or A1 refuses (a sync extern reaching an async op, `lower.py:3593`). An ungranted
service stays a compile refusal, because the enclosing component must declare and
be granted the requirement.

Why this is the wrong shape. The host body is verbatim and unchecked (G8, item 24;
the gate does not sandbox host code, recorded in `docs/design/329-untrusted-author-profile.md`).
To use the threaded handle, the `@py` body must name it and call methods on it,
for example `settings.provider()`. That couples the verbatim host body to the
runtime service-handle ABI (the cordis committed-view object), and it moves a
service call inside the G8 blind spot, where `_component_req_call`'s audit,
arity check, and G4 `emit` marking do not run. A document-global extern also
inherits a whole-program obligation: every call site must satisfy the requirement,
which reintroduces at the extern the exact ambient coupling we are trying to
remove. And "give the extern a Context" is, once you demand audit and capability
discipline, just "give it a home component," which is option (c). Option (a)
looks like a small surface change but is a large and leaky semantic one.

### (b) A mechanism-configuration coeffect (typed config into a classified extern)

Surface: an extern declares a typed `config` block, the same shape a component
already has (`parser.py:283-285`), and the composition supplies it at plug time.

```revl sketch
extern emission fn author_model_complete(msgs: List[Msg]) -> Str
    config { provider: Str, endpoint: Str, model: Str }
    = @py {
        # body reads the bound config, not os.environ
        ...
    }
```

Typing: the config schema is checked like a component config schema; the
composition must supply every non-defaulted field, refused at load if missing
(mirroring `_print_plan`'s config listing and the component config-schema check).

Lowering: the extern IR entry grows a `config` schema; the emitter binds a
`_revl_config` local in the extern body scope, exactly as it does for a component
method (`emit.py:663`, `emit.py:714`); the driver resolves the config at plug time
from the composition config map and passes it in as a hidden leading argument.
This is a genuinely small change because the machinery already exists on the
component side; it is being made available to a classified extern.

A1 and the capability gate: there is no service reach and no async op, so A1 is
untouched. Config is static data, not a capability, so the capability gate is
untouched; a secret carried in config is the concern of the untrusted-author
admission profile (item 329), not of this coeffect.

What it fixes and what it does not. This is the honest, minimal replacement for
the env shuttle: environment variables ARE ambient, untyped, undeclared config,
and this makes them typed, declared, per-extern config injected at the sanctioned
plug seam (the seam roadmap item 350 already names as where the environment
contract belongs). It fits Cam's framing exactly ("every time mechanism needs
configuration"). It does NOT give the extern a live, swappable service: a
composition live-swap that changes the provider (item 351's `ProviderSettings`
handoff) would not reach a statically-configured extern, because the config is
resolved once at plug. Option (b) is the right tool when the mechanism needs a
static identity, and the wrong tool when it needs to observe a service whose
state changes at runtime.

### (c) Give the extern a home component (make it a provide method's mechanism)

Surface: no language change. The mechanism stops being a document-global extern
and becomes a method on a component that `requires ProviderSettings`. The raw host
crossing stays an extern, but a dumb one: it takes provider identity as ordinary
data parameters. The provide method holds the require reach and passes the
resolved identity down.

```revl sketch
component ModelClient requires settings: ProviderSettings provides model {
    provide model {
        fn complete(msgs: List[Msg]) -> Str =
            raw_model_post(settings.endpoint(), settings.model(), encode(msgs))
    }
}

extern emission fn raw_model_post(endpoint: Str, model: Str, body: Str) -> Str
    = @py { ... }
```

Typing, lowering, A1, capability gate: all already work. The service reach is the
existing `_component_req_call` path (`lower.py:3577`), audited, arity-checked, and
G4-marked. If the crossing is sync, this is a sync provide method reaching a sync
extern, no A1 issue. If it is async, it is an `async fn` provide method reaching an
async extern, the item 90/115 path that already compiles. The capability gate
applies because the reach is a real declared requirement. The host body stays a
context-free function over data parameters, exactly the shape `_emit_externs`
already produces (`emit.py:2057`), so nothing about the G8 boundary changes.

Cost: a wrapper component and the ceremony of a service where before there was a
bare function. For a mechanism that genuinely is a service (a model client is a
service), that ceremony is honest. For a leaf mechanism that only needs three
strings of static config, the wrapper is overhead, which is what option (b) is
for.

## Recommendation

Adopt (c) now and (b) next; reject (a).

- **(c) is the immediate, zero-compiler-change migration.** It unblocks
  `author_model_complete` today by restoring full stratum, capability, and audit
  discipline: the service reach goes back through `_component_req_call`, the host
  body goes back to being a ctx-free function over data, and the env shuttle is
  deleted. Nothing in the parser, typecheck, lower, or runtime has to move. For a
  model client, which is a service by nature, this is not a workaround; it is the
  correct decomposition, and it is the reason the require reach was built on the
  component in the first place.

- **(b) is the durable language addition for the recurring case.** Cam's wart
  "recurs every time mechanism needs configuration." Not every such mechanism
  wants to be a service; some are leaf host functions that need three typed
  strings and nothing live. For those, a per-extern config coeffect is the honest,
  minimal generalization of the env shuttle, and it reuses the component config
  machinery almost verbatim. It closes the ambient-env hole at the language level
  rather than by convention.

- **(a) is rejected.** Threading a live service handle into a verbatim host body
  couples that body to the runtime service-handle ABI, moves an un-audited service
  call into the G8 blind spot, and imposes a whole-program per-call-site
  obligation on a document-global. Its "give the extern a Context" is exactly
  (c)'s "give it a home component" once audit and capability discipline are taken
  seriously, so it buys a leaky surface for no semantic gain over (c).

The split is principled: (c) for mechanisms that ARE services and need a live
reach, (b) for mechanisms that only need static configuration. Together they cover
the whole of Cam's "mechanism needs configuration" without ever handing a
context-free host body a live Context it cannot audit.

## Migration for author_model_complete

The harness's `author_model_complete` needs the provider IDENTITY (which provider,
which endpoint, which model), not a live per-turn service handle, so both (c) and
(b) fit. Recommended path, in the harness:

1. Introduce a `ModelClient` component that `requires ProviderSettings` and
   `provides` the model service. Move the crossing into a `provide` method whose
   body reads `settings.provider()` / `settings.endpoint()` / `settings.model()`
   and calls a dumb `raw_model_post(endpoint, model, body)` extern that takes only
   data. This is option (c) and needs no revl-core change.
2. Delete the env-var boot seam. Provider identity now flows through the declared
   `requires ProviderSettings` edge, which is auditable and survives a live swap
   through the existing `handoff` on the settings state (the fix recorded in
   roadmap item 351, `docs/v2.0-roadmap.md:3996`).
3. Point every caller (the async `SelfEvolver.author` path and the sync
   `ToolRegistry.call` path from item 342) at the provided model service. The
   arrow-color polymorphism from item 342 already lets one repair loop serve both
   callers, so no sync twin is needed.

If, after this, a leaf mechanism in the harness still wants configuration without
a wrapper component (a plausible follow-up for smaller host functions), it adopts
option (b)'s extern `config` block once that lands, dropping any remaining env
reads.

## The honest hard part

A document-global extern with no component is context-free by construction. The
require reach is a handle on the cordis Context (`emit.py:731`), and an extern
host body is emitted at module scope with no Context (`emit.py:2057`). There is no
seam that both keeps the extern document-global AND hands it a live service handle
without either (a) leaking the runtime service-handle ABI into unchecked host code
and bypassing the G8 and G4 audit, or (b) demoting the reach to static config
resolved once at plug. So the design cannot make a truly context-free extern read
a live service; it must either give the extern a home (option c) or restrict what
it reads to static config (option b). The recommendation embraces that constraint
rather than fighting it: services get a home, configuration gets a coeffect, and
nothing hands a context-free body a Context it cannot audit.

## Staged implementation plan

Option (c) is a harness migration and a docs task, no compiler work. The staged
plan below is for option (b), the language addition, so a follow-up agent can pick
it up. It deliberately reuses the component config path.

- **Stage 1 (parser).** Add an optional `config` clause to `ExternDecl`
  (`src/revl/parser.py:335`), reusing the `ConfigField` list and the component
  config parsing at `parser.py:1352-1365`. No new keyword; `config` is already a
  contextual head. Exit: a fixture extern with a `config` block parses; an extern
  without one is byte-identical.

- **Stage 2 (lower + typecheck).** Carry a `config` schema onto the extern IR
  entry in `_lower_externs` (`src/revl/lower.py:1913`). Check the schema shape the
  same way component config is checked. Bind the config field names in the extern
  body's type env so a `@py`-tier reference to a config field is a known name.
  Exit: a config extern lowers with a `config` schema on its entry; a missing
  required field at a load site is refused with the component-config-style message.

- **Stage 3 (emitter, py first).** In `_emit_externs`
  (`backends/python/emit.py:2057`), bind `_revl_config` in the extern body scope
  when the entry carries a config schema, mirroring `_ComponentEmitter`
  (`emit.py:663`, `emit.py:714`). Add a hidden leading `_revl_config` parameter and
  thread it at every call site from the composition config map. Exit: golden py
  output binds `_revl_config` and reads config fields instead of `os.environ`; a
  no-config extern's golden is byte-identical.

- **Stage 4 (driver).** Resolve an extern's config at plug time from the
  composition config map in the driver (`src/revl/run.py`), so a config extern is
  configured through the same seam a component is (roadmap item 350). Exit: a
  composition that configures a config extern boots and the extern reads the
  supplied values; an unconfigured required field fails the load preflight.

- **Stage 5 (other tiers).** Repeat the emitter change for ts, then go/java/rust/
  wasm as their conformance requires, keeping the per-backend goldens green.
  Exit: the cross-tier conformance table (`docs/conformance.md`) shows the config
  extern behaving identically on every tier it targets.

- **Stage 6 (docs + capability note).** Document the extern config coeffect
  alongside component config, and state plainly that config is static data, not a
  live service; a mechanism that needs a live, swappable service uses option (c).
  Note that a secret in extern config is governed by the untrusted-author
  admission profile (item 329), unchanged.

## Exit tests

For option (c), the harness migration:

- `author_model_complete`'s successor is a `provide` method on a component that
  `requires ProviderSettings`; the raw crossing is a data-only extern.
- No `os.environ` / env read carries provider identity anywhere in the harness.
- A live provider swap changes the model target without a restart (the item 351
  handoff), verified by a model call succeeding after the swap.
- One repair loop serves both the async `SelfEvolver.author` caller and the sync
  `ToolRegistry.call` caller (item 342), with no duplicated sync twin.

For option (b), the language addition:

- A fixture extern with a `config` block parses, lowers, and emits with
  `_revl_config` bound; a no-config extern is byte-identical across parse, IR, and
  every golden.
- A load site that omits a required config field is refused with a
  component-config-style message, at compile or load, not at runtime.
- The config extern behaves identically on py and ts (and every other targeted
  tier) under the conformance harness.
- A1 is untouched: a sync config extern stays sync, and no config extern gains an
  async color or an `await`.
- The capability gate is untouched: a config extern reaches no service, so it
  needs no grant; `docs/design/329-untrusted-author-profile.md`'s `no_extern`
  admission still refuses a model-authored source from declaring one.
- `pytest tests/test_doc_examples.py` stays green: this note's proposed-syntax
  blocks are marked `sketch` and must not compile until the feature lands, at
  which point they are promoted to worked examples.
