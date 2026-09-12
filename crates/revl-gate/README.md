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

## This gate issues no admissions

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

So there is no `Verdict::Admitted` and no `is_admitted()`. The non-refusing arm
is `Verdict::NoObjection`, meaning *"this gate found nothing it is able to
refuse"* — never *"the reference would admit this"*. A host that must ADMIT,
because it is about to run the program, still needs a reference verdict
(`revl compile`, or `revl.gate.admit` on py). What this crate buys is the other
direction: a local, in-process, Python-free REFUSAL that agrees with the
reference byte for byte on the covered corpus.

The asymmetry is the whole design: refusing what the reference admits is an
inconvenience; **admitting what the reference refuses is the defect class the
admission-gate arc exists to prevent.** A crate that cannot issue an admission
cannot commit that defect.

On the wire, `to_json()` emits `"admitted": false` for **every** arm, so a
consumer written against the design's fixed `{admitted, code, message}` shape
reads this gate as "never admits" rather than misreading a no-objection. The
arm itself travels in the extra `"verdict"` field
(`"refused"` / `"no_objection"` / `"outside_frontier"`).

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
    // frontier "selfhost-admit:7a45a990cda8887b"
    // layer    "composition + guarantee layer (G1..G4, A1, PRELUDE) and parse (BAD); NOT the reference type layer"

`api` is the gate surface semver (bumped by surface changes only); the
navigation surface carries its own, `SYMBOLS_API_VERSION`. `language` is
the revl version this gate's refusals are drawn from. `frontier` identifies the
COVERED surface: two gates with different frontier ids cover different
languages, and their agreement carries no information. `layer` says in prose
what the gate decides. Codes are append-only; message text is not promised
stable across versions.
