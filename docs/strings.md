# Strings — what a `Str`'s unit is, and how a `Float` renders in a template

This is the decision half of roadmap item 51 (**the string wave — "Str gets
the treatment Int got"**). The measurement half lives in
`tests/test_cross_tier_execution.py` under *THE STRING WAVE*; the numbers
below come from running those probes on every tier this environment could
execute (py, ts, go, rust, wasm; java read from its emitter — no JDK here).

The problem is the arithmetic problem, one level up. `Int / Int` diverged
because the IR carried no operand type, so every backend inherited its host's
division. A `Str`'s **unit** diverges for the same reason: the stdlib spec
(`docs/stdlib-2.0.md`) names `length`, `charAt`, `charCodeAt`, `slice` but
never says what they *count*, so every backend inherits its host's string
model. Every test passed because every test was ASCII, where code points,
UTF-16 units and UTF-8 bytes are the same number. They are not the same
number for `"😀"` (U+1F600): **1** code point, **2** UTF-16 units (D83D DE00),
**4** UTF-8 bytes (F0 9F 98 80). One emoji separates all three.

## The measured divergence

`"😀"` is the separating input. Values are what each tier actually computed;
`rej` and `refused` are real failures, recorded honestly, not omissions.

### The unit

| probe | py | ts | go | rust | java† | wasm |
|-------|----|----|----|------|------|------|
| `"😀".length()` | **1** | **2** | `rej`* | `rej`* | **2** | **4** |
| `"😀".charCodeAt(0)` | **128512** | **55357** | `rej`* | `rej`* | **55357** | **240** |
| `"😀".charAt(0) == "😀"` | `true` | `false` | `rej`* | `rej`* | `false` | `false` |
| `"a😀b".slice(1,2) == "😀"` | `true` | `false` | `rej`* | `rej`* | `false` | `false` |

- **py / go / rust** measure (or would measure) in **code points**:
  `len`/`ord`/`str[i]`, `utf8.RuneCountInString`/`[]rune`,
  `chars().count()`/`chars().nth()`. Code points is their native unit.
- **ts / java** measure in **UTF-16 code units**: `.length`, `charCodeAt`,
  `charAt`, `slice`/`substring` are all UTF-16. `charCodeAt(0)` returns
  `55357` — a lone high surrogate, not a character.
- **wasm** measures in **UTF-8 bytes**: `length` reads the byte-count prefix,
  `$str_char_code_at` does `i32.load8_u` (returns `240`, the first byte).

`*` **go and rust reject the astral literal before any method runs.** The IR
stores the string literal itself as **UTF-16** — `{"kind":"lit","value":"😀"}`
is carried as the surrogate pair `😀` — and the go/rust emitters
spell that as lone-surrogate `\uXXXX` escapes, which are not valid source in
those languages (go: *"escape sequence is invalid Unicode code point"*; rust
wants `\u{1F600}`, and in fact rejects **every** non-ASCII literal because it
emits `é`-style escapes rather than `\u{e9}`). Their length/slice helpers
are already code-point based — verified independently with a BMP literal,
where `"é".length()` **is** `1` on go — so the astral failure is a *literal
representation* defect, not a unit defect. Once the literal is emitted
correctly, go and rust answer in code points for free.

`†` java was not executed (no JDK in this environment); its column is read
from `backends/java/emit.py` (`String.length()` / `charAt` / `substring`, all
UTF-16).

### Float rendering inside a template (`${aFloat}`)

The same silent-divergence class in the interpolation path: a `Float` has no
canonical `Str` form, so `${x}` renders differently per tier.

| expression | py | ts | go | rust | java† | wasm |
|-----------|----|----|----|------|------|------|
| `${1.0e21}` | `1e+21` | `1e+21` | `1e+21` | `1000000000000000000000` | `1.0E21` | `refused` |
| `${0.0 / 0.0}` (NaN) | `nan` | `NaN` | `NaN` | `NaN` | `NaN` | `refused` |
| `${0.0}` (whole) | `0.0` | `0` | `0` | `0` | `0.0` | `refused` |
| `${(0.0-1.0)*0.0}` (−0.0) | `-0.0` | `0` | `0` | `-0` | `-0.0` | `refused` |

Renderers: py `str(float)`, ts `` `${x}` `` (Number→String), go
`fmt.Sprintf("%v", …)`, rust `format!("{}", …)`. wasm **refuses** a `Float`
literal in interpolation outright (*"literal 1e+21 is not lowerable on this
tier"*). No two of `nan`/`NaN`, `1e+21`/`1.0E21`/the full expansion, or
`0`/`0.0`/`-0`/`-0.0` agree across all tiers.

## The decision — the unit is **code points**

**A `Str` is a sequence of Unicode scalar values (code points). `length`,
`charAt`, `charCodeAt`, `slice`, `indexOf` and `split` all index and count in
code points, on every tier.** `charCodeAt` returns the scalar value
(`"😀".charCodeAt(0)` is `128512`), never a surrogate or a byte.

### Why code points, and not the JS-prior UTF-16

`syntax-2.0 §0` — *"syntax revl shares with TypeScript means what TypeScript
means; where the meaning must differ, the syntax must differ"* — is the reason
`/` is true division and the natural argument *for* UTF-16 (JS's unit). It
does not win here, for three reasons that the arithmetic wave's own logic
supplies:

1. **The discipline already exists, and it is code points.** `split` is
   already lowered by hand on every tier, and every tier splits into *code
   points*: python `list(s)`, go `[]rune(s)`, rust `s.chars()`. Choosing
   UTF-16 for `length` would make `split(s, "").length()` disagree with
   `s.length()` on the astral input — an *internal* contradiction, worse than
   a cross-tier one. Code points is the unit the surface already half-commits
   to.

2. **§0's escape clause is satisfied.** §0 permits a different meaning when the
   syntax differs, and the revl surface is `s.length()` — a method, on a
   `Str` that on four of six tiers is not a UTF-16 string at all — not JS's
   `s.length` property on a UTF-16 `string`. The unit that is coherent across
   *revl's* six hosts, not JavaScript's one, is code points.

3. **It is where the tiers already are, so it is the cheapest true answer.**
   Three tiers (py, go, rust) are already code points; UTF-16 would make all
   three wrong and cost work on the reference python tier. Just as rust was
   "the tier out of step with the syntax" for `/`, ts and java are the tiers
   out of step for the unit.

Byte-oriented work keeps its own type: `Bytes` is a sequence of `u8` and its
`length`/index are byte-based, unchanged. The unit decision is about `Str`.

### The float-rendering sub-decision — **ECMAScript `Number::toString`**

Here §0 *does* win, cleanly: `${x}` shares its template syntax with JS, and
the JS form is cheap for every tier to produce. **Canonical `Float → Str` is
the ECMAScript `Number::toString` shortest-round-trip form:** `1e+21`,
`"NaN"`, `"Infinity"` / `"-Infinity"`, a whole-number float as `0` (no
trailing `.0`), and **negative zero as `"0"`**. This is the same "spelled as
TS spells it, means what TS means" tiebreak that decided `/`. One wart, stated
so the next wave does not rediscover it: `Number::toString(-0)` drops the sign
(`"0"`), so the string does not distinguish `-0.0` from `0.0` even though the
values remain distinct under `==`/IEEE; a stricter `-0` renderer was
considered and rejected for divergence from the JS prior. This same canonical
renderer is what `Float.to_str()` should call when it lands (it does not exist
yet; `Int.to_str()` is the precedent).

## What the fix wave must do, per tier

Scoped from the measurements above, so the next wave is enumerated, not
discovered. **No emitter is touched by this probe wave; this is the work it
hands off.**

**Cross-cutting (frontend / IR — `src/revl`, out of scope for the probe):**

- **Store string literals as code points, not UTF-16.** Today the IR value is
  a surrogate-paired Python `str` (`😀`). The canonical IR form for
  a `Str` literal must be the sequence of Unicode scalars, so a code-point
  unit and a correct per-backend escaper are even possible. This single change
  is what unblocks go and rust.
- Provide the **one canonical `Float → Str`** renderer (ECMAScript form) that
  every backend's interpolation and `Float.to_str()` route through — the exact
  `Int`-wave move (`docs/arithmetic.md`), a shared spec each tier spells in
  host syntax.

**Per tier (`backends/*/emit.py`, the fix wave's files):**

| tier | unit today | unit work | literal / float work |
|------|-----------|-----------|----------------------|
| **python** | code points | none — reference | float: render via the canonical form, not `str()` (drop `nan`→`NaN`, `-0.0`/`0.0`→`0`, keep `1e+21`) |
| **go** | code points* | none (helpers already `[]rune`) | **literal**: emit valid UTF-8 / `\u{}`-free source so astral literals compile; float: `fmt.Sprintf` via the canonical form (fix `-0`→`0` is already close, but drive it through the shared renderer) |
| **rust** | code points* | none (helpers already `chars()`) | **literal**: emit `\u{XXXX}` (rust syntax), unblocking *all* non-ASCII literals; float: `format!` → canonical (`1000…`→`1e+21`, `-0`→`0`) |
| **typescript** | UTF-16 | **rewrite** `length`/`charAt`/`charCodeAt`/`slice` to code-point-aware helpers: `[...s].length`, `codePointAt`, spread/`Array.from` indexing, code-point `slice`. `charCodeAt` must return the scalar, not a surrogate | literal already valid; float: `${x}` is already the canonical form for `1e+21`/`NaN`/`0`, only `-0`→`0` already holds — TS is nearly free on floats |
| **java** | UTF-16 | **rewrite** through `codePointCount` / `offsetByCodePoints` / `codePointAt`; `charAt`→`new String(Character.toChars(cp))`; `slice`→code-point offsets | float: `String.valueOf` gives `1.0E21`/`-0.0`/`0.0` — needs the canonical renderer (biggest float gap) |
| **wasm** | UTF-8 bytes | **rewrite** `$str_length` / `$str_char_at` / `$str_char_code_at` / `$str_slice` to decode UTF-8 and count/index code points (walk continuation bytes), not raw byte offsets | float: currently **refuses** `Float` in interpolation entirely — needs a Float→Str routine (canonical form) before templates with floats lower at all |

**Rough cost:** python ≈ free; go / rust ≈ literal-escaper fix only (methods
already correct); typescript ≈ moderate (helper rewrite, float nearly done);
java ≈ moderate (helper rewrite + float renderer); wasm ≈ largest (UTF-8
decoding in wat helpers + a Float renderer that does not exist yet).

## The probes

`tests/test_cross_tier_execution.py` carries the probes under *THE STRING
WAVE*. They assert the **chosen** answers (code points; the ECMAScript float
form), so they are `xfail(strict=False)` today — the tiers already in the
chosen unit **xpass**, the tiers that owe work **xfail** — and they flip to
plain `pass` (remove the marker) as each tier is fixed. They are the
acceptance test for the fix wave. They do **not** modify any emitter; the
divergence is documented and measured here, not yet closed.
