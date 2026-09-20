"""The self-hosted expression type-checker slice (selfhost/checker.rvl),
compiled by revl, emitted through the python backend, executed, and
cross-checked against the reference checker (src/revl/typecheck.py's
`infer_ast`) on both *verdict* and *inferred type*.

This is the second half of a differential oracle, in the exact shape of
tests/test_selfhost_parser.py: two independent implementations of one
operator-typing algebra are forced to agree on every input, so a
disagreement is always a real defect in one of them.

Agreement is checked three ways:
  * accepted input -> identical inferred-type strings ("?" stands for the
    reference's None, the gradual frontier's unknown);
  * refused input  -> both refuse (revl's pure stratum has no exceptions,
    so the reference *raising* RevlError and the selfhost checker returning
    "refuse" is the agreement; the messages are not compared);
  * a fuzz corpus of random binop expressions over the same fixed
    environment, where the two checkers are each other's oracle including
    on the inputs the generator makes ill-typed by accident.

Slice: literal typing, + - * / % and the comparison families, over the
five-binding environment ENV below (mirrored inside checker.rvl's
base_env). Everything else the grammar allows is out of slice on both
sides of the corpus.

Slice three (bottom of this file) extends the differential to the module
table: type declarations parse, aliases resolve, record/variant specs and
the ADT case table build, and the ExprVar/ExprField/ExprCall arms of
`infer_ast` agree on verdict AND inferred type against the reference's
`_resolve_type_aliases` / `_lower_type_decls` / `_case_table` machinery.
"""

import importlib.util
import random
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
import revl.parser as refparser  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.typecheck import CASES_KEY, check_ast, infer_ast  # noqa: E402


# ---------------------------------------------------------------- harness

def _exec_emitted() -> dict:
    ir = compile_files([str(ROOT / "selfhost" / "checker.rvl")])
    assert ir["ir_version"] == 3
    spec = importlib.util.spec_from_file_location(
        "pyemit_selfhost_checker", ROOT / "backends" / "python" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stub = types.ModuleType("runtime")
    stub.__getattr__ = lambda name: (lambda *a, **k: None)  # PEP 562
    had = "runtime" in sys.modules
    previous = sys.modules.get("runtime")
    sys.modules["runtime"] = stub
    try:
        namespace = {}
        exec(compile(module.emit(ir), "selfhost_checker.py", "exec"), namespace)
    finally:
        if had:
            sys.modules["runtime"] = previous
        else:
            del sys.modules["runtime"]
    return namespace


@pytest.fixture(scope="module")
def ns():
    return _exec_emitted()


@pytest.fixture(scope="module")
def infer_src(ns):
    return ns["infer_expr_str"]


@pytest.fixture(scope="module")
def check_src(ns):
    """The slice-two entry point: "" if the selfhost checker accepts a
    program, else its refusal spelled as the reference spells it."""
    return ns["check_service_src"]


# ------------------------------------------------- reference inferencer

# Mirrored by base_env() in selfhost/checker.rvl. Keep the two in lockstep.
# `m` is Int32 — the one operand type the bitwise operators accept (item 366);
# without it the positive bitwise path (`m & m` -> Int32) could not be exercised.
# `opt`/`xs` are slice T2a's: the optional-escape refusals (a field read and an
# index THROUGH an `Opt`) and the `??` rule need an optional in scope, and the
# index rules need a `List`. The fuzz generators do not draw either name.
ENV = {"x": "Int", "y": "Int", "f": "Float", "s": "Str", "flag": "Bool",
       "m": "Int32", "opt": "Opt[Str]", "xs": "List[Int]"}


def _ref_parse(src: str):
    parser = refparser.Parser(src, "diff.rvl")
    node = parser.pure_expr()
    if not parser.at("eof"):
        raise RevlError("diff.rvl", 1, "trailing tokens")
    return node


def _ref_infer(src: str) -> str:
    """The reference verdict+type, rendered in the selfhost checker's
    vocabulary: "refuse" where infer_ast raises, "?" where it returns
    None, else the type's spelling."""
    try:
        node = _ref_parse(src)
        t = infer_ast(node, dict(ENV), {}, filename="diff.rvl")
    except RevlError:
        return "refuse"
    return t if t else "?"


def _agree(infer_src, src: str) -> None:
    want = _ref_infer(src)
    got = infer_src(src)
    assert got == want, f"{src!r}: selfhost {got!r} != reference {want!r}"


# ---------------------------------------------------------------- corpus

ACCEPTED = [
    # literals and variables
    "1", "0", "true", "false", "x", "y", "f", "s", "flag",
    # float literals (parser slice gained them; they infer Float, like the
    # reference's lit handling)
    "2.5", "0.5", "2.5 + 1", "1 + 2.5", "f / 2.5", "2.5 < f",
    "x / 0.5", "(2.5 + 0.5) * x",
    "q",  # not in the environment: the gradual frontier's unknown
    # arithmetic
    "1 + 2", "x - y", "x * 2", "7 / 2", "x % 3",
    "f + 1", "1 + f", "f - f", "f * f", "f / f", "f % 2", "x / f", "x % f",
    "(1 + 2) * x", "x - y - z", "7 / 2 / 2",
    # string concatenation: two Strs concatenate; a Str beside an UNKNOWN
    # operand (`q`) stays on the gradual frontier (result unknown). A Str beside
    # a KNOWN numeric operand is refused (issue #549) — see REJECTED below.
    "s + s", "s + q",
    # ordering (Str orders too)
    "x < y", "x <= f", "f > 1", "f >= x", "s < s", "s <= q",
    # equality
    "x == y", "x != y", "s == s", "flag == false", "flag != true",
    "q == x", "q != q", "x === y", "x !== y",  # === canonicalizes to ==
    # nesting across the families
    "1 + 2 < 4", "x < y == true", "(x == y) == (flag == false)",
    "1 < 2 == s < s",
    # Int32 bitwise operators (item 366): Int32-only, result Int32. An unknown
    # operand (`q`) stays on the gradual frontier — the reference skips it — so
    # `m & q` still types Int32; unary `~` mirrors the binary Int32-only rule.
    "m & m", "m | m", "m ^ m", "m << m", "m >> m", "~m", "~ ~m",
    "m & m | m", "m << m >> m", "(m & m) | (m ^ m)", "~m & m", "m & ~m",
    "m & q", "q & m", "q & q", "~q",
    "m == m", "m != m",  # equality over Int32 (compatible), not ordering
    # ---- slice T2a ----------------------------------------------------------
    # Int32 IS numeric (typecheck._NUMERIC), so two Int32 operands add, and the
    # WIDTH MIX below — not the operand family — is what refuses `m + x`.
    "m + m", "m - m", "m * m", "m / m", "-m", "m + m * m",
    # index: a List indexes to its element; the target and the index are each
    # walked first
    "xs[0]", "xs[x]", "xs[q]", "q[0]", "q[s]", "xs.length", "xs[0] + 1",
    # ternary: the branches join, and a disagreement is named
    "flag ? 1 : 2", "flag ? x : f", "flag ? q : 1", "q ? 1 : 2", "flag ? s : s",
    # lists: the elements join, and an element whose type is unknown leaves the
    # accumulator unset rather than poisoning it
    "[1, 2]", "[]", "[s, s]", "[q, y]", "[1, 2.5]", "[[1], [2]]", "[xs]",
    # records: an anonymous literal infers a sorted structural shape, and a
    # repeated field name keeps the LAST value under one key
    '{ a: 1, b: s }', '{ b: s, a: 1 }', '{ a: s, a: 1 }', '{ a: q }',
    '{ a: 1 }.a', '{ }',
    # record update on a structural base (item 71)
    '{ { h: s } | h = s }', '{ { h: s, a: 1 } | a = 2 }', '{ q | h = 1 }',
    # `&& || ??` joined the binop rules with `_binop_type`
    "flag && flag", "flag || flag", "flag && q", "opt ?? s", "opt ?? q",
    "q ?? s",
    # the optional's own members are not reachable, but `.length` on the Str
    # inside is reachable through the `??` fallback
    '(opt ?? "") .length',
    # Float literals inside binary64
    "1e308", "1.7976931348623157e308", "0.0", "1e-400",
]

REJECTED = [
    # Bool in arithmetic (refusal-parity: `/` on Bool is the headline case)
    "flag / flag", "flag + 1", "true * false", "x + flag", "1 % flag",
    "flag / x", "x - true",
    # Str in arithmetic (only `+` takes a Str, and only beside another Str)
    "s * s", "s - s", "1 % s", "s / s", "s * 2", "2 / s",
    # Str `+` beside a KNOWN numeric operand: refused on every tier (issue
    # #549 — `Str + Int`/`Str + Float` scattered, now a compile error). A Str
    # beside an UNKNOWN operand stays accepted (see ACCEPTED `s + q`).
    "s + 1", "1 + s", "s + f", "s + 2.5",
    # Bool / Str under ordering
    "flag < 1", "x < true", "flag <= flag", "true > false",
    # equality across incompatible types
    "x == s", "s == x", "flag == x", "1 == flag", "s != 2",
    # null has no type (absence is Opt[T])
    "null", "null + 1", "1 + null", "null == null",
    # Int32 bitwise operators are Int32-only (item 366): Int (incl. literals),
    # Float, Str and Bool operands are all refused, and so is any mix with a
    # non-Int32 side (the shift count must be Int32 too). Unary `~` mirrors it.
    "x & y", "x & m", "m & x", "1 & 2", "m & 1", "m | 2.5",
    "f & f", "s & s", "flag & flag", "x | y", "x ^ y",
    "m << x", "x << m", "m >> y", "x << 1",
    "~x", "~f", "~s", "~flag", "~1", "~y",
    # ---- slice T2a ----------------------------------------------------------
    # the Int32 WIDTH MIX and the Int-only `%` (docs/arithmetic.md)
    "m + x", "x + m", "m - y", "m * x", "m % m", "x % m", "m % x",
    # `Str` is not indexable, and an index must be an Int
    "s[0]", "s[x]", "xs[s]", "xs[flag]", "xs[2.5]",
    # a field read and an index THROUGH an optional (the opt escape)
    "opt.length", "opt[0]", "opt.anything",
    # disagreeing ternary branches
    "flag ? 1 : s", "flag ? s : flag", "flag ? xs : s",
    # an unknown field of an anonymous record literal (item 71)
    '{ a: 1 }.b', '{ a: 1 }.length', '{ }.a',
    # record update: an unknown field, a wrong replacement type, a non-record
    '{ { h: s } | missing = s }', '{ { h: s } | h = 5 }', '{ x | h = 5 }',
    '{ s | h = 5 }', '{ xs | h = 5 }',
    # `&&`/`||` want Bool and `??` wants an optional on the left
    "x && flag", "flag || s", "x ?? 1", "s ?? s", "xs ?? xs",
    # a Float literal outside binary64 folds to IEEE infinity (issue #312)
    "1e999", "2e400", "1.5e310", "1e999 + 1.0", "123456789e400",
]


@pytest.mark.parametrize("src", ACCEPTED)
def test_accepted_expressions_agree(infer_src, src):
    assert _ref_infer(src) != "refuse", f"corpus bug: reference refuses {src!r}"
    assert _ref_infer(src) != "(bad)", f"corpus bug: reference rejects {src!r}"
    _agree(infer_src, src)


@pytest.mark.parametrize("src", REJECTED)
def test_rejected_expressions_agree(infer_src, src):
    assert _ref_infer(src) == "refuse", f"corpus bug: reference accepts {src!r}"
    assert infer_src(src) == "refuse", f"selfhost accepted {src!r}"


@pytest.mark.parametrize("binding", ["let", "var"])
@pytest.mark.parametrize("annotation", ["", ": Int"])
def test_local_binding_type_reaches_service_call(check_src, binding, annotation):
    source = f'''service Db {{ fn query(sql: Str) -> Int }}
service Api {{ fn run() -> Int }}
component C requires db: Db provides api: Api {{
  provide api {{
    fn run() {{
      {binding} value{annotation} = 1
      return db.query(value)
    }}
  }}
}}'''
    expected = "`db.query` argument `sql` expects `Str`, got `Int`"
    assert _ref_check(source) == expected
    assert check_src(source) == expected


# ---------------------------------------------------------------- fuzz

ATOMS = ["1", "0", "7", "2.5", "x", "y", "f", "s", "flag", "q",
         "true", "false"]
BINOPS = ["+", "-", "*", "/", "%", "<", "<=", ">", ">=", "==", "!="]


def _gen(rng: random.Random, depth: int) -> str:
    if depth <= 0:
        return rng.choice(ATOMS)
    roll = rng.random()

    def g():
        return _gen(rng, depth - 1)

    if roll < 0.75:
        return f"{g()} {rng.choice(BINOPS)} {g()}"
    return f"({g()})"


@pytest.mark.parametrize("seed", range(12))
def test_generated_expressions_agree(infer_src, seed):
    """Random binop expressions over the whole slice. Nothing here is a
    fixed oracle — the two checkers are each other's oracle, including on
    the inputs the generator makes ill-typed by accident (the Str-beside-
    arithmetic and Bool-operand mixes especially), where agreeing to
    *refuse* is the property under test."""
    rng = random.Random(seed)
    for _ in range(60):
        _agree(infer_src, _gen(rng, rng.randint(1, 4)))


# Bitwise-only generator (item 366): the operators are all Int32-only, so a
# subtree that mixes `m` (Int32) with any of `x/y/f/s/flag/1` (non-Int32) must
# be refused by BOTH checkers, while an all-Int32 (or unknown-`q`) subtree types
# Int32. Deliberately kept to the bitwise operators + unary `~`: the checker
# slice models Int32 for bitwise typing only, not for Int32 arithmetic/ordering
# (a separate slice), so mixing in `+`/`<` here would compare a rule this port
# does not claim to mirror.
BIT_ATOMS = ["m", "x", "y", "f", "s", "flag", "q", "1", "0", "true"]
BIT_BINOPS = ["&", "|", "^", "<<", ">>"]


def _gen_bit(rng: random.Random, depth: int) -> str:
    if depth <= 0:
        return rng.choice(BIT_ATOMS)
    roll = rng.random()

    def g():
        return _gen_bit(rng, depth - 1)

    if roll < 0.60:
        return f"{g()} {rng.choice(BIT_BINOPS)} {g()}"
    if roll < 0.75:
        return f"~{g()}"
    return f"({g()})"


@pytest.mark.parametrize("seed", range(12))
def test_generated_bitwise_expressions_agree(infer_src, seed):
    """Random bitwise expressions over the Int32-only operator family. The two
    checkers are each other's oracle: an Int32/unknown subtree types Int32,
    any non-Int32 operand refuses, and both must agree on verdict AND type."""
    rng = random.Random(1000 + seed)
    for _ in range(60):
        _agree(infer_src, _gen_bit(rng, rng.randint(1, 4)))



# ================================================================ slice two
#
# The service-boundary checker (selfhost/checker.rvl's second half) against
# the reference compiler's provision checks. The reference is ground truth on
# BOTH the verdict and the diagnostic text: a refusal must carry the exact
# message lower.py would raise, not merely the same verdict.

from revl import compile_source  # noqa: E402


def _ref_check(src: str) -> str:
    """"" if the reference compiler accepts, else its diagnostic message."""
    try:
        compile_source(src, "diff.rvl")
        return ""
    except RevlError as e:
        return e.message


# ------------------------------------------------------------- corpus

# ------------------------------------------- the top-level declaration heads
#
# Every one of these is a shape the reference's `_parse_program` accepts and
# `p_top` had no case for, so the checker answered `(bad) unexpected token at
# top level` / `(bad) unexpected declaration` and the WHOLE document failed at
# the parse stage — never reaching the service boundaries, the checkable core
# of G4 or the call-site argument types this slice decides. Measured over the
# census corpus: 75 documents the reference ADMITS were refused this way, 44 of
# them by the `pub` prefix alone.
#
# The fixtures come in pairs. The ACCEPTED half puts the head before (or
# around) a clean component; the REJECTED half puts the SAME head in front of a
# component with a real call-site type error, so a step that overshoots the
# declaration turns a refusal into silence and is caught. `_TH_*` are the two
# shared tails.
_TH_HEAD = """service Database { fn query(sql: Str) -> Int }
"""

_TH_TAIL = """component Auditor requires db: Database {
  let rows = effect db.query("select 1") undo db.query("cleanup")
}
"""

_TH_BAD_TAIL = """component Auditor requires db: Database {
  let rows = effect db.query(1) undo db.query("cleanup")
}
"""

_TH_BAD_MSG = "`db.query` argument `sql` expects `Str`, got `Int`"


# ------------------------------- the DECLARATION SIGNATURES (item 391)
#
# The heads above are the ones `p_top` could not enter. These are the ones it
# entered and then could not READ: an extern classification it had no case for,
# a type-parameter list, a trailing `cache` clause, a record field named after a
# grammar noun, and the service-operation modifier slot. Each failed the WHOLE
# document at the parse stage, which reads as agreement. Measured over the
# census corpus: 53 documents the reference ADMITS were refused this way — 29
# at `expected fn after extern`, 16 at `bad method signature in service <S>`,
# 6 at `expected { after fn signature` and 2 at `bad record field in type <T>`.
#
# Paired the same way: the ACCEPTED half puts the declaration in front of a
# clean component, the REJECTED half in front of one carrying a real refusal,
# so a step that overshoots turns a refusal into silence and is caught.
_DS_WITNESSED = '''type Stash = { path: Str, bak: Str }
type FsError = { code: Str }
extern pure fn unstash(w: Stash) -> Unit = @py { return None }
extern witnessed[fs] fn stash(path: Str) -> Result[Stash, FsError] undo idempotent unstash(result) = @py { return 1 }
'''

_DS_ASYNC_EXTERN = (
    'extern emission async fn http_post(url: Str, body: Str) -> Str'
    ' = @py { return "" }\n')

# the rest of the modifier slot, in one declaration each: item 309's
# `idempotent`, item 245's `deferred`, item 257's `validated retry N`, item
# 388's caller-decided `fn|async`, and item 373's `(confined: p)` reach clause
_DS_MODIFIERS = '''extern emission idempotent fn ping(m: Str) -> Int = @py { return 1 }
extern emission deferred fn mail(m: Str) -> Unit = @py { return None }
extern emission validated retry 3 fn charge(m: Str) -> Int = @py { return 1 }
extern emission fn|async engine_run(m: Str) -> Int = @py { return 1 }
extern emission (confined: path) fn write_at(path: Str, body: Str) -> Int = @py { return 1 }
'''

_DS_GENERIC = "fn pair_up[T, U](a: T, b: U) -> T { return a }\n"
_DS_CACHE = "fn twice(n: Int) -> Int cache pure { return n + n }\n"
_DS_RECORD = '''type Envelope = {
  config: Int, component: Int, requires: Int, realm: Int, intercept: Int,
  isolate: Int, in: Int, with: Int, plain: Int,
}
'''


ACCEPTED_PROGRAMS = [
    # `pub` is a visibility PREFIX, not a declaration head — the single largest
    # parse gap on this surface, and the one that made every `stdlib/*.rvl` and
    # every `pub fn`-bearing selfhost source unreachable to this checker.
    ("pub fn before a component",
     'pub fn normalize(k: Str) -> Str { return k }\n' + _TH_HEAD + _TH_TAIL),
    ("pub type / pub service / pub extern prefixes", '''pub type Key = { id: Str }
pub service Database { fn query(sql: Str) -> Int }
pub extern pure fn norm(k: Str) -> Str = @py { return k }
''' + _TH_TAIL),
    # a `test` block was skipped by LINE, so the body's statements were walked
    # as top-level declarations
    ("a named test block before a component", _TH_HEAD + '''test "arith" {
  let a = 1
  assert a + 1 == 2
}
''' + _TH_TAIL),
    # the three contextual qualifiers on `test`
    ("a lifecycle test before a component", _TH_HEAD + '''lifecycle test "round trip" {
  load Auditor
  unload Auditor
  assert no_residue
}
''' + _TH_TAIL),
    ("a prop test before a component", _TH_HEAD + '''prop test "commutes" (a: Int, b: Int) {
  assert a + b == b + a
}
''' + _TH_TAIL),
    ("a fault test after the component it names",
     _TH_HEAD + _TH_TAIL + '''fault test "reverts" for Auditor {
  fail at step 1
  assert no residue
}
service Later { fn ping() -> Int }
component Pinger requires later: Later {
  let r = effect later.ping() undo later.ping()
}
'''),
    # the two contextual heads `selfhost/lower.rvl` already knew and this
    # surface did not
    ("an event declaration before a component",
     'event Tick(key: at) { at: Int }\n' + _TH_HEAD + _TH_TAIL),
    ("a boot component", '''service Env { fn a() -> Str }
boot component B provides e: Env {
  config { x: Str }
  provide e { fn a() = config.x }
}
''' + _TH_HEAD + _TH_TAIL),
    # ---- the provide-method return annotation (item 391, issue #1065) ------
    # A provide method may RESTATE the return type its service already
    # declares. The reference parses it (`parser.py`'s `pmethod`, where the
    # `-> T` sits between the parameter list and the body) and leaves any
    # mismatch to the type layer. `p_prov_methods` here read the `{`/`=`
    # straight off the end of the parameter list, so an annotated method failed
    # the WHOLE component with `(bad) bad provide block in component <C>`.
    # Its `lower.rvl` twin was the same defect and PR #1063 closed it.
    #
    # All four spellings are in one component on purpose. The two annotated
    # ones are what fails against the unported checker; the two bare ones are
    # the negative controls, because the step over the annotation must be
    # CONDITIONAL — a step taken unconditionally eats the `{` or the `=` of an
    # unannotated method and reproduces the same failure from the other side.
    # Brace body and `=` shorthand are two distinct parser arms, so each form
    # carries both controls.
    ("provide method restates its return type", """
service Counter {
  fn size() -> Int
  fn bump(n: Int) -> Int
  fn label(n: Int) -> Str
  fn flag() -> Bool
}
component Tally provides counter: Counter {
  provide counter {
    fn size() -> Int { return 0 }
    fn bump(n) { return n + 1 }
    fn label(n) -> Str = n.to_str()
    fn flag() = true
  }
}
"""),
    # ---- the declaration signatures (item 391) ---------------------------
    # Five extern heads `p_extern` had no case for. `emission[caps]` /
    # `witnessed[caps]` (item 343) and `async` (docs/design/async-extern.md §1)
    # are the three the tree actually spells, 29 documents between them; the
    # rest of the modifier slot is here because the reference consumes it in a
    # LOOP, so any subset in any order has to parse.
    ("a scoped emission extern", """
extern emission[net.edge] fn ship(body: Str) -> Int = @py { return 1 }
service Sink { emission[net.edge] fn send(m: Str) }
component S provides sink: Sink {
  provide sink { fn send(m) { emit ship(m) } }
}
"""),
    ("a witnessed extern", _DS_WITNESSED + _TH_HEAD + _TH_TAIL),
    ("an async extern and the async provide method implementing it",
     _DS_ASYNC_EXTERN + """service Http { emission async fn post(url: Str, body: Str) -> Str }
component Poster provides http: Http {
  provide http { async fn post(url, body) = http_post(url, body) }
}
"""),
    ("the rest of the extern modifier slot",
     _DS_MODIFIERS + _TH_HEAD + _TH_TAIL),
    # a type-parameter list between the name and the parameters: `params_at`
    # walked into `[T, U]` and found no `{` where the body should be
    ("a generic fn signature", _DS_GENERIC + _TH_HEAD + _TH_TAIL),
    # the item-310 trailing clause, the only spelling in the tree and the only
    # one the reference admits on a plain `fn`
    ("a cache pure trailing clause", _DS_CACHE + _TH_HEAD + _TH_TAIL),
    # `_record_key_name`: eight cordis-domain nouns are legal FIELD names even
    # though they are genuine keywords where they can lead a form
    ("record fields named after the grammar nouns",
     _DS_RECORD + _TH_HEAD + _TH_TAIL),
    # the whole service-operation modifier slot in one declaration, each
    # operation implemented so the component is complete
    ("the service-operation modifier slot", """
service Api {
  route get "/notes" fn list_notes() -> Str
  emission idempotent fn put(k: Str)
  commutative fn add(k: Str)
  emission endorse[fs] fn run(k: Str)
  emission validated retry 3 fn charge(k: Str) -> Int
  fn look(k: Str) -> Int cache pure
}
extern emission fn w(k: Str) -> Int = @py { return 1 }
component ApiHost provides api: Api {
  provide api {
    fn list_notes() = "x"
    fn put(k) { emit w(k) }
    fn add(k) { let x = 1 }
    fn run(k) { emit w(k) }
    fn charge(k) { emit w(k) return 1 }
    fn look(k) = 1
  }
}
"""),
    # ---- A6, the negative controls ---------------------------------------
    # The signature-agreement block whose refusals are in REJECTED_PROGRAMS
    # below must stay SILENT on a provider that AGREES with its declaration:
    # annotations that match, on both parameters and return, with matching
    # async colours. A one-directional compatibility test, or an off-by-one in
    # the positional pairing, reds here.
    ("annotations that agree with the declaration", """
service Database {
  async fn query(sql: Str, n: Int) -> Int
  fn plain(k: Str) -> Str
}
component C provides db: Database {
  provide db {
    async fn query(sql: Str, n: Int) -> Int = n
    fn plain(k: Str) -> Str { return k }
  }
}
"""),
    ("a restated annotation that is the declared type exactly", """
service Database { fn query(n: Float) -> Float }
component C provides db: Database {
  provide db { fn query(n: Float) -> Float = n }
}
"""),
    # a provider may be purer than its declaration: no emission, no refusal
    ("honest provider", """
service Cache { fn put(key: Str, value: Str) }
component HonestCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
    }
  }
}
"""),
    # a declared `emission` covers a marked emission in the body
    ("declared emission covers the body", """
service Database { emission fn execute(sql: Str) -> Int }
service Cache { emission fn put(key: Str, value: Str) }
component C requires db: Database provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
      emit db.execute(`INSERT INTO log VALUES (${key})`)
    }
  }
}
"""),
    # `emission[db]` bounds *where*: emitting through db and only db is inside
    ("scoped declaration honored", """
service Database { emission fn execute(sql: Str) -> Int }
service Cache { emission[db] fn put(key: Str, value: Str) }
component C requires db: Database provides cache: Cache {
  provide cache {
    fn put(key, value) {
      emit db.execute(key)
    }
  }
}
"""),
    # a plain fn helper that reaches no emission keeps the provider clean
    ("pure helper chain", """
fn normalize(k: Str) -> Str { return k }
service Cache { fn put(key: Str, value: Str) }
component C provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn put(key, value) {
      let k = normalize(key)
      effect store.insert(k, value)
      undo   store.remove(k)
    }
  }
}
"""),
    # call-site argument typing: the declared type is satisfied
    ("argument type ok", """
service Database { fn query(sql: Str) -> Int }
component Probe requires db: Database {
  let rows = effect db.query("select 1") undo db.query("cleanup")
}
"""),
    # Int widens to Float at the call site, as everywhere else
    ("argument widening", """
service Stat { fn sample(t: Float) -> Int }
component P requires s: Stat {
  let r = effect s.sample(3) undo s.sample(4)
}
"""),
]

# (name, source, expected message). The message pins are the reference's own
# text — several are the documented `expected error` of a checked-in fixture.
EXAMPLES = ROOT / "examples" / "rejections"


def _fixture(name: str) -> str:
    return (EXAMPLES / name).read_text()


REJECTED_PROGRAMS = [
    # ---- the top-level heads, negative controls (item 391) -----------------
    # The same head in front of a component the checker must still REFUSE.
    # Every one of these drew a parse `(bad)` before, which a verdict-direction
    # comparison reads as agreement while nothing was checked at all.
    ("a bad call-site type after a pub fn",
     'pub fn normalize(k: Str) -> Str { return k }\n' + _TH_HEAD + _TH_BAD_TAIL,
     _TH_BAD_MSG),
    ("a bad call-site type after a named test block", _TH_HEAD + '''test "arith" {
  let a = 1
  assert a + 1 == 2
}
''' + _TH_BAD_TAIL, _TH_BAD_MSG),
    ("a bad call-site type after a lifecycle test",
     _TH_HEAD + '''lifecycle test "round trip" {
  load Auditor
  unload Auditor
  assert no_residue
}
''' + _TH_BAD_TAIL, _TH_BAD_MSG),
    ("a bad call-site type after a prop test",
     _TH_HEAD + '''prop test "commutes" (a: Int, b: Int) {
  assert a + b == b + a
}
''' + _TH_BAD_TAIL, _TH_BAD_MSG),
    ("a bad call-site type after a fault test",
     _TH_HEAD + _TH_TAIL + '''fault test "reverts" for Auditor {
  fail at step 1
  assert no residue
}
service Later { fn ping(n: Str) -> Int }
component Pinger requires later: Later {
  let r = effect later.ping(1) undo later.ping("x")
}
''', "`later.ping` argument `n` expects `Str`, got `Int`"),
    ("a bad call-site type after an event declaration",
     'event Tick(key: at) { at: Int }\n' + _TH_HEAD + _TH_BAD_TAIL, _TH_BAD_MSG),
    ("a bad call-site type after a boot component", '''service Env { fn a() -> Str }
boot component B provides e: Env {
  config { x: Str }
  provide e { fn a() = config.x }
}
''' + _TH_HEAD + _TH_BAD_TAIL, _TH_BAD_MSG),
    # ---- the declaration signatures, negative controls (item 391) --------
    # The same declaration in front of a component the checker must still
    # REFUSE. A step that runs past the declaration and eats the component
    # after it turns this refusal into silence.
    ("a bad call-site type after a scoped emission extern",
     "extern emission[net.edge] fn ship(body: Str) -> Int = @py { return 1 }\n"
     + _TH_HEAD + _TH_BAD_TAIL, _TH_BAD_MSG),
    ("a bad call-site type after a witnessed extern",
     _DS_WITNESSED + _TH_HEAD + _TH_BAD_TAIL, _TH_BAD_MSG),
    ("a bad call-site type after an async extern",
     _DS_ASYNC_EXTERN + _TH_HEAD + _TH_BAD_TAIL, _TH_BAD_MSG),
    ("a bad call-site type after the extern modifier slot",
     _DS_MODIFIERS + _TH_HEAD + _TH_BAD_TAIL, _TH_BAD_MSG),
    ("a bad call-site type after a generic fn",
     _DS_GENERIC + _TH_HEAD + _TH_BAD_TAIL, _TH_BAD_MSG),
    ("a bad call-site type after a cache pure fn",
     _DS_CACHE + _TH_HEAD + _TH_BAD_TAIL, _TH_BAD_MSG),
    ("a bad call-site type after a keyword-named record field",
     _DS_RECORD + _TH_HEAD + _TH_BAD_TAIL, _TH_BAD_MSG),
    # The capability a `witnessed[fs]` extern SEEDS is its declared scope, not
    # its own name (`_emitting_capabilities`, emission_analysis.py). Reading it
    # as the name would answer "emits through `stash`" here, which is not a
    # capability any service bound can mention, so this pins the seeding rule
    # rather than the parse alone.
    ("a witnessed extern reaching outside its declared scope",
     _DS_WITNESSED + """service Keep { emission[net] fn hold(p: Str) }
component K provides keep: Keep {
  provide keep { fn hold(p) { effect stash(p) } }
}
""",
     "`Keep.hold` is declared `emission[net]`, but this implementation emits "
     "through `fs` (reaching `stash()`)"),
    # An `async` provide method's BODY has to be reached and checked, not just
    # stepped over: these two put the slice's own two verdict families inside
    # one.
    ("an async provide method reaching an undeclared emission", """
service Store { async fn get(k: Str) -> Int }
extern emission fn w(k: Str) -> Int = @py { return 1 }
fn through(k: Str) -> Int { return w(k) }
component C provides store: Store {
  provide store { async fn get(k) { let n = through(k) return n } }
}
""",
     "`Store.get` is declared plain, but this implementation reaches "
     "`through()`"),
    ("an async provide method mistyping a call-site argument", """
service Store { async fn get(k: Str) -> Int }
service Database { fn query(sql: Str) -> Int }
component C requires db: Database provides store: Store {
  provide store { async fn get(k) -> Int = db.query(42) }
}
""", "`db.query` argument `sql` expects `Str`, got `Int`"),
    # ---- A6: the provider against its declaration (item 391) -------------
    # The six rules of `_lower_provide`, in the reference's own order. None was
    # decided here. The two checked-in fixtures come first: `t7` is the
    # PARAMETER twin of the return annotation #1063 taught this parser to read
    # and nothing then compared, and `v2_async_signature_mismatch` is the
    # document the service-operation modifier slot above unmasked — it spent
    # its life behind `(bad) bad method signature in service Database`.
    ("t7 the checked-in parameter-annotation fixture",
     _fixture("t7_provide_param_annotation_mismatch.rvl"),
     "parameter `sql` of `query` (from service `Db`) expects `Str`, got `Int`"),
    ("v2 the checked-in async-agreement fixture",
     _fixture("v2_async_signature_mismatch.rvl"),
     "method `stats` of provision `db` is not async but service Database "
     "declares it async"),
    ("a6 the provider is not async and the declaration is", """
service Database { async fn stats() -> Int }
component C provides db: Database {
  provide db { fn stats() = 1 }
}
""",
     "method `stats` of provision `db` is not async but service Database "
     "declares it async"),
    ("a6 the arities disagree", """
service Database { fn query(sql: Str) -> Int }
component C provides db: Database {
  provide db { fn query(sql, extra) = 1 }
}
""",
     "method `query` of provision `db` takes 2 params but service Database "
     "declares 1"),
    ("a6 the provider implements a method the service never declared", """
service Database { fn query(sql: Str) -> Int }
component C provides db: Database {
  provide db { fn upsert(sql) = 1 }
}
""", "`upsert` is not a method of service Database"),
    ("a6 one method provided twice", """
service Database { fn query(sql: Str) -> Int }
component C provides db: Database {
  provide db {
    fn query(sql) = 1
    fn query(sql) = 2
  }
}
""", "duplicate method `query` in provision `db`"),
    ("a6 the restated return type disagrees", """
service Database { fn query(sql: Str) -> Int }
component C provides db: Database {
  provide db { fn query(sql) -> Str = "x" }
}
""",
     "return type of `query` (from service `Database`) expects `Int`, got "
     "`Str`"),
    # A widening annotation is still not the published signature: `Float`
    # accepts an `Int`, so a ONE-directional compatibility test would admit
    # this. The reference tests both directions and so does the port.
    ("a6 a restated parameter annotation that merely widens", """
service Database { fn query(sql: Int) -> Int }
component C provides db: Database {
  provide db { fn query(sql: Float) = 1 }
}
""",
     "parameter `sql` of `query` (from service `Database`) expects `Int`, got "
     "`Float`"),
    ("g4 emission not declared",
     _fixture("g4_emission_not_declared.rvl"),
     "`Cache.put` is declared plain, but this implementation reaches `db.execute`"),
    ("g4 capability not declared",
     _fixture("g4_capability_not_declared.rvl"),
     "`Cache.put` is declared `emission[db]`, but this implementation emits "
     "through `bus` (reaching `db.execute`, `bus.publish`)"),
    ("g4 unmarked emission",
     _fixture("g4_unmarked_emission.rvl"),
     "call to emission `db.execute` must be marked `emit` (G4)"),
    ("t1 service argument type",
     _fixture("t1_service_arg_type.rvl"),
     "`db.query` argument `sql` expects `Str`, got `Int`"),
    ("g4 multi-hop named-call chain", '''extern emission fn audit_write(msg: Str) -> Int = @py { return 1 }
fn audit_log(msg: Str) -> Int {
  return audit_write(msg)
}
fn write_through(key: Str) -> Int {
  return audit_log(key)
}
service Cache {
  fn put(key: Str, value: Str)
}
component LyingCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache {
    fn put(key, value) {
      effect store.insert(key, value)
      undo   store.remove(key)
      let n = write_through(key)
    }
  }
}
''',
     "`Cache.put` is declared plain, but this implementation reaches `write_through()`"),
    ("service method arity", '''
service Database { fn query(sql: Str) -> Int }
component Probe requires db: Database {
  let rows = effect db.query("a", "b") undo db.query("x")
}
''',
     "`db.query` takes 1 argument(s), 2 given"),
    ("unknown service method", '''
service Database { fn query(sql: Str) -> Int }
component Probe requires db: Database {
  let rows = effect db.upsert("x") undo db.query("y")
}
''',
     "`db.upsert` is not a method of service Database"),
    # item 423, both halves. Until it landed, the corpus spelled only
    # WELL-FORMED bare calls in a component body, so the oracle stayed green
    # while BOTH sides admitted these (item 429: the oracle cannot demand a fix
    # for a case the corpus never spells). These two spell it.
    ("component-body extern call arity", '''
type SecretHandle = Opaque
extern acquire fn secret_put(v: Str) -> SecretHandle undo secret_release(result) = @py { return 1 }
extern pure fn secret_release(h: SecretHandle) = @py { return }
component C {
  let s = effect secret_put("v", "w") undo secret_release(s)
}
''',
     "`secret_put` takes 1 argument(s), 2 given"),
    # ---- the verdicts the provide-block parse refusal was MASKING ---------
    # The parse refusal above was not only a false rejection. On a program the
    # reference REFUSES it stood in for the real verdict: the checker answered
    # `(bad) bad provide block in component C` from a stage that never reached
    # the guarantee, and a reader comparing verdict directions would have
    # called that agreement. These three are the same three families the
    # checker's slice already decides (G4 upper bound, unmarked emission, and a
    # required-service argument type), each written with the annotation that
    # used to stop them at the parse — one in the brace-body arm and two in the
    # `=` shorthand arm, so neither arm can regress silently.
    ("annotated provide method reaches an undeclared emission", """
service Database { emission fn execute(sql: Str) -> Int }
service Cache { fn put(key: Str, value: Str) }
component C requires db: Database provides cache: Cache {
  provide cache {
    fn put(key, value) -> Int {
      emit db.execute(key)
      return 1
    }
  }
}
""",
     "`Cache.put` is declared plain, but this implementation reaches "
     "`db.execute`"),
    ("annotated shorthand provide method emits unmarked", """
service Database { emission fn execute(sql: Str) -> Int }
service Cache { fn put(key: Str, value: Str) }
component C requires db: Database provides cache: Cache {
  provide cache {
    fn put(key, value) -> Int = db.execute(key)
  }
}
""",
     "call to emission `db.execute` must be marked `emit` (G4)"),
    ("annotated shorthand provide method mistypes a call-site argument", """
service Database { fn query(sql: Str) -> Int }
service Cache { fn put(key: Str) -> Int }
component C requires db: Database provides cache: Cache {
  provide cache {
    fn put(key) -> Int = db.query(42)
  }
}
""",
     "`db.query` argument `sql` expects `Str`, got `Int`"),
    ("component-body extern call argument type", '''
type SecretHandle = Opaque
extern acquire fn secret_put(v: Str) -> SecretHandle undo secret_release(result) = @py { return 1 }
extern pure fn secret_release(h: SecretHandle) = @py { return }
component C {
  let s = effect secret_put(42) undo secret_release(s)
}
''',
     "argument 1 of `secret_put(...)` expects `Str`, got `Int`"),
    # ---- the reach order inside one statement (issue #1261) ---------------
    # `_method_emissions` notes a statement's evidence at the STATEMENT node:
    # the step's own crossing, then every emitting NAME the whole statement
    # calls, SORTED (`for name in sorted(calls & env.emitting_fns)`), then the
    # names passed as values. `check_stmts` accumulated those names in SOURCE
    # order, so this slice refused the same program as the reference with the
    # same tag and the same offending tokens in a different reach order:
    #   reference:   … (reaching `audit_log()`, `pg_write()`)
    #   this slice:  … (reaching `pg_write()`, `audit_log()`)
    # Both refuse; the divergence was on the message alone, and no document in
    # this corpus reached two emitting names from one statement, so nothing
    # measured it.
    #
    # `pg_write` ahead of `audit_log` in the source on purpose: the two orders
    # are each other's reverse, so a document written the other way round would
    # agree whether or not anything sorted.
    ("two host externs in one statement sort by name", """
extern emission fn pg_write(row: Str) -> Int = @py { return 0 }
extern emission fn audit_log(row: Str) -> Int = @py { return 0 }
service Ledger { emission[db] fn post(row: Str) -> Int }
component Bookkeeper provides ledger: Ledger {
  provide ledger {
    fn post(row) {
      let r = pg_write(row) + audit_log(row)
      return r
    }
  }
}
""",
     "`Ledger.post` is declared `emission[db]`, but this implementation emits "
     "through `audit_log`, `pg_write` (reaching `audit_log()`, `pg_write()`)"),
    # The plain-declaration half, rendered by the other `g4_verdict` arm.
    ("two host externs in one statement sort by name, plain half", """
extern emission fn pg_write(row: Str) -> Int = @py { return 0 }
extern emission fn audit_log(row: Str) -> Int = @py { return 0 }
service Ledger { fn post(row: Str) -> Int }
component Bookkeeper provides ledger: Ledger {
  provide ledger {
    fn post(row) {
      let r = pg_write(row) + audit_log(row)
      return r
    }
  }
}
""",
     "`Ledger.post` is declared plain, but this implementation reaches "
     "`audit_log()`, `pg_write()`"),
    # One statement, two branches: the reference collects the subtree at the
    # statement node, so a conditional sorts the same way.
    ("a conditional reaching two host externs sorts them", """
extern emission fn pg_write(row: Str) -> Int = @py { return 0 }
extern emission fn audit_log(row: Str) -> Int = @py { return 0 }
service Ledger { emission[db] fn post(row: Str) -> Int }
component Bookkeeper provides ledger: Ledger {
  provide ledger {
    fn post(row) {
      let r = row == "x" ? pg_write(row) : audit_log(row)
      return r
    }
  }
}
""",
     "`Ledger.post` is declared `emission[db]`, but this implementation emits "
     "through `audit_log`, `pg_write` (reaching `audit_log()`, `pg_write()`)"),
    # Control: the same two externs in TWO statements. The sort is per node, so
    # the reference keeps source order here, and a fix that sorted the whole
    # accumulated list would fail on this one.
    ("two host externs in two statements keep source order", """
extern emission fn pg_write(row: Str) -> Int = @py { return 0 }
extern emission fn audit_log(row: Str) -> Int = @py { return 0 }
service Ledger { emission[db] fn post(row: Str) -> Int }
component Bookkeeper provides ledger: Ledger {
  provide ledger {
    fn post(row) {
      let a = pg_write(row)
      let b = audit_log(row)
      return a + b
    }
  }
}
""",
     "`Ledger.post` is declared `emission[db]`, but this implementation emits "
     "through `audit_log`, `pg_write` (reaching `pg_write()`, `audit_log()`)"),
]


@pytest.mark.parametrize("name_src", ACCEPTED_PROGRAMS)
def test_accepted_programs_agree(check_src, name_src):
    name, src = name_src
    assert _ref_check(src) == "", f"corpus bug: reference refuses {name}"
    got = check_src(src)
    assert got == "", f"{name}: selfhost refused: {got!r}"


@pytest.mark.parametrize("case", REJECTED_PROGRAMS)
def test_rejected_programs_agree(check_src, case):
    name, src, expected = case
    got_ref = _ref_check(src)
    assert got_ref == expected, f"corpus bug: reference says {got_ref!r}"
    got = check_src(src)
    assert got == got_ref, f"{name}: selfhost {got!r} != reference {got_ref!r}"


# ---------------------------------------------------------------- fuzz

# Random argument lists at a required-service call site: the two checkers
# must agree on verdict AND message for every combination, including the
# ones the generator makes ill-typed on purpose.
# No `2.5` here: the selfhost *expression* layer cannot parse a float
# literal yet (slice-one grammar gap; see dogfood/findings-shadow2.md), so a
# float argument would be an unparseable statement on that side alone.
# Int still meets a Float parameter, so the widening path stays covered.
FUZZ_LITERALS = ["1", "7", '"x"', "true"]
FUZZ_PARAMS = ["Int", "Float", "Str", "Bool"]


def _fuzz_program(rng: random.Random) -> str:
    params = ", ".join(
        f"a{i}: {rng.choice(FUZZ_PARAMS)}" for i in range(rng.randint(1, 3)))
    args = ", ".join(rng.choice(FUZZ_LITERALS)
                     for _ in range(rng.randint(1, 3)))
    return (
        "service Fz { fn op(" + params + ") -> Int }\n"
        "component FzP requires r: Fz {\n"
        "  let v = effect r.op(" + args + ") undo r.op(" + args + ")\n"
        "}\n")


@pytest.mark.parametrize("seed", range(8))
def test_generated_call_sites_agree(check_src, seed):
    rng = random.Random(seed)
    for _ in range(25):
        src = _fuzz_program(rng)
        want = _ref_check(src)
        got = check_src(src)
        assert got == want, f"{src!r}: selfhost {got!r} != reference {want!r}"


# ================================================================ slice three
#
# The module-table expression checker (selfhost/checker.rvl's third half:
# `infer_prog_expr`) against the reference's type-table machinery. The
# selfhost parses the *program*'s type declarations, resolves transparent
# aliases, builds the record/variant table and the ADT case table, then
# infers one expression against base_env() — and the reference does the same
# through `_resolve_type_aliases` / `_validate_declared_types` /
# `_lower_type_decls` / `_case_table` and `infer_ast`. Both sides must agree
# on verdict AND inferred type, exactly as in slices one and two.
#
# In scope: nullary case constructors as values, payload checking at a case
# call, the Some/Ok/Err builtins (Opt/Result parametric results), ExprField
# against a named record (opt-escape refusal, `.length` on sized heads,
# unknown-field refusal), and table-build refusals (duplicate type/field/
# case, alias cycles, malformed generic spellings). Out of slice on both
# sides of the corpus: generics with parameters, match exhaustiveness,
# host-provenance receivers, `&&`/`||`/`??` (the reference types them, the
# selfhost binop surface does not), and fn-signature call checking.

from revl.lower import (  # noqa: E402
    _case_table,
    _lower_type_decls,
    _resolve_type_aliases,
    _validate_declared_types,
)


@pytest.fixture(scope="module")
def infer_prog(ns):
    """The slice-three entry point: infer one expression against a program's
    declarations. "(bad) ..." on a program parse error, "refuse" on a table
    or checker refusal, "?" where the reference returns None, else the type's
    spelling."""
    return ns["infer_prog_expr"]


def _ref_prog_infer(prog_src: str, expr_src: str) -> str:
    """The reference verdict+type for infer_prog_expr's surface, rendered in
    the selfhost vocabulary: "refuse" where the reference raises (program
    parse, alias/table build, or expression inference), "?" where infer_ast
    returns None, else the type's spelling."""
    try:
        program = refparser.Parser(prog_src, "diff.rvl").parse()
        _resolve_type_aliases(program, "diff.rvl")
        _validate_declared_types(program, "diff.rvl")
        types = _lower_type_decls(program, "diff.rvl")
        types[CASES_KEY] = _case_table(types)
    except RevlError:
        return "refuse"
    try:
        node = _ref_parse(expr_src)
        t = infer_ast(node, dict(ENV), types, filename="diff.rvl")
    except RevlError:
        return "refuse"
    return t if t else "?"


def _agree_prog(infer_prog, prog_src, expr_src) -> None:
    want = _ref_prog_infer(prog_src, expr_src)
    got = infer_prog(prog_src, expr_src)
    assert got == want, (f"{prog_src!r} / {expr_src!r}: "
                         f"selfhost {got!r} != reference {want!r}")


# ---------------------------------------------------------------- corpus

ACCEPTED_PROG_EXPR = [
    # nullary case constructors are values of their ADT
    ("type S = Idle | Busy", "Idle"),
    ("type S = Idle | Busy", "Busy"),
    ("type S = Idle | Busy", "Some(Idle)"),
    ("type S = Idle | Busy", "Idle.x"),
    ("type Status = Pending", "Pending"),
    # payload checking at a case call (widening, unknown, zero args)
    ("type Shape = Circle(Float) | Square", "Circle(1.0)"),
    ("type Shape = Circle(Float) | Square", "Circle(1)"),
    ("type Shape = Circle(Float) | Square", "Circle(q)"),
    ("type Shape = Circle(Float) | Square", "Circle()"),
    ("type Shape = Circle(Float) | Square", "Square"),
    ("type Box = Wrap(Any) | Empty", "Wrap(1)"),
    ("type Box = Wrap(Any) | Empty", "Empty"),
    ("type W = Wrap(Int)", "Wrap(x)"),
    ("type W = Wrap(Int)", "Wrap(q)"),
    ("type M = M(Opt[Int]) | N", "M(1)"),
    ("type M = M(Opt[Int]) | N", "M(Some(1))"),
    # Some/Ok/Err keep their builtin parametric results
    ("", "Some(1)"),
    ("", "None"),
    ("", "None(1)"),
    ("", "Some(None)"),
    ("", "Some(Some(1))"),
    ("type Outcome = Ok(Row) | Err(Str)\ntype Row = { name: Str }",
     "Err(\"m\")"),
    ("type Outcome = Ok(Row) | Err(Str)\ntype Row = { name: Str }", "Ok"),
    # records: `.length` on sized heads, unknown fields stay on the frontier
    ("type Row = { name: Str, age: Int }", "s.length"),
    ("type Row = { name: Str, age: Int }", "x.length"),
    ("type Row = { name: Str, age: Int }", "q.length"),
    ("type Row = { name: Str, age: Int }", "Ok(1).name"),
    # equality and ordering over the new heads
    ("type Row = { name: Str, age: Int }", "Some(1) == Some(1)"),
    ("type Row = { name: Str, age: Int }", "Some(1) == 1"),
    ("type Row = { name: Str, age: Int }", "1 == Some(1)"),
    ("type Row = { name: Str, age: Int }", "None == None"),
    ("type Row = { name: Str, age: Int }", "Ok(1) == Err(1)"),
    ("", "Some(1) == Some(2.5)"),
    ("", "Some(1) != Some(2.5)"),
    ("", "Ok(Some(1)) == Ok(Some(1))"),
    ("type S = A(Float) | B", "A(x) == A(x)"),
    ("type Box = Wrap(Any) | Empty", "Wrap(1) == Wrap(1)"),
    # aliases erase transparently
    ("type Sku = Str", "Sku"),
    ("type Rows = List[Row]\ntype Row = { name: Str }", "Rows"),
    ("type Rows = List[Row]\ntype Row = { name: Str }", "Row"),
    ("type MaybeRow = Row?\ntype Row = { name: Str }", "MaybeRow"),
    ("type A = Str type B = A", "B"),
    ("type A = B type B = Str", "A"),
    ("type Handler = (Int) -> Str", "1"),
    ("type R = { h: Handler }\ntype Handler = (Int) -> Str", "s"),
    # Some/None reserved for Opt: a user case reusing them is dropped
    ("type S = Some(Int) | Other", "Other"),
    ("type S = Some(Int) | Other", "Some(1)"),
    ("type S = Some(Int) | Other", "None"),
    # multi-case variants and non-name callees stay on the frontier
    ("type S = A | B | C", "B"),
    ("type S = A(Int) | B(Str)", "A(x)"),
    ("type S = A(Int) | B(Str)", "B(s)"),
    ("", "q(1)"),
    ("", "x(1)"),
    ("", "s(1)"),
    ("type S = A(Int) | B", "q(A(1))"),
    ("type List = { x: Int }", "List"),
]

REJECTED_PROG_EXPR = [
    # opt-escape: field access on an optional is refused
    ("", "Some(1).foo"),
    ("", "None.foo"),
    ("type S = Idle | Busy", "Some(Idle).foo"),
    # payload mismatch at a case call
    ("type Shape = Circle(Float) | Square", "Circle(\"a\")"),
    ("type W = Wrap(Int)", "Wrap(2.5)"),
    ("type Outcome = Ok(Row) | Err(Str)\ntype Row = { name: Str }",
     "Ok(x)"),
    ("type Outcome = Ok(Row) | Err(Str)\ntype Row = { name: Str }",
     "Err(1)"),
    ("type E = Evt(Row) | Stop\ntype Row = { id: Int }", "Evt(1)"),
    ("type M = M(Opt[Int]) | N", "M(\"a\")"),
    ("type T = A(Int32) | B", "A(1)"),
    ("type T = A(Int32) | B", "A(2.5)"),
    ("type L = L(List[Int]) | N", "L(1)"),
    # ordering / arithmetic on non-numeric heads
    ("", "Some(1) < Some(2)"),
    ("", "Some(1) + 1"),
    ("", "Some(1) == 2.5"),
    ("", "Some(1) < q"),
    ("", "Ok(1) < 1"),
    ("type S = Idle | Busy", "Idle < Busy"),
    ("type S = Idle | Busy", "Idle + 1"),
    ("type S = A(Int) | B", "A(1) < A(2)"),
    ("type S = A(Int) | B", "A(1) + A(2)"),
    # table build refusals
    ("type A = { x: Int } type A = { y: Int }", "1"),
    ("type S = A | A", "1"),
    ("type R = { a: Int, a: Str }", "1"),
    ("type A = B\ntype B = A", "1"),
    ("type A = A", "1"),
    ("type A = B\ntype B = C\ntype C = A", "1"),
    ("type Bad = Opt", "1"),
    ("type Bad = List", "1"),
    ("type Bad = Map[Str]", "1"),
    ("type Bad = Result", "1"),
    ("type R = { a: Opt }", "1"),
    ("type S = A(Opt) | B", "1"),
    # `Never` is the inferred bottom, uninhabited and ONE-WAY: it flows out of a
    # `Never` position into any other, and nothing flows in. So an `Int` payload
    # for a `Wrap(Never)` case is a definite argument mismatch, not a cast.
    ("type N = Wrap(Never) | X", "Wrap(1)"),
]


@pytest.mark.parametrize("prog_expr", ACCEPTED_PROG_EXPR)
def test_accepted_prog_expr_agree(infer_prog, prog_expr):
    prog, expr = prog_expr
    assert _ref_prog_infer(prog, expr) not in ("refuse", "(bad)"), \
        f"corpus bug: reference refuses {prog!r} / {expr!r}"
    _agree_prog(infer_prog, prog, expr)


@pytest.mark.parametrize("prog_expr", REJECTED_PROG_EXPR)
def test_rejected_prog_expr_agree(infer_prog, prog_expr):
    prog, expr = prog_expr
    assert _ref_prog_infer(prog, expr) == "refuse", \
        f"corpus bug: reference accepts {prog!r} / {expr!r}"
    assert infer_prog(prog, expr) == "refuse", \
        f"selfhost accepted {prog!r} / {expr!r}"


# ---------------------------------------------------------------- fuzz

# Programs whose type tables exercise every slice-three shape; the first is
# the empty table (builtin cases only).
FUZZ_PROGS = [
    "",
    "type S = Idle | Busy",
    "type Shape = Circle(Float) | Square",
    "type W = Wrap(Int) | Empty",
    "type Box = Wrap(Any) | Empty",
    "type M = M(Opt[Int]) | N",
    "type E = Evt(Row) | Stop\ntype Row = { id: Int }",
    "type Outcome = Ok(Row) | Err(Str)\ntype Row = { name: Str }",
    "type S = A(Int) | B(Str)",
    "type Rows = List[Row]\ntype Row = { name: Str }",
]

# Case-constructor names usable as nullary values or calls in each program.
FUZZ_CTORS = [
    [],
    ["Idle", "Busy"],
    ["Circle", "Square"],
    ["Wrap", "Empty"],
    ["Wrap", "Empty"],
    ["M", "N"],
    ["Evt", "Stop"],
    ["Ok", "Err"],
    ["A", "B"],
    [],
]

FUZZ_ATOMS3 = ["1", "0", "7", "2.5", "x", "y", "f", "s", "flag", "q",
               "true", "false"]


def _gen3(rng: random.Random, ctors: list, depth: int) -> str:
    if depth <= 0:
        return rng.choice(FUZZ_ATOMS3 + ctors)
    roll = rng.random()

    def g():
        return _gen3(rng, ctors, depth - 1)

    if roll < 0.20:
        return f"Some({g()})"
    if roll < 0.30:
        return rng.choice(["None", f"Ok({g()})", f"Err({g()})"])
    if roll < 0.45:
        return f"{rng.choice(ctors)}({g()})" if ctors else g()
    if roll < 0.55:
        return f"{g()}.length"
    if roll < 0.80:
        return f"{g()} {rng.choice(BINOPS)} {g()}"
    return f"({g()})"


@pytest.mark.parametrize("seed", range(10))
def test_generated_prog_exprs_agree(infer_prog, seed):
    """Random expressions over a fixed pool of type-table programs. Nothing
    here is a fixed oracle — the two checkers are each other's oracle,
    including on the payload-mismatch and opt-escape inputs the generator
    makes ill-typed by accident, where agreeing to *refuse* is the property
    under test."""
    rng = random.Random(seed)
    for _ in range(40):
        prog = rng.choice(FUZZ_PROGS)
        ctors = FUZZ_CTORS[FUZZ_PROGS.index(prog)]
        _agree_prog(infer_prog, prog, _gen3(rng, ctors, rng.randint(1, 3)))


# ================================================================= slice T2a
#
# THE MESSAGE, not just the verdict (docs/design/457 §2, slice T2a).
#
# Slices one to three compared a VERDICT: "refuse" against `RevlError`. The
# type layer's obligation is stronger, and it is what `crates/revl-gate`'s
# consumers act on: the self-host must spell the reference's diagnostic BYTE
# FOR BYTE, under the tag `tests/test_selfhost_lower.py::_classify` derives
# from it. The comparison unit is fixed at `"<TAG>|<message>"` — the same one
# the lowering oracle and `tools/gate_reference_census.py` use, and the
# classifier is IMPORTED rather than copied so the two cannot drift apart.
#
# In this slice: the operator rules (`_binop_type` in full, including the five
# dedicated T1 messages, `&& || ??` and the Int32 width/remainder rules), the
# field rules (the opt escape, `.length`, the erased `Any`/`Value` read, the
# unknown field of a structural or nominal record), index, ternary, lists,
# record literals and record updates, and the Float literal range. Calls and
# signatures (T2b), arrows (T2c) and match/optional-chaining (T2d) keep the
# verdict-only comparison above.
#
# WHAT THIS SLICE DOES NOT REACH, stated so the next one does not rediscover
# it: `selfhost/lower.rvl`'s `admit_src` runs no fn-body type walk, so none of
# these refusals reaches the gate yet and no census document changes bucket.
# The fixtures below are pinned at EXPRESSION level — the expression the
# reference refuses, in the environment its own signature declares — which is
# exactly the algebra T3a's statement layer consumes.

from test_selfhost_lower import _classify  # noqa: E402


@pytest.fixture(scope="module")
def verdict_env(ns):
    """`(prog, env, expr, expected, where) -> "" | "<TAG>|<message>"`.

    `env` is `name: Type` entries separated by `;` (a revl string literal has
    no `\n` escape, item 183); `expected` empty is the inference position."""
    return ns["check_env_verdict"]


def _ref_verdict(prog: str, env: str, expr: str, expected: str,
                 where: str) -> str:
    """The reference's own verdict for the same question, in the same wire."""
    tenv = dict(ENV)
    for part in env.split(";"):
        if ":" in part:
            name, ty = part.split(":", 1)
            tenv[name.strip()] = ty.strip()
    try:
        program = refparser.Parser(prog, "diff.rvl").parse()
        _resolve_type_aliases(program, "diff.rvl")
        _validate_declared_types(program, "diff.rvl")
        types = _lower_type_decls(program, "diff.rvl")
        types[CASES_KEY] = _case_table(types)
    except RevlError:
        return "(table)"
    try:
        node = _ref_parse(expr)
    except RevlError:
        return "(bad)"
    try:
        if expected:
            check_ast(node, expected, tenv, types, "diff.rvl", where)
        else:
            infer_ast(node, tenv, types, filename="diff.rvl")
    except RevlError as exc:
        return f"{_classify(exc)}|{exc.message}"
    return ""


def _agree_msg(verdict_env, prog, env, expr, expected="", where="") -> None:
    want = _ref_verdict(prog, env, expr, expected, where)
    got = verdict_env(prog, env, expr, expected, where)
    assert got == want, (f"{expr!r} (env {env!r}, expected {expected!r}): "
                         f"\n  selfhost {got!r}\n  reference {want!r}")


# ---------------------------------------------------------------- corpus
#
# One row per rule of design §2.2 that this slice owns, ACCEPTED rows
# included: a rule that never fires is not pinned by a refusal.

ROW = "type Row = { name: Str, age: Int }"
OPTROW = "type Row = { name: Str }"

MESSAGE_CORPUS = [
    # (prog, env, expr, expected, where)
    # -- literals ----------------------------------------------------------
    ("", "", "null", "", ""),
    ("", "", "1 + null", "", ""),
    ("", "", "1e999", "", ""),
    ("", "", "-1e999", "", ""),
    ("", "", "1e308", "", ""),
    ("", "", "1.7976931348623157e308", "", ""),
    # -- equality / ordering ------------------------------------------------
    ("", "", "x == s", "", ""),
    ("", "", "s == x", "", ""),
    (ROW, "r: Row", "r == x", "", ""),
    ("", "", "flag < 1", "", ""),
    ("", "", "opt < opt", "", ""),
    ("", "", "xs > xs", "", ""),
    ("", "", "s < s", "", ""),
    # -- boolean and nullish -------------------------------------------------
    ("", "", "x && flag", "", ""),
    ("", "", "flag || s", "", ""),
    ("", "", "x ?? 1", "", ""),
    ("", "", "opt ?? s", "", ""),
    ("", "", "q ?? s", "", ""),
    # -- bitwise -------------------------------------------------------------
    ("", "", "f | f", "", ""),
    ("", "", "x & y", "", ""),
    ("", "", "m << x", "", ""),
    ("", "", "~x", "", ""),
    ("", "", "~s", "", ""),
    ("", "", "~m", "", ""),
    # -- arithmetic ----------------------------------------------------------
    ("", "", "s + 1", "", ""),
    ("", "", "1 + s", "", ""),
    ("", "", "x + flag", "", ""),
    ("", "", "m + x", "", ""),
    ("", "", "x - m", "", ""),
    ("", "", "m % m", "", ""),
    ("", "", "x % m", "", ""),
    ("", "", "-s", "", ""),
    ("", "", "!x", "", ""),
    # -- fields --------------------------------------------------------------
    (OPTROW, "o: Opt[Row]", "o.name", "", ""),
    ("", "", "opt.length", "", ""),
    ("", "", "v.kind", "", "") if False else ("", "v: Any", "v.kind", "", ""),
    ("", "v: Value", "v.kind", "", ""),
    ("", "v: Any", "v.a.b", "", ""),
    (ROW, "r: Row", "r.nope", "", ""),
    (ROW, "r: Row", "r.name", "", ""),
    ("", "", "{ a: 1 }.b", "", ""),
    ("", "", "{ }.b", "", ""),
    ("", "", "s.length", "", ""),
    ("", "", "xs.length", "", ""),
    # -- index ---------------------------------------------------------------
    ("", "", "s[0]", "", ""),
    ("", "", "opt[0]", "", ""),
    ("", "", "xs[s]", "", ""),
    ("", "", "xs[0]", "", ""),
    ("", "", "q[s]", "", ""),
    # -- ternary -------------------------------------------------------------
    ("", "", "flag ? 1 : s", "", ""),
    ("", "", "flag ? xs : opt", "", ""),
    ("", "", "flag ? 1 : 2.5", "", ""),
    ("", "", "(x == s) ? 1 : 2", "", ""),
    # -- lists ---------------------------------------------------------------
    ("", "", "[1, s]", "", ""),
    ("", "", "[1, null]", "", ""),
    ("", "", "[q, y]", "", ""),
    # -- record literals and updates ----------------------------------------
    ("", "", "{ a: 1, b: s }", "", ""),
    ("", "", "{ a: s, a: 1 }", "", ""),
    ("", "a: {h: Str}", "{ a | h = 5 }", "", ""),
    ("", "a: {h: Str}", '{ a | missing = "y" }', "", ""),
    ("", "", "{ x | h = 5 }", "", ""),
    ("", "", "{ opt | h = 5 }", "", ""),
    (ROW, "r: Row", "{ r | nope = 1 }", "", ""),
    (ROW, "r: Row", "{ r | age = s }", "", ""),
    (ROW, "r: Row", "{ r | age = 2 }", "", ""),
    # -- check position: the `where` string is part of the message -----------
    ("", "n: Int", "n", "Int32", "this function's return"),
    ("", "n: Int", "n", "Int", "this function's return"),
    ("", "", "s", "Int", "`let v: Int`"),
    ("", "", "s", "Int", "argument 1 of `f(...)`"),
    ("", "", "[s]", "List[Int]", "this function's return"),
    ("", "", "flag ? s : s", "Int", "this function's return"),
    (ROW, "", "{ name: s, age: x }", "Row", "this function's return"),
    (ROW, "", "{ name: s }", "Row", "this function's return"),
    (ROW, "", "{ name: s, age: x, nope: 1 }", "Row", "this function's return"),
    (ROW, "", "{ name: x, age: x }", "Row", "this function's return"),
    (ROW, "", "{ name: s, age: x }", "Opt[Row]", "this function's return"),
    (ROW, "", "[{ name: s, age: x }]", "List[Row]", "this function's return"),
    (ROW, "r: Row", "{ r | age = s }", "Row", "this function's return"),
    (ROW, "r: Row", "r", "Str", "this function's return"),
    (ROW, "b: {name: Str, age: Int}", "b", "Row", "this function's return"),
    (ROW, "b: {name: Str}", "b", "Row", "this function's return"),
    (ROW, "b: {name: Int, age: Int}", "b", "Row", "this function's return"),
]


@pytest.mark.parametrize("case", MESSAGE_CORPUS,
                         ids=lambda c: f"{c[2]}|{c[3]}")
def test_message_corpus_agrees(verdict_env, case):
    """Tag AND message, byte for byte, against `RevlError.message`."""
    _agree_msg(verdict_env, *case)


def test_the_message_corpus_exercises_both_directions():
    """A corpus of refusals only would not pin the rules that must stay
    silent, and one of acceptances only would pin no message at all."""
    refusals = sum(1 for c in MESSAGE_CORPUS if _ref_verdict(*c))
    assert refusals >= 40, f"only {refusals} refusing rows"
    assert len(MESSAGE_CORPUS) - refusals >= 12, "too few accepting rows"


# ------------------------------------------- the type-layer gap fixtures
#
# The twelve `examples/rejections` documents this slice's algebra decides
# (design §1's "expression typing (T1/T2)" family, plus the two erased-`Any`
# reads). For each one the reference's refusal of the WHOLE DOCUMENT is
# compared against the self-host's verdict for the single expression that
# draws it, in the environment the fixture's own signature declares.
#
# These documents still sit in `false-admit/T1` / `T2` / `TYPE` in
# `tools/gate_reference_census.py`, and they stay there until T3a gives
# `admit_src` a typed statement walk to carry these verdicts through — see
# TYPE_LAYER_GAP in tests/test_selfhost_lower.py, whose rows are deliberately
# NOT deleted by this slice. What is closed here is the algebra; what is open
# is its reach.

FIXTURES = ROOT / "examples" / "rejections"

TYPE_LAYER_EXPRESSIONS = [
    # (fixture path relative to the repo root, prog, env, expr, expected, where)
    ("examples/rejections/t2_null_in_expression.rvl",
     "", "", "null", "", ""),
    ("examples/rejections/t11_field_through_opt.rvl",
     "type Row = { name: Str }", "o: Opt[Row]", "o.name", "", ""),
    ("examples/rejections/t12_str_index.rvl",
     "", "s: Str", "s[0]", "", ""),
    ("examples/rejections/t21_int32_narrow_implicit.rvl",
     "", "n: Int", "n", "Int32", "this function's return"),
    ("examples/rejections/t22_int32_width_mix.rvl",
     "", "a: Int32; b: Int", "a + b", "", ""),
    ("examples/rejections/t23_int32_remainder.rvl",
     "", "a: Int32; b: Int32", "a % b", "", ""),
    ("examples/rejections/t26_anon_record_update_wrong_type.rvl",
     "", "a: {h: Str}", "{ a | h = 5 }", "", ""),
    ("examples/rejections/t27_anon_record_update_undeclared_field.rvl",
     "", "a: {h: Str}", '{ a | missing = "y" }', "", ""),
    ("examples/rejections/t28_bitwise_non_int32.rvl",
     "", "a: Float; b: Float", "a | b", "", ""),
    ("examples/rejections/t29_field_read_on_any.rvl",
     "", "v: Any", "v.kind", "", ""),
    ("examples/rejections/t36_float_literal_range.rvl",
     "", "", "1e999", "", ""),
    ("backends/typescript/tests/fixtures/dynamic_reserved_key.rvl",
     "", "tc: Any", "tc.function.name", "", ""),
]


@pytest.mark.parametrize("case", TYPE_LAYER_EXPRESSIONS,
                         ids=lambda c: Path(c[0]).stem)
def test_type_layer_fixture_expressions_agree(verdict_env, case):
    from revl.compiler import compile_source
    path, prog, env, expr, expected, where = case
    source = (ROOT / path).read_text()
    try:
        compile_source(source, "diff.rvl")
        pytest.fail(f"{path}: the reference no longer refuses this fixture")
    except RevlError as exc:
        want = f"{_classify(exc)}|{exc.message}"
    got = verdict_env(prog, env, expr, expected, where)
    assert got == want, (f"{path}:\n  selfhost  {got!r}\n"
                         f"  reference {want!r}")


def test_every_type_layer_expression_fixture_is_named_once():
    seen = [c[0] for c in TYPE_LAYER_EXPRESSIONS]
    assert len(seen) == len(set(seen)) == 12


# ---------------------------------------------------------------- fuzz

T2A_ATOMS = ["1", "0", "7", "2.5", "x", "y", "f", "s", "flag", "m", "q",
             "opt", "xs", "true", "false", "[1, 2]", "[]", "[s]", "{ h: s }",
             "{ a: 1, b: 2.5 }", "Some(1)", "None"]
T2A_BINOPS = ["+", "-", "*", "/", "%", "<", "<=", ">", ">=", "==", "!=",
              "&&", "||", "??", "&", "|", "^", "<<", ">>"]
T2A_FIELDS = ["h", "a", "b", "name", "length", "missing"]
T2A_PROGS = ["", ROW, "type S = Idle | Busy",
             "type Row = { name: Str }\ntype Box = Wrap(Row) | Empty"]
# Deliberately NOT drawn: an expected type whose nominal head the program does
# not declare. The reference reports that as `T-UNRESOLVED` with an
# `unresolved_nominal_reason`, which needs the declared-type resolution
# types.rvl leaves out of the spelling algebra — and `_validate_declared_types`
# refuses an undeclared annotation before any body is typed, so no real
# check position can reach it.
T2A_EXPECTED = ["", "Int", "Float", "Str", "Bool", "Int32", "Opt[Str]",
                "List[Int]", "List[Str]", "Any", "Never", "{h: Str}",
                "{a: Int, b: Float}", "Map[Str, Int]"]
T2A_WHERE = ["this function's return", "`let v: T`", "argument 1 of `f(...)`",
             "element of `List[Int]`"]


def _gen_t2a(rng: random.Random, depth: int) -> str:
    if depth <= 0:
        return rng.choice(T2A_ATOMS)
    roll = rng.randrange(8)
    sub = lambda: _gen_t2a(rng, depth - 1)  # noqa: E731
    if roll == 0:
        return f"({sub()} {rng.choice(T2A_BINOPS)} {sub()})"
    if roll == 1:
        return f"{rng.choice(['!', '~', '-'])}{sub()}"
    if roll == 2:
        return f"{sub()}.{rng.choice(T2A_FIELDS)}"
    if roll == 3:
        return f"{sub()}[{sub()}]"
    if roll == 4:
        return f"({sub()} ? {sub()} : {sub()})"
    if roll == 5:
        return f"[{sub()}, {sub()}]"
    if roll == 6:
        return (f"{{ {rng.choice(T2A_FIELDS)}: {sub()}, "
                f"{rng.choice(T2A_FIELDS)}: {sub()} }}")
    return f"{{ {sub()} | {rng.choice(T2A_FIELDS)} = {sub()} }}"


@pytest.mark.parametrize("seed", range(12))
def test_generated_expressions_agree_on_the_message(verdict_env, seed):
    """Random expressions over every kind this slice owns, compared on tag AND
    message. Nothing here is a fixed oracle: the two checkers are each other's,
    including on the inputs the generator makes ill-typed by accident, where
    agreeing on WHICH refusal fires FIRST is as much the property under test as
    agreeing that one does."""
    rng = random.Random(9000 + seed)
    for _ in range(50):
        _agree_msg(verdict_env, rng.choice(T2A_PROGS), "",
                   _gen_t2a(rng, rng.randint(1, 3)))


@pytest.mark.parametrize("seed", range(12))
def test_generated_check_positions_agree_on_the_message(verdict_env, seed):
    """The same draw in CHECK position, where the expectation and the `where`
    string are part of the answer."""
    rng = random.Random(7000 + seed)
    for _ in range(50):
        _agree_msg(verdict_env, rng.choice(T2A_PROGS), "",
                   _gen_t2a(rng, rng.randint(1, 3)),
                   rng.choice(T2A_EXPECTED), rng.choice(T2A_WHERE))


# `List[Row]` is deliberately NOT here: the descent reaches the record against
# `Row` only through the element rule, which `compatible` refuses on the head
# before the reason is sought, so the reference reports a plain T1 there too.
@pytest.mark.parametrize("expected", ["Row", "S", "Opt[S]", "Opt[Opt[Row]]"])
def test_an_undeclared_nominal_expectation_is_the_one_bounded_divergence(
        verdict_env, expected):
    """The single place this slice knowingly differs, pinned rather than
    described.

    A record literal meeting a nominal head the compilation does not declare is
    not a mismatch the checker made — it is a comparison it never made. The
    reference says so with `unresolved_nominal_reason`, which needs the
    declared-type resolution `types.rvl` leaves out of the spelling algebra
    (design §3.1, "NOT in this slice"), so the self-host reports the mismatch
    without that clause. The divergence is bounded exactly here: the message is
    the reference's up to the `;`, and the tag differs only because a
    T-UNRESOLVED carries no type-layer marker.

    Nothing real reaches it — `_validate_declared_types` refuses an undeclared
    annotation before any body is typed — which is why this is a pin and not a
    bug, and why the T2a fuzz above does not draw one. A slice that ports the
    resolution deletes this test and adds the rows to the corpus."""
    where = "`let v: T`"
    want = _ref_verdict("", "", "{ h: s }", expected, where)
    got = verdict_env("", "", "{ h: s }", expected, where)
    assert "has no declaration in this compilation" in want
    assert got == "T1|" + want.split("|", 1)[1].split(";")[0]


def test_checker_in_file_tests_pass(ns):
    """`selfhost/checker.rvl`'s own `test` blocks, run under the python
    backend. Nothing ran them before this slice, which is how an assertion
    contradicting the file's own REJECTED corpus (`s + 1`, refused since issue
    #549) survived in it."""
    tests = ns.get("REVL_TESTS")
    assert tests and len(tests) >= 30, \
        "expected the file's test blocks in REVL_TESTS"
    for entry in tests:
        fn = entry[-1] if isinstance(entry, tuple) else entry
        fn()
