"""Issue #3179: a function-local name must not replace an emitted helper.

The ts tier emits checked Int arithmetic as `revlI64(a + b)`, a list index as
`revlIndex(xs, i)` and an `Opt` test as `x !== undefined`. Those are bare
names, so a `let`, a parameter, a match binder, a lambda parameter or a loop
variable spelled `revlI64`, `revlIndex` or `undefined` used to become a JS
binding of the same name inside the function and the emitted code called the
user's value instead: the overflow trap and the index bound were gone and a
`None` arm never fired.

Each case below is emitted and RUN under node against the real runtime, so a
rename that only changed the text but not the behaviour (or the reverse) is
caught. The drift test emits the example corpus and checks every module-scope
binding of the output against the local family, so a new helper or import
cannot be added without being covered.
"""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from revl import compile_source  # noqa: E402
from _node_capability import node_ts_skip_reason  # noqa: E402

_NODE_REASON = node_ts_skip_reason("node is required to run the emitted ts")
needs_node = pytest.mark.skipif(_NODE_REASON is not None, reason=str(_NODE_REASON))


def _load():
    spec = importlib.util.spec_from_file_location("revl_ts_emit_3179", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EMIT = _load()

SOURCE = """\
pub fn add(a: Int, b: Int) -> Int {
  let revlI64 = (x: Int) => 7
  return a + b
}
pub fn apply(revlI64: (Int) -> Int, a: Int, b: Int) -> Int = a * b
pub fn pick(undefined: Int, m: Map[Str, Str], k: Str) -> Str = match (m.lookup(k)) {
    Some(v) => v,
    None => "denied"
}
pub fn at(xs: List[Int], i: Int) -> Int {
  let revlIndex = (ys: List[Int], j: Int) => 42
  return xs[i]
}
pub fn binder(o: Opt[Int], a: Int, b: Int) -> Int = match o {
    Some(revlI64) => a + b,
    None => 0
}
pub fn lam(a: Int, b: Int) -> Int {
  let f = (revlI64: Int) => a + b
  return f(1)
}
pub fn loop(xs: List[Int], a: Int, b: Int) -> Int {
  for (revlI64 of xs) {
    return a + b
  }
  return 0
}
pub fn escape(x: Int) -> Int = x + 100
pub fn local_beside_module_fn(a: Int) -> Int {
  let escape = 5
  return escape + a
}
pub fn plain(total: Int, count: Int) -> Int {
  let sum = total + count
  return sum
}
"""

BIG = "2n ** 62n"

# (label, call, expected stdout). `throws:<msg>` is a RangeError the checked
# program raises.
CASES = [
    ("let revlI64", "M.add(2n, 3n)", "5"),
    ("let revlI64 overflow", f"M.add({BIG}, {BIG})", "throws:revl: Int overflow"),
    ("param revlI64", "M.apply((x: bigint) => 0n, 2n, 3n)", "6"),
    ("param undefined", "M.pick(5n, new Map(), 'z')", "denied"),
    ("param undefined hit", "M.pick(5n, new Map([['z', 'A']]), 'z')", "A"),
    ("let revlIndex", "M.at([1n], 5n)", "throws:revl: list index out of range"),
    ("match binder revlI64", f"M.binder(1n, {BIG}, {BIG})", "throws:revl: Int overflow"),
    ("lambda param revlI64", "M.lam(2n, 3n)", "5"),
    ("for variable revlI64", "M.loop([1n], 2n, 3n)", "5"),
    ("local beside module fn", "M.local_beside_module_fn(1n)", "6"),
    ("module fn kept", "M.escape(1n)", "101"),
]


def _emit(src: str = SOURCE, runtime_import: str = "../runtime.ts") -> str:
    return EMIT.emit(compile_source(src), runtime_import=runtime_import)


@needs_node
def test_shadowing_locals_keep_the_checked_semantics(tmp_path):
    (tmp_path / "m.ts").write_text(_emit(runtime_import=str(BACKEND / "runtime.ts")),
                                   encoding="utf-8")
    lines = ["import * as M from './m.ts'",
             "const t = (f: () => unknown) => { try { console.log(String(f())) }"
             " catch (e) { console.log('throws:' + (e as Error).message) } }"]
    lines += [f"t(() => {call})" for _, call, _ in CASES]
    (tmp_path / "h.ts").write_text("\n".join(lines) + "\n", encoding="utf-8")
    proc = subprocess.run(["node", "h.ts"], capture_output=True, text=True, cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    got = proc.stdout.splitlines()
    assert len(got) == len(CASES), proc.stdout
    wrong = [(label, want, seen) for (label, _, want), seen in zip(CASES, got) if seen != want]
    assert wrong == []


def test_locals_are_renamed_and_ordinary_names_are_not():
    out = _emit()
    assert "const revlI64 =" not in out
    assert "const revlIndex =" not in out
    assert "export function pick(undefined_: bigint" in out
    assert "export function apply(revlI64_: " in out
    assert "for (const revlI64_ of xs)" in out
    # the checked calls still name the helpers and the global
    assert "return revlI64(a + b)" in out
    assert "!== undefined)" in out
    # ordinary names and a local named like a module fn keep their spelling
    assert "export function plain(total: bigint, count: bigint): bigint {" in out
    assert "const sum = revlI64(total + count)" in out
    assert "const escape = 5n" in out


def test_provide_method_locals_and_params_are_renamed():
    src = (
        "service Calc {\n"
        "  fn add(a: Int, b: Int) -> Int\n"
        "  fn pick(undefined: Int, m: Map[Str, Str], k: Str) -> Str\n"
        "}\n"
        "component C provides calc: Calc {\n"
        "  provide calc {\n"
        "    fn add(a, b) {\n"
        "      let revlI64 = (x: Int) => 7\n"
        "      return a + b\n"
        "    }\n"
        "    fn pick(undefined, m, k) = match (m.lookup(k)) {\n"
        "      Some(v) => v,\n"
        '      None => "denied"\n'
        "    }\n"
        "  }\n"
        "}\n"
    )
    out = _emit(src)
    assert "const revlI64 =" not in out
    assert "return revlI64(a + b)" in out
    assert re.search(r"\bpick\(undefined\b(?!_)", out) is None
    assert "!== undefined)" in out


def test_family_covers_helpers_globals_and_the_revl_prefix():
    assert {"revlI64", "revlIndex", "revlEq", "REVL_I64_MIN"} <= EMIT.TS_HELPER_NAMES
    for name in ("revlI64", "revlNotYetAHelper", "undefined", "BigInt", "host",
                 "record", "_revlSecret", "REVL_I32_MAX"):
        assert EMIT._reserved_for_locals(name) or name in EMIT.EMITTER_RESERVED, name
    for name in ("total", "revl", "revlish", "sum", "x"):
        assert not EMIT._reserved_for_locals(name), name


_IMPORT_RE = re.compile(r"^import (?!type\b)\{([^}]*)\} from", re.M)


def _module_scope_names(out: str) -> set[str]:
    names = set(EMIT._TS_MODULE_DECL_RE.findall(out))
    for group in _IMPORT_RE.findall(out):
        for part in group.split(","):
            part = part.strip()
            if part:
                names.add(part.split(" as ")[-1].strip())
    return names


def test_every_emitted_module_scope_binding_is_reserved_for_locals():
    """Drift guard: a helper or import the emitter adds later is a bare name a
    local could shadow, so it must be in the local family. `$` names are out
    of revl's identifier alphabet and the user's own module names are #3127."""
    seen: set[str] = set()
    uncovered: dict[str, str] = {}
    emitted = 0
    for path in sorted((ROOT / "examples").rglob("*.rvl")) + [None]:
        src = SOURCE if path is None else path.read_text(encoding="utf-8")
        try:
            ir = compile_source(src)
            out = EMIT.emit(ir)
        except Exception:
            continue
        emitted += 1
        user = EMIT._module_value_names(ir)
        for name in _module_scope_names(out):
            seen.add(name)
            if "$" in name or name in user or name.rstrip("_") in user:
                continue
            if EMIT._reserved_for_locals(name) or name in EMIT.EMITTER_RESERVED:
                continue
            uncovered[name] = str(path)
    # the reader saw real helpers and imports (a scan that saw nothing proves nothing)
    assert emitted >= 20
    assert {"revlI64", "revlEq", "host"} <= seen
    assert uncovered == {}
