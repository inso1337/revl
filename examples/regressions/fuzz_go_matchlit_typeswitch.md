# Cross-tier divergence: `go` disagrees with the `py` reference (NEW)

- Found by: `tools/fuzz_cross_tier.py` (roadmap item 292), then minimized by hand.
- Divergence kind: **build** (the go emitter produces syntactically invalid Go).
- Tiers that disagree: `py` (reference) runs and returns `1`; `go` fails to build
  the code its own emitter produced.
- Maps to roadmap item: **NEW** — not 280 / 302 / 304. Those were Opt/Result
  value and resolve gaps; this is a syntactic build failure on a *literal*
  match scrutinee.

## Property under test

One composition has the same meaning across tiers. The frontend ADMITTED this
program and the `py` reference ran it; `go` could not build the emitted code
(a compiles-implies-runs violation).

## Minimized program (`fuzz_go_matchlit_typeswitch.rvl`)

```revl
pub fn probe() -> Int { return match Ok(1) { Ok(o) => o, Err(e) => 0 } }
test "cross_tier_probe" { assert probe() == 1 }
```

## Per-tier outcome

- `py` (reference): pass — returns `1`.
- `go`: fail — `go test exited 1`:

```
FAIL	revltest [setup failed]
FAIL
# revltest
gen_test.go:21:34: expected '}', found Value
```

## Root cause

The match on an Opt/Result **constructor literal** (`match Ok(1) { ... }`) lowers
to a Go type switch whose init clause holds the scrutinee as a composite literal:

```go
switch _m := RevlOk[int64, any]{Value: 1}.(type) {   // emitted
```

A composite literal in the init clause of a `switch`/`if`/`for` is a Go grammar
ambiguity: the parser reads the `{` after `RevlOk[int64, any]` as the *switch
body*, then hits the struct field `Value:` where it expected `}` — hence
`expected '}', found Value`. (Go requires such literals to be parenthesized.)
It is also a second, latent bug: `.(type)` is only valid on an interface, and a
bare `RevlOk[...]{}` composite is a concrete struct.

Matching on a **variable** (`let r = Ok(1); match r { ... }`) works, because the
scrutinee is then an identifier, not a composite literal. `match None { ... }`
also works. It is specifically `Ok(_)` / `Err(_)` / `Some(_)` literal scrutinees
that break.

- Trigger: `match <Ok|Err|Some literal> { ... }` (Opt/Result constructor literal).
- Backend file: `backends/go/emit.py`, the v3 match lowering at the
  `switch _m := {scrutinee}.(type) {` line (~L3408). The scrutinee should be
  bound to an interface-typed temp (or at minimum parenthesized) before the
  type switch.
