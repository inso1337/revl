"""A verbatim `@ts` extern body declared `Int` is coerced on the way out.

Issue #2147. This tier's `Int` is a `bigint` (`emit.py` `TYPE_MAP`) while a
verbatim JS body naturally yields a `number` — `Math.floor(...)` above all.
The emitter spliced the body under a `: bigint` annotation, so the emitted
function's annotation was false and nothing threw at the boundary. The value
survived until its first use in arithmetic, where it failed inside the
language's OWN helpers, all of them typed `bigint`:

    TypeError: Cannot mix BigInt and other types, use explicit conversions

measured on `iso_date(day_number_now())`, which throws in `civil_date` at
`days + 719468n`. The same call with a literal, `iso_date(day_number(2026, 11,
8))`, passed — so the defect is the extern boundary and not the date code.

The fix is the RESULT-side mirror of the ARGUMENT-side conversion issue #1566
already established at the seam (commit 15fcdfd05): `bridge.ts`'s `toBigInt`
coerces a wire value by its DECLARED type (`Int` -> `bigint`, leaving a
non-integer for the method to refuse loudly). The same three cases in the same
order now run at the extern result boundary, so the two sides of one boundary
cannot disagree. Nothing at the boundary says which numbers are `Int`s, so the
coercion follows the declared type, exactly as the seam does.

`Int` is the only declared return that needs it: `Int32`/`Float` are already a
`number`, `Bool`/`Str`/`Bytes` are what a JS body naturally returns, and `Unit`
yields nothing to convert. Every other module is byte-identical — pinned here
by `test_a_module_with_no_int_extern_is_byte_identical` and, end to end, by
`test_async_extern_ts.py::test_async_http_golden_is_current`.

The durable exit test is the golden `golden/ts_extern_result_int.ts`, which
`npm run typecheck` compiles one program per module
(`scripts/typecheck-generated.mjs`, tsconfig.generated.json) — a reverted
coercion fails it with `Type 'number' is not assignable to type 'bigint'`,
which is the issue's own complaint ("the emitted function's annotation is
false") stated as a type error. Regenerate the golden with
tools/regen_goldens.py typescript.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

FIXTURE = BACKEND / "tests" / "fixtures" / "ts_extern_result_int.ir.json"
GENERATED = BACKEND / "tests" / "generated"


def _load_ts_emit():
    spec = importlib.util.spec_from_file_location(
        "revl_ts_emit_extern_result_int", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ir():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# --- the coercion is emitted, by the DECLARED return type -------------------


def test_an_int_declared_extern_body_is_coerced_on_the_way_out():
    """The body is still verbatim — it keeps its own `return` — and its result
    is coerced by the declared type before it leaves the function."""
    out = _load_ts_emit().emit(_ir())
    # the declared type is unchanged: the function still IS a bigint function
    assert "export function day_number_now(): bigint {" in out
    # ... and now it is one. The body keeps its shape and its own return.
    assert ("export function day_number_now(): bigint {\n"
            "  return revlToBigInt((() => {\n"
            "    const now = new globalThis.Date(0)\n") in out
    assert ("    return Math.floor(globalThis.Date.UTC(\n"
            "      now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())"
            " / 86400000)\n"
            "  })())\n"
            "}") in out
    # the same for the above-2^53 body, whose literal is a JS `number`
    assert ("export function big_day_number(): bigint {\n"
            "  return revlToBigInt((() => {\n"
            "    return 9007199254740994\n"
            "  })())\n"
            "}") in out


def test_the_coercion_is_the_seam_s_own_toBigInt():
    """The result side mirrors the argument side (#1566, `bridge.ts`), so the
    two sides of one boundary cannot disagree: the same three cases, in the
    same order, and a non-integer left for the caller to refuse loudly."""
    out = _load_ts_emit().emit(_ir())
    assert ("function revlToBigInt(v: unknown): bigint {\n"
            "  if (typeof v === 'bigint') return v\n"
            "  if (typeof v === 'number' && Number.isInteger(v))"
            " return BigInt(v)\n"
            "  return v as bigint // not an integer: leave it for the caller"
            " to refuse loudly\n"
            "}") in out


def test_the_coercion_runs_on_the_result_of_the_body_not_instead_of_it():
    """The uses the issue names: `+`, `div_trunc` and `iso_date` all receive a
    `bigint`, because the value that crossed is one."""
    out = _load_ts_emit().emit(_ir())
    assert "return revlI64(day_number_now() + 1n)" in out
    assert "return revlDivTrunc(day_number_now(), 2n)" in out
    assert "return iso_date(day_number_now())" in out
    assert "return revlI64(big_day_number() + 1n)" in out


def test_the_helper_is_emitted_only_where_it_is_used():
    """A module with no `Int`-declared extern gets no `revlToBigInt` — the
    repo's "helpers only where used" rule (see `_revl_helpers`), and the gate
    that keeps an unused function out of every other module's output."""
    m = _load_ts_emit()
    assert "revlToBigInt" in m.emit(_ir())
    no_int_extern = json.loads(
        (BACKEND / "tests" / "fixtures" / "async_http.ir.json")
        .read_text(encoding="utf-8"))
    assert "revlToBigInt" not in m.emit(no_int_extern)


def test_a_module_with_no_int_extern_is_byte_identical():
    """`async_http`'s extern is declared `Str`, so nothing about this change
    reaches it — the golden is the pre-change bytes, still."""
    m = _load_ts_emit()
    no_int_extern = json.loads(
        (BACKEND / "tests" / "fixtures" / "async_http.ir.json")
        .read_text(encoding="utf-8"))
    assert m.emit(no_int_extern) == (
        BACKEND / "golden" / "async_http.ts").read_text(encoding="utf-8")


def test_a_ref_thunk_is_not_coerced():
    """A `@ts ref` extern has no verbatim body — the emitter emits a lazy
    import thunk — so there is no result boundary here to coerce. The real
    `ts_witnessed_fs` fixture is all refs; it must gain nothing."""
    m = _load_ts_emit()
    refs_only = json.loads(
        (BACKEND / "tests" / "fixtures" / "ts_witnessed_fs.ir.json")
        .read_text(encoding="utf-8"))
    assert all("ts" not in (e.get("bodies") or {})
               for e in refs_only["externs"] if e.get("refs"))
    assert "revlToBigInt" not in m.emit(refs_only)
    # the predicate itself, on a ref-shaped extern declared `Int`
    assert not m._ts_extern_needs_int_coercion({
        "name": "host_now", "class": "pure", "params": [], "returns": "Int",
        "refs": {"ts": {"path": "backends/typescript/revl_fs_ts.ts"}},
    })


def test_the_golden_is_current():
    m = _load_ts_emit()
    golden = (BACKEND / "golden" / "ts_extern_result_int.ts")
    assert m.emit(_ir()) == golden.read_text(encoding="utf-8")


# --- the runtime proof ------------------------------------------------------
#
# The strongest available evidence: the emitted bytes, executed by node. The
# type-level half is `tsc` over the golden (CI's typecheck-generated step);
# this half is the values the issue measured.

_DRIVER = """
const cases: Array<[string, () => unknown]> = [
  ['day_number_now', day_number_now],
  ['plus_one', plus_one],
  ['halved', halved],
  ['iso_date_now', iso_date_now],
  ['big_day_number', big_day_number],
  ['big_plus_one', big_plus_one],
]
for (const [name, f] of cases) {
  try {
    const v = f()
    console.log(name + ' = ' + String(v) + ' [' + typeof v + ']')
  } catch (e) {
    console.log(name + ' THREW ' + (e as Error).message)
  }
}
"""

_EXPECTED = """\
day_number_now = 0 [bigint]
plus_one = 1 [bigint]
halved = 0 [bigint]
iso_date_now = 719468 [bigint]
big_day_number = 9007199254740994 [bigint]
big_plus_one = 9007199254740995 [bigint]
"""


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="no node on PATH; the ts tier's runtime is the proof")
def test_the_emitted_module_runs_and_every_value_is_an_exact_bigint():
    """Executed by plain node, the tier's runtime.

    Before the coercion every one of the six printed
    `THREW Cannot mix BigInt and other types` — the first four because the
    `number` from `day_number_now` met `+ 1n`, `revlDivTrunc`'s bigint `/`, and
    `civil_date`'s `days + 719468n`; the last two because the same happened to
    `big_day_number`. `big_plus_one = 9007199254740995` is the precision seam:
    that value is ODD and past 2^53, so no double can hold it, and only a
    `bigint` computed it. `9007199254740994` is the largest `number` the body
    could return exactly there, so the assertion fails the moment the value is
    a rounded `number` again.
    """
    GENERATED.mkdir(parents=True, exist_ok=True)
    # `../../runtime.ts` is what emit.py writes for a module at this depth, and
    # runtime.ts' only cordis imports are `import type` — erased by node's type
    # stripping — so the emitted module runs as-is.
    path = GENERATED / "ts_extern_result_int_2147.ts"
    try:
        path.write_text(_load_ts_emit().emit(
            _ir(), runtime_import="../../runtime.ts") + _DRIVER,
            encoding="utf-8")
        proc = subprocess.run(["node", str(path)], cwd=BACKEND,
                              capture_output=True, text=True, timeout=300)
    finally:
        path.unlink(missing_ok=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == _EXPECTED, proc.stdout + proc.stderr
