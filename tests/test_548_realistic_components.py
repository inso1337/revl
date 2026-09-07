"""#548 — component-author gaps: eight realistic components compile first-draft.

The private full review (REVL-REVIEW-2026-09-06, summarised in #548) wrote eight
realistic components against the current surface and *none compiled on the first
draft* — every failure was a language gap, not an author mistake. The issue
ranked the walls: no `if`/`while`/`for` in provide-method bodies, expr-bodied
top-level fns absent, match-arm bindings a `${...}` template could not see,
variant payloads, `Float` one-way (no `to_str`), the stdlib string kit, Opt
methods, integer division/rendering.

This file pins the eight as regression-guarded compile tests. Each is a
component an author would plausibly write, and each exercises walls that are now
closed:

* **if / else-if chains and `while`/`for` loops in a provide method** — #548's
  top two ranked walls, landed by the control-flow slice (#681). c1/c2/c4/c6/c7.
* **`match` on a user variant with a payload binding, referenced from a
  `${...}` template in the arm** — review item 8's component-path twin. The
  fn-body fix (#570) walked template parts in scope; the provide-method path
  deferred template lowering to the env-only lowerer and lost the arm binding,
  so ``Ident(w) => `word:${w}` `` refused with "`w` is not a declared
  requirement". Fixed in this slice (lower.py, the component `Interp` case). c5.
* **`Float.to_str()`** — review item 12, "Float is one-way": a Float could enter
  arithmetic but never render back. Added in this slice as the Float row of the
  `to_str` builtin, emitting the same canonical ECMAScript Number::toString
  every tier already produces for a `${aFloat}` interpolation. c2.
* **expr-bodied top-level fns** (#633), the **stdlib string kit** via `use`
  (c3), **Opt.unwrap_or** off a `Str.to_int()` parse (c8), and integer
  `div_trunc`/`mod`/`to_str` (c6).

Per-tier remainders (pre-existing, documented, NOT #548 language gaps): the wasm
tier does not lower a `Float` value (c2), a method-body `for (x of xs)` (the
tracked #681 remainder — count with a `while`+index, c4/c7), or an Opt method
(c8). Those four assert wasm refuses; the other four emit on all six tiers.
"""

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT / "src", ROOT / "tests", ROOT / "backends" / "python", ROOT / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from _backend_import import backend_emitter  # noqa: E402
from revl import RevlError, compile_files, compile_source  # noqa: E402

ALL_TIERS = ["python", "typescript", "go", "java", "rust", "wasm"]


# --------------------------------------------------------------------------- #
# The eight realistic components.
# --------------------------------------------------------------------------- #

# 1 — a grader: an `if` / else-if chain and an early guard `return` in a provide
#     method (control flow, #681). Compiles on every tier.
GRADER = """
service Grader { fn grade(score: Int) -> Str }
component GradeBook provides grader: Grader {
  provide grader {
    fn grade(score) {
      if (score < 0) { return `invalid` }
      var letter = `F`
      if (score >= 90) { letter = `A` }
      else if (score >= 80) { letter = `B` }
      else if (score >= 70) { letter = `C` }
      else if (score >= 60) { letter = `D` }
      return letter
    }
  }
}
"""

# 2 — descriptive statistics over a `List[Float]`: two `for` loops that compute a
#     mean and variance, then render the Floats with `.to_str()` (review item 12)
#     inside a template. wasm does not carry Float values (tier remainder).
STATS = """
service Stats { fn summary(xs: List[Float]) -> Str }
component StatsBox provides stats: Stats {
  provide stats {
    fn summary(xs) {
      let n = xs.length()
      if (n == 0) { return `n=0` }
      var total = 0.0
      for (x of xs) { total = total + x }
      let mean = total / n
      var acc = 0.0
      for (x of xs) {
        let d = x - mean
        acc = acc + d * d
      }
      let variance = acc / n
      return `n=${n} mean=${mean.to_str()} var=${variance.to_str()}`
    }
  }
}
"""

# 3 — a text normalizer: trims via the stdlib string kit (`use`) and lowercases
#     ASCII with a small pure helper (a `while` loop over code points). Compiles
#     on every tier — the kit is pure revl. Compiled from files so the `use`
#     resolves against a real stdlib.
NORMALIZER = """
use "stdlib/str.rvl" { trim, str_char }
fn lower_ascii(s: Str) -> Str {
  var out = ""
  var i = 0
  let n = s.length()
  while (i < n) {
    let code = s.charCodeAt(i)
    if (code >= 65 && code <= 90) { out = out.concat(str_char(code + 32)) }
    else { out = out.concat(s.slice(i, i + 1)) }
    i = i + 1
  }
  return out
}
service Normalizer { fn normalize(raw: Str) -> Str }
component Norm provides normalizer: Normalizer {
  provide normalizer {
    fn normalize(raw) {
      let t = trim(raw)
      return lower_ascii(t)
    }
  }
}
"""

# 4 — a CSV field splitter: a `for (x of xs)` loop over `Str.split` accumulating
#     into a `List[Str]`. wasm's method-body `for` is the tracked #681 remainder.
CSV = """
service Csv { fn fields(line: Str) -> List[Str] }
component CsvReader provides csv: Csv {
  provide csv {
    fn fields(line) {
      var out: List[Str] = []
      for (part of line.split(`,`)) { out = out.push(part) }
      return out
    }
  }
}
"""

# 5 — a token describer: `match` on a user variant, each arm binding the payload
#     and reading it back through a `${...}` template (review item 8, the
#     component-path twin fixed by this slice). Compiles on every tier.
LEXER = """
type Token = Ident(Str) | Digits(Str) | Sym(Str)
service Lexer { fn describe(t: Token) -> Str }
component Describe provides lexer: Lexer {
  provide lexer {
    fn describe(t) {
      return match t {
        Ident(w) => `word:${w}`,
        Digits(nu) => `number:${nu}`,
        Sym(sy) => `symbol:${sy}`
      }
    }
  }
}
"""

# 6 — a price formatter: integer `div_trunc`/`mod`/`to_str` in an expr-shaped
#     pure helper (#633), padded and assembled in a template, with a sign guard
#     in the provide method (control flow, #681). Compiles on every tier.
MONEY = """
fn format_cents(cents: Int) -> Str {
  let dollars = cents.div_trunc(100)
  let rem = cents.mod(100)
  var frac = rem.to_str()
  if (rem < 10) { frac = `0`.concat(frac) }
  return `$${dollars.to_str()}.${frac}`
}
service Money { fn format(cents: Int) -> Str }
component Price provides money: Money {
  provide money {
    fn format(cents) {
      if (cents < 0) { return `-`.concat(format_cents(0 - cents)) }
      return format_cents(cents)
    }
  }
}
"""

# 7 — a list summarizer: a `for` loop with an inner `if` selecting a running max
#     (control flow, #681). wasm's method-body `for` is the tracked remainder.
SUMMARIZER = """
service Nums { fn positive_max(xs: List[Int]) -> Int }
component Summ provides nums: Nums {
  provide nums {
    fn positive_max(xs) {
      var best = 0
      for (x of xs) { if (x > best) { best = x } }
      return best
    }
  }
}
"""

# 8 — a config reader: a fallible `Str.to_int()` parse whose `Opt[Int]` is
#     resolved with `.unwrap_or(default)`. wasm does not lower an Opt method.
CONFIG = """
service Config { fn port_or_default(raw: Str) -> Int }
component Cfg provides cfg: Config {
  provide cfg {
    fn port_or_default(raw) {
      let parsed = raw.to_int()
      return parsed.unwrap_or(8080)
    }
  }
}
"""

# (name, source, tiers that must EMIT). Every component must compile on the
# frontend; the tiers a component does not list are pre-existing wasm remainders
# asserted separately below.
FIVE_TIERS = ["python", "typescript", "go", "java", "rust"]
IN_MEMORY = [
    ("grader", GRADER, ALL_TIERS),
    ("stats", STATS, FIVE_TIERS),        # wasm: no Float value
    ("csv", CSV, FIVE_TIERS),            # wasm: method-body `for` (#681 remainder)
    ("lexer", LEXER, ALL_TIERS),
    ("money", MONEY, ALL_TIERS),
    ("summarizer", SUMMARIZER, FIVE_TIERS),  # wasm: method-body `for`
    ("config", CONFIG, FIVE_TIERS),      # wasm: no Opt method
]


def _compile_with_stdlib(source: str, tmp_path: Path):
    """Compile a source that `use`s the stdlib string kit, resolving the import
    against a real copy of `stdlib/str.rvl` beside the main file."""
    (tmp_path / "stdlib").mkdir(exist_ok=True)
    shutil.copy(ROOT / "stdlib" / "str.rvl", tmp_path / "stdlib" / "str.rvl")
    main = tmp_path / "main.rvl"
    main.write_text(source, encoding="utf-8")
    return compile_files([str(main)])


# --------------------------------------------------------------------------- #
# The frontend: all eight compile (the #548 wall the review measured).
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name,source,_tiers", IN_MEMORY, ids=[c[0] for c in IN_MEMORY])
def test_component_compiles_on_frontend(name, source, _tiers):
    ir = compile_source(source)
    assert ir["components"]  # a realistic component compiles first-draft


def test_normalizer_compiles_on_frontend(tmp_path):
    ir = _compile_with_stdlib(NORMALIZER, tmp_path)
    assert ir["components"]


def test_all_eight_components_present():
    # the review measured "0/8 realistic components"; this file pins eight.
    assert len(IN_MEMORY) + 1 == 8


# --------------------------------------------------------------------------- #
# The tiers: each component emits on every tier its features lower on.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name,source,tiers", IN_MEMORY, ids=[c[0] for c in IN_MEMORY])
def test_component_emits_on_supported_tiers(name, source, tiers):
    ir = compile_source(source)
    for tier in tiers:
        backend_emitter(tier).emit(ir)  # raises on failure


def test_normalizer_emits_on_all_tiers(tmp_path):
    ir = _compile_with_stdlib(NORMALIZER, tmp_path)
    for tier in ALL_TIERS:
        backend_emitter(tier).emit(ir)


# --------------------------------------------------------------------------- #
# The documented wasm remainders (pre-existing tier limits, not #548 gaps).
# Pinned so a future wasm capability change updates this expectation on purpose.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name,source,reason", [
    ("stats", STATS, "type 'Float' is not lowerable"),
    ("csv", CSV, "a `for (x of xs)` loop in a provide-method body is not yet lowerable on the wasm tier"),
    ("summarizer", SUMMARIZER, "a `for (x of xs)` loop in a provide-method body is not yet lowerable on the wasm tier"),
    ("config", CONFIG, "scalar values have no methods"),
])
def test_wasm_remainder_is_a_clear_refusal(name, source, reason):
    ir = compile_source(source)
    with pytest.raises(Exception) as excinfo:
        backend_emitter("wasm").emit(ir)
    assert reason in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Focused pins on the two language fixes this slice lands.
# --------------------------------------------------------------------------- #

def test_template_sees_match_arm_binding_in_provide_method():
    # the component-path twin of #570: before this slice `${w}` in a match arm
    # of a PROVIDE METHOD misresolved `w` as a component requirement.
    ir = compile_source(LEXER)
    assert ir["components"]
    # the same shape refuses cleanly for an unbound name (not a crash)
    with pytest.raises(RevlError):
        compile_source(LEXER.replace("`word:${w}`", "`word:${nope}`"))


def test_float_to_str_is_admitted_and_int_to_str_still_is():
    assert "functions" in compile_source(
        "fn f(x: Float) -> Str { return x.to_str() }")
    assert "functions" in compile_source(
        "fn f(n: Int) -> Str { return n.to_str() }")
    # a Str receiver is still refused, now with the multi-family message
    with pytest.raises(RevlError) as excinfo:
        compile_source("fn f(s: Str) -> Str { return s.to_str() }")
    assert "has no form for a `Str` receiver" in str(excinfo.value)


def test_float_to_str_renders_through_the_canonical_ftoa():
    # the Float row emits each tier's canonical ECMAScript Number::toString, the
    # same helper a `${aFloat}` interpolation uses — never the Int spelling.
    ir = compile_source(STATS)
    py = backend_emitter("python").emit(ir)
    assert "_revl_ftoa(mean)" in py
    java = backend_emitter("java").emit(ir)
    assert "revlFtoa" in java and "private static String revlFtoa" in java
    rust = backend_emitter("rust").emit(ir)
    assert "revl_ftoa" in rust and "fn revl_ftoa(x: f64)" in rust
    go = backend_emitter("go").emit(ir)
    assert "strconv.FormatFloat(mean, 'g', -1, 64)" in go
