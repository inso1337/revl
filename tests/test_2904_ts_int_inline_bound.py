"""Issue #2904: on the TypeScript tier an Int `+ - *` or unary `-` inside a
module fn or test body tests the i64 bound INLINE instead of calling
`revlI64` for every operation, and the helper's own test is `BigInt.asIntN`.

`Int` stays a JS bigint; only the cost changes. The inline form assigns the
operation to the function's `$revl_i64` temp once and compares it with its
`BigInt.asIntN(64, ...)` image, which is equal exactly in [-2^63, 2^63 - 1];
an out-of-range result still raises through `revlI64`, so the message is
unchanged. An arrow body or a match arm keeps the call, because the temp lives
in the enclosing function and reaching it from a closure would make it a
context slot (a module-level temp measured 46% slower than the call).
"""

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

T = "$revl_i64"


def _inline(op: str) -> str:
    return f"(BigInt.asIntN(64, {T} = {op}) === {T} ? {T} : revlI64({T}))"


ARITH = """
pub fn add(a: Int, b: Int) -> Int { return a + b }
pub fn sub(a: Int, b: Int) -> Int { return a - b }
pub fn mul(a: Int, b: Int) -> Int { return a * b }
pub fn neg(a: Int) -> Int { return -a }
pub fn nested(a: Int, b: Int, c: Int) -> Int { return (a + b) + c }
pub fn negsub(a: Int, b: Int) -> Int { return -(a - b) }
pub fn div(a: Int, b: Int) -> Int { return a.div_trunc(b) }
pub fn idiv(a: Int, b: Int) -> Int { return a.div_euclid(b) }
pub fn apply(f: (Int) -> Int, x: Int) -> Int { return f(x) }
pub fn calls(f: (Int) -> Int, x: Int) -> Int { return f(x) + f(x + 1) }
pub fn lam(x: Int) -> Int { return apply((y) => y * 2, x) }
pub fn arm(o: Opt[Int]) -> Int { return match o { Some(v) => v + 1, None => 0 } }
pub fn later(cb: () -> Async[Int], k: Int) -> Int { return cb() + k }
pub fn plain(a: Int, b: Int) -> Bool { return a < b }
test "sums" {
  let x = add(2, 3)
  assert x + 1 == 6
}
"""


def _emit_ts(source: str) -> str:
    spec = importlib.util.spec_from_file_location(
        "emit_ts_2904", ROOT / "backends" / "typescript" / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit(compile_source(source, "int_inline_2904.rvl"))


def _body(emitted: str, fn: str) -> str:
    """The emitted text of one function, signature to closing brace."""
    start = emitted.index(f"function {fn}(")
    return emitted[start:emitted.index("\n}", start)]


def test_int_plus_no_longer_renders_the_helper_call():
    body = _body(_emit_ts(ARITH), "add")
    assert "revlI64(a + b)" not in body, body
    assert _inline("a + b") in body, body


def test_every_int_site_in_a_fn_body_tests_the_bound_inline():
    emitted = _emit_ts(ARITH)
    for fn, op in [("sub", "a - b"), ("mul", "a * b"), ("neg", "-a"),
                   ("nested", "a + b"), ("negsub", "a - b")]:
        body = _body(emitted, fn)
        assert _inline(op) in body, f"{fn}: {body}"
        # one temp per function, declared first
        assert body.count(f"let {T} = 0n") == 1, f"{fn}: {body}"
        assert body.split("\n")[1].strip() == f"let {T} = 0n", f"{fn}: {body}"
    # nesting: the inner site is the outer site's left operand
    assert _inline(f"{_inline('a + b')} + c") in _body(emitted, "nested")
    assert _inline(f"-{_inline('a - b')}") in _body(emitted, "negsub")
    # the test body is a function scope too
    test_body = emitted[emitted.index('it("sums"'):]
    assert test_body.split("\n")[1].strip() == f"let {T} = 0n", test_body
    assert _inline("x + 1n") in test_body, test_body


def test_closures_keep_the_call_and_no_temp_is_declared_without_a_site():
    emitted = _emit_ts(ARITH)
    assert "revlI64(y * 2n)" in _body(emitted, "lam")
    assert "revlI64(v + 1n)" in _body(emitted, "arm")
    for fn in ("lam", "arm", "plain", "div", "idiv", "apply"):
        assert T not in _body(emitted, fn), fn
    # the temp is never module-level
    assert f"\nlet {T}" not in emitted
    # the helper keeps the trapping path and its constants
    assert "if (BigInt.asIntN(64, v) !== v) throw new RangeError('revl: Int overflow')" in emitted
    assert "const REVL_I64_MIN = -(2n ** 63n)" in emitted


PROBE = """
function probe(label: string, run: () => unknown) {
  try { const v = run(); console.log(label + ":no-throw:" + String(v)) }
  catch (e) { console.log(label + ":" + (e as Error).message) }
}
const MAX = 9223372036854775807n, MIN = -9223372036854775808n
probe("add-max", () => add(MAX, 1n))
probe("sub-min", () => sub(MIN, 1n))
probe("mul-max", () => mul(MAX, 2n))
probe("neg-min", () => neg(MIN))
probe("negsub-min", () => negsub(MIN, 0n))
probe("nested-max", () => nested(MAX, 0n, 1n))
probe("div-min", () => div(MIN, -1n))
probe("idiv-min", () => idiv(MIN, -1n))
probe("lam-max", () => lam(MAX))
probe("arm-max", () => arm(MAX))
let seen: bigint[] = []
const f = (x: bigint): bigint => { seen.push(x); return x * 10n }
console.log("calls:" + String(calls(f, 4n)) + ":" + seen.join(","))
console.log("in-range:" + [add(2n, 3n), neg(4n), sub(-1n, 2n), nested(MAX, -1n, 1n),
  negsub(MIN + 1n, 0n), mul(-3037000499n, 3037000499n), lam(5n), arm(6n),
  add(9007199254740993n, 0n)].map(String).join(","))
const slow = (v: bigint, ms: number) => () => new Promise<bigint>(r => setTimeout(() => r(v), ms))
Promise.all([later(slow(10n, 20), 1n), later(slow(20n, 1), 2n), later(slow(MAX, 5), 0n)])
  .then(v => console.log("async:" + v.map(String).join(",")))
later(slow(MAX, 1), 1n).then(v => console.log("async-max:no-throw:" + v),
  e => console.log("async-max:" + e.message))
"""

EXPECTED = [
    "add-max:revl: Int overflow",
    "sub-min:revl: Int overflow",
    "mul-max:revl: Int overflow",
    "neg-min:revl: Int overflow",
    "negsub-min:revl: Int overflow",
    "nested-max:revl: Int overflow",
    "div-min:revl: Int overflow",
    "idiv-min:revl: Int overflow",
    "lam-max:revl: Int overflow",
    "arm-max:revl: Int overflow",
    "calls:90:4,5",
    "in-range:5,-4,-3,9223372036854775807,9223372036854775807,"
    "-9223372030926249001,10,7,9007199254740993",
    "async-max:revl: Int overflow",
    "async:11,22,9223372036854775807",
]


def test_overflow_still_throws_on_the_emitted_module(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    src = _emit_ts(ARITH)
    # the module imports the runtime and vitest; a bare node run needs neither
    src = "\n".join(line for line in src.splitlines()
                    if not line.startswith("import "))
    src = src[:src.index('it("sums"')]
    module = tmp_path / "mod.ts"
    module.write_text(src + PROBE, encoding="utf-8")
    proc = subprocess.run(["node", str(module)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == EXPECTED
