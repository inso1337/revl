# revl-gate

The revl admission gate as an embeddable rust library. Roadmap item 332,
Stage 3; design: `docs/design/332-embeddable-gate-api.md`.

**GENERATED — do not edit by hand.** Every file in this directory is written by
`tools/build_gate_crate.py` from the self-host compiler sources. CI regenerates
from the same tree and fails on any byte difference
(`tests/test_gate_crate_drift.py`). To change the crate, change the generator.

    python3 tools/build_gate_crate.py            # regenerate
    python3 tools/build_gate_crate.py --check    # the drift gate

## What it gives you

```rust
use revl_gate::{admit, Verdict};

match admit(source) {
    // Definitive, and byte-agreeing with the reference on the covered corpus.
    Verdict::Refused { code, message } => reject(code, message),
    // NOT an admission — see below.
    Verdict::NoObjection => ask_the_reference(source),
    // The gate declined to decide at all.
    Verdict::OutsideFrontier { reason } => ask_the_reference(source),
}
```

`admit` is a pure function: no disk, no clock, no live state, no cordis runtime
boot. It is `selfhost/lower.rvl`'s `admit_src` — the native lex / parse /
composition-guarantee chain — compiled to rust through the reference rust
backend.

`admit_into` is the same gate across a composition boundary: the verdict on a
candidate once it is admitted INTO a RUNNING composition (item 186).

```rust
use revl_gate::{admit_into, Verdict};

// The running composition, in item 186's row wire: `Kv` provides `store`,
// `App` provides `app` and requires `store`.
let running = "Kv/store/;App/app/;App<store";

match admit_into(candidate, running) {
    Verdict::Refused { code, message } => reject(code, message),
    Verdict::NoObjection => ask_the_reference(candidate),
    Verdict::OutsideFrontier { reason } => ask_the_reference(candidate),
}
```

The crate builds with no Python on the machine. That is why the generated source
is committed rather than produced at install time (items 336 and 338 depend on
it).

## The verdict surface issues no admissions

Read this before wiring the crate into anything.

The self-host compiler is behind the reference implementation (roadmap item
391), and the gap is not "a few missing constructs" — it is a whole missing
LAYER. `admit_src` decides the composition and guarantee layer (`G1`..`G4`,
`A1`, `PRELUDE`, and parse failures as `BAD`). It does **not** run the
reference's type layer. Measured, not assumed: the reference refuses all of

    fn f() -> Int { return "s" }
    fn f() -> Int { return undefined_name }
    fn f() -> { }

and the self-host gate raises no objection to any of them.

So `Verdict` has no admitting arm and no `is_admitted()`. Its non-refusing arm
is `Verdict::NoObjection`, meaning *"this gate found nothing it is able to
refuse"* — never *"the reference would admit this"*. On the wire, `to_json()`
emits `"admitted": false` for **every** arm, so a consumer written against the
design's fixed `{admitted, code, message}` shape reads the verdict surface as
"never admits" rather than misreading a no-objection. The arm itself travels in
the extra `"verdict"` field
(`"refused"` / `"no_objection"` / `"outside_frontier"`).

The asymmetry is the whole design: refusing what the reference admits is an
inconvenience; **admitting what the reference refuses is the defect class the
admission-gate arc exists to prevent.**

## The admission surface (issue #346)

An admission is a SECOND question, asked through a second type so the two cannot
be confused: `issue_admission(source)` returns an `Admission`, not a `Verdict`.

```rust
match revl_gate::issue_admission("service Store { fn get(key: Str) -> Str }") {
    revl_gate::Admission::Admitted { basis } => println!("admitted: {basis}"),
    revl_gate::Admission::Withheld { verdict } => println!("withheld: {verdict:?}"),
}
```

`Admission::Admitted` is reachable through exactly one path, and both conditions
are necessary:

1. `admit(source)` returned `Verdict::NoObjection` — the composition/guarantee
   gate ran and found nothing to refuse. An admission is never issued over a
   refusal or a frontier gap.
2. the source is inside the ADMISSION SURFACE (`ADMISSION_SURFACE_ID`,
   `src/admission.rs`) — the region where the covered layer is the WHOLE
   question, because the source carries no term the type layer decides.

The surface is deliberately tiny, and its one line is `ADMITTED_LAYER`:

    interface declarations only: service method signatures and scalar type aliases, over a closed scalar type vocabulary; no term the reference type layer decides

That is not the covered layer read optimistically; it is the sliver of it where
reading a no-objection as an admission is sound. No body, no expression, no
literal, no generic head. A source outside it is `Admission::Withheld` carrying
the verdict verbatim, so switching a consumer from `admit` to `issue_admission`
can only ADD the admitted wire — every other answer is byte-identical to the one
it already handled. Widening the surface is the self-host type layer's lane
(`docs/design/457-selfhost-type-layer.md`).

`issue_admission_into(source, manifest)` asks the same question against a running
composition. The empty manifest is the empty composition, so it is
`issue_admission` byte for byte. Against a NON-EMPTY manifest only a candidate
that DECLARES NOTHING is admitted, and the reason is the item-186 row wire rather
than the certifier: a row carries a component name, a provision key and a realm,
and no service shapes, so a declared `service Store` may collide with a `Store`
the running composition already holds in a different shape and the wire cannot
say. The reference refuses exactly that pair. Carrying the running shapes on the
wire is the remaining half of issue #346.

An issued admission serialises `{"verdict":"admitted","admitted":true,
"code":null,"message":null}` — byte-identical to `revl.gate`'s own wire for a py
admission, so a seam comparing the two tiers compares equal bytes. The basis is
off the wire on purpose: it is evidence for a log, not part of the contract.

## Fail closed at the frontier

`Verdict::OutsideFrontier` means *this gate is not entitled to decide*, and the
crate returns it whenever:

* the source uses a construct in the generated frontier table below;
* the source is larger than the bound the gate will decide (a stack overflow in
  the deeply-recursive native front end ABORTS, and an abort cannot be turned
  back into a refusal);
* the source has more items at one bracket level than the gate will decide —
  the emitted parser recurses once per SIBLING item, so a flat `g(1, 1, …)` a
  few KB long and one bracket deep exhausts the stack where neither the size
  bound nor the nesting bound can see it. The bound is
  `revl_gate::MAX_LEVEL_ITEMS`;
* the native gate panics while deciding (caught via `catch_unwind`);
* the native gate returns a verdict wire shape this crate does not recognise;
* in `admit_into`, the manifest wire is longer than the bound the gate will
  decide, or carries more `;`-separated rows than the gate will fold, or the
  fold returns a shape this crate does not recognise. Both manifest limits are
  checked BEFORE the wire reaches the parser, because the byte bound is not a
  row bound: the fold consumes one stack frame per row, so 2 700 rows of
  `A/b/;` are 13 KB and still take a 1 MiB stack down. The row half of the pair
  is `revl_gate::MANIFEST_ROW_LIMIT`.

### The generated frontier table at this generation

Not hand-listed. `tools/build_gate_crate.py` computes it as the difference
between the reference compiler's own tables and the self-host sources, so a
reference construct added without a self-host port changes these bytes and reds
the drift gate.

* Reference keywords the self-host does not lex: (none at this generation)
* Reference stdlib builtins the self-host does not lower as builtins:
  (none at this generation)

## The manifest arm (issue #346)

`admit_into(source, manifest)` asks a different question from `admit`: not "is
this text well formed on its own", but "does this text compose with the
composition that is ALREADY RUNNING". `source` is decided against the UNION of
the manifest and the incoming text, so a key the running composition already
holds conflicts (`G2`), a route into a realm the union does not provide dangles
(`G2`), and a dependency cycle spanning the manifest boundary is a cycle
(`G3`). The decision is the native fold `selfhost/lower.rvl::admit_ambient`,
compiled to rust like `admit`, and the manifest arrives as item 186's row wire
(`docs/design/186-ambient-admission-guarantees.md`): `C/k/r` for a provision
(`r` the realm, `""` for shared), `C<k` for a requirement, `!halted` for a
halted composition, joined by `;`. The empty manifest is the empty composition,
so `admit_into(source, "")` is `admit(source)` byte for byte — the arm
generalises `admit` rather than re-implementing it.

Two honest limits, both fail-closed:

* **It closes the `G2`/`G3` legs and nothing else.** The reference TYPE layer is
  its own lane (the self-host compiler has no type layer yet), so a
  type-incorrect candidate is a no-objection here, exactly as in `admit`. This
  arm does not RESOLVE the requirements a candidate declares either; it checks
  them for disjointness and acyclicity. The reference remains the only tier that
  admits.
* **A row it cannot honour is REFUSED, never skipped.** The wire reserves row
  kinds for waves that have not landed — replacement (`-C`) and handoff
  (`C=k:T`), both of which need the type layer. Those rows come back as a
  `MANIFEST` refusal. Ignoring a row would be the wave-through this crate exists
  to prevent.

## What is deliberately absent

* **`compile_to` output.** Exported, and it refuses unconditionally: the
  self-host emitters still carry `@py`-only helper externs and do not emit to
  rust. Stage 4's lane.
* **The reference type layer.** Still absent, in `admit` and in `admit_into`
  alike: neither arm issues an admission. That lane is the type layer's, not the
  manifest parameter's.
* **The deferred manifest rows.** Replacement and handoff rows are refused, for
  the reason in the section above: they need the type layer.
* **Layer 2 (the session surface).** `revl_gate::session::Session` is item 334's
  foundational first slice: the generation state machine, the untrusted-author
  admission entry (`propose`/`admit`/`admit_into`), and the item-245
  witnessed-call recording path (`call`/`commit`/`abort`/`unload`). The
  accept-and-swap half, the witnessed-effect runtime, the WAL and the approver
  callback are later slices; a candidate the native gate does not refuse is
  fail-closed, never admitted. `Session::admit_into` takes the manifest wire as
  a PARAMETER, not as a projection of the loaded composition: a manifest also
  needs requirements and realms, and synthesising rows out of what the session
  holds would be inventing a running composition.

## Host obligations

* Build the calling profile with `panic = "unwind"`. Under `panic = "abort"` the
  fail-closed panic path cannot run and a native gate abort takes the process
  down instead. Loud, so still not a false admission — but not the intended
  behaviour.
* The default panic hook prints to stderr when the fail-closed path fires.
  Install your own hook if that matters.

## The navigation surface

```rust
use revl_gate::symbols::{symbols, Symbols};

match symbols(source) {
    // Every top-level declaration this crate will resolve, and the line each
    // was declared on. A name that is ABSENT is not "undeclared" — it is "not
    // resolvable here", and the caller must ask the reference.
    Symbols::Table(rows) => navigate(rows),
    // Not entitled to answer for this document at all.
    Symbols::Undecided { reason } => ask_the_reference(source),
}
```

A second surface over the same front end, for editor navigation (roadmap item
336 slice 2): go-to-definition and the signature half of hover. It issues no
verdicts — a program the gate REFUSES still has declarations to navigate — so it
is versioned by `SYMBOLS_API_VERSION`, not by the gate api above.

Its fail-closed rule mirrors the gate's, pointed at the risk navigation actually
carries. A navigation engine that answers nothing merely fails to jump; one that
answers WRONGLY sends a developer to the wrong declaration. So it answers only
what it can answer exactly, and everything else is an absence: a construct the
self-host parser cannot read makes the whole document `Undecided`, a name a
parameter or `let` might shadow is dropped (this crate cannot see scopes), and a
signature it cannot spell the way the reference spells it comes back as
`detail: None`.

## Versions

    revl_gate::gate_version()
    // api      "1.0.0"
    // language "2.0.0"
    // frontier "selfhost-admit:642b249fa2ddf80c"
    // layer    "composition + guarantee layer (G1..G4, A1, PRELUDE) and parse (BAD); NOT the reference type layer"

`api` is the gate surface semver (bumped by surface changes only); the
navigation surface carries its own, `SYMBOLS_API_VERSION`. `language` is
the revl version this gate's refusals are drawn from. `frontier` identifies the
COVERED surface: two gates with different frontier ids cover different
languages, and their agreement carries no information. `layer` says in prose
what the gate decides. Codes are append-only; message text is not promised
stable across versions.
