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
from revl.typecheck import infer_ast  # noqa: E402


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
ENV = {"x": "Int", "y": "Int", "f": "Float", "s": "Str", "flag": "Bool"}


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
    "s + 2.5", "x / 0.5", "(2.5 + 0.5) * x",
    "q",  # not in the environment: the gradual frontier's unknown
    # arithmetic
    "1 + 2", "x - y", "x * 2", "7 / 2", "x % 3",
    "f + 1", "1 + f", "f - f", "f * f", "f / f", "f % 2", "x / f", "x % f",
    "(1 + 2) * x", "x - y - z", "7 / 2 / 2",
    # string concatenation
    "s + s", "s + 1", "1 + s", "s + f", "s + q",
    # ordering (Str orders too)
    "x < y", "x <= f", "f > 1", "f >= x", "s < s", "s <= q",
    # equality
    "x == y", "x != y", "s == s", "flag == false", "flag != true",
    "q == x", "q != q", "x === y", "x !== y",  # === canonicalizes to ==
    # nesting across the families
    "1 + 2 < 4", "x < y == true", "(x == y) == (flag == false)",
    "1 < 2 == s < s",
]

REJECTED = [
    # Bool in arithmetic (refusal-parity: `/` on Bool is the headline case)
    "flag / flag", "flag + 1", "true * false", "x + flag", "1 % flag",
    "flag / x", "x - true",
    # Str in arithmetic (only `+` takes a Str, and only beside Str/numeric)
    "s * s", "s - s", "1 % s", "s / s", "s * 2", "2 / s",
    # Bool / Str under ordering
    "flag < 1", "x < true", "flag <= flag", "true > false",
    # equality across incompatible types
    "x == s", "s == x", "flag == x", "1 == flag", "s != 2",
    # null has no type (absence is Opt[T])
    "null", "null + 1", "1 + null", "null == null",
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

ACCEPTED_PROGRAMS = [
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
