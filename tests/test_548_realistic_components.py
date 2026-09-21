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
tier does not lower a `Float` value (c2) or an Opt method (c8). Each remainder
asserts a NAMED refusal rather than being skipped. Item 458 landed the
method-body `for (x of xs)` walk, so c4/c7 (the old #681 remainder) emit and run
on wasm too.

**Why the tier cases assert what came back, not that emit returned.** For months
`lexer`, `money` and `normalizer` passed a case named "emits on all tiers" while
go answered them with a package that had no component in it at all: go routed a
document carrying a top-level `fn` to its pure typed-core path, which renders
the declarations and DROPS every component it routes past, and raised nothing
(issue #721). Three of these eight were in that state and the gate could not
see it, because calling `emit(ir)` and discarding the result only ever tests
that the emitter did not raise. `assert_tier_carried_the_components` closes
that: every tier that returns must have the component's name and each of its
provide-method names somewhere in what it returned. Issue #1321 made go carry
those documents on the combined renderer, so all eight now emit on all six
tiers, but the gate is what makes that claim checkable rather than assumed.
"""

import copy
import json
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


def _emitted_text(tier, ir):
    """What a tier answered with, as text. wasm answers with a dict of
    sections rather than one source string; every check below is a substring
    search, so render it once here instead of special-casing each call."""
    out = backend_emitter(tier).emit(copy.deepcopy(ir))
    return out if isinstance(out, str) else json.dumps(out, default=str)


def _provide_method_names(comp):
    for step in comp.get("body") or []:
        for method in step.get("methods") or []:
            yield method["name"]


def _spellings(name):
    """The name as written, PascalCase and camelCase. Tiers case-mangle a
    method name (`positive_max` -> `PositiveMax` on go and java); the component
    name itself is carried verbatim by all six, but accept the same set for it
    so a future renaming convention does not read as a drop."""
    parts = name.split("_")
    return {name,
            "".join(part.capitalize() for part in parts),
            parts[0] + "".join(part.capitalize() for part in parts[1:])}


def assert_tier_carried_the_components(tier, ir, text):
    """A tier's `emit` RETURNING is not evidence that it rendered what it was
    given (issue #721 / #1321).

    go dropped every component of a document that also declared a top-level
    `fn` and raised nothing, so three of the eight below passed a case named
    "emits on all tiers" while go answered with a package that had no
    component, no service and no route in it. Nothing catches an emitter that
    renders less than it was given and says nothing: a wrong lowering is
    caught by a byte oracle and a refusal by its message, but silence is caught
    only by looking at what came back.

    So look: every declared component's name, and every provide method it
    declares, has to appear somewhere in the answer.
    """
    for comp in ir["components"]:
        assert any(spelling in text for spelling in _spellings(comp["name"])), (
            "%s returned %d bytes with no sign of component %r. A tier that "
            "cannot carry a component must refuse by name, not answer with a "
            "module the component is missing from"
            % (tier, len(text), comp["name"]))
        for method in _provide_method_names(comp):
            assert any(spelling in text for spelling in _spellings(method)), (
                "%s rendered component %r but not its provide method %r"
                % (tier, comp["name"], method))


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
#     into a `List[Str]`. Emits on all six tiers (item 458 landed wasm `for`).
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
#     (control flow, #681). Emits on all six tiers (item 458 landed wasm `for`).
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
    ("csv", CSV, ALL_TIERS),             # wasm method-body `for` landed (item 458)
    ("lexer", LEXER, ALL_TIERS),    # go carries it since issue #1321
    ("money", MONEY, ALL_TIERS),    # go carries it since issue #1321
    ("summarizer", SUMMARIZER, ALL_TIERS),  # wasm method-body `for` landed (item 458)
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
        # raises on failure, and `assert_tier_carried_the_components` is what
        # makes NOT raising mean something (see its docstring)
        assert_tier_carried_the_components(tier, ir, _emitted_text(tier, ir))


def test_normalizer_emits_on_all_tiers(tmp_path):
    # `use`d stdlib functions are top-level `fn`s in the compiled document, so
    # the normalizer is the shape go used to route to the pure typed-core path
    # and answer with a component-free package (#721). go carries it now
    # (#1321), and this case checks the component is in the answer rather than
    # that the call returned. That is the assertion that was missing while three of
    # these eight passed it over a go package with no component in it.
    ir = _compile_with_stdlib(NORMALIZER, tmp_path)
    assert ir["components"], "the case proves nothing without a component"
    for tier in ALL_TIERS:
        assert_tier_carried_the_components(tier, ir, _emitted_text(tier, ir))


# --------------------------------------------------------------------------- #
# The documented wasm remainders (pre-existing tier limits, not #548 gaps).
# Pinned so a future wasm capability change updates this expectation on purpose.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name,source,reason", [
    ("stats", STATS, "type 'Float' is not lowerable"),
    ("config", CONFIG, "scalar values have no methods"),
])
def test_wasm_remainder_is_a_clear_refusal(name, source, reason):
    ir = compile_source(source)
    with pytest.raises(Exception) as excinfo:
        backend_emitter("wasm").emit(ir)
    assert reason in str(excinfo.value)


@pytest.mark.parametrize("name,source,component,helper", [
    ("lexer", LEXER, "Describe", None),
    ("money", MONEY, "Price", "format_cents"),
])
def test_go_carries_the_component_beside_the_top_level_declaration(
        name, source, component, helper):
    # issue #721 / #1321: these two are "realistic" in exactly the way the
    # review meant, a helper `fn` or a `type` beside the component that uses
    # it, and that is the shape go's pure typed-core routing used to drop.
    # It answered `money` with 6185 bytes of stdlib preamble and two free
    # functions: no services, no component, no routes, no error.
    #
    # go carries the document on the combined renderer now, so BOTH halves are
    # in the answer: the top-level declaration the pure path was there for, and
    # the component the pure path was dropping.
    ir = compile_source(source)
    out = backend_emitter("go").emit(ir)
    assert_tier_carried_the_components("go", ir, out)
    assert "stc.Component" in out, "the component is live, not a bare struct"
    assert f'Name: "{component}"' in out
    if helper:
        assert f"func {helper}(" in out, (
            "the top-level declaration the pure path was chosen for is still "
            "rendered; carrying the component must not cost it")


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
