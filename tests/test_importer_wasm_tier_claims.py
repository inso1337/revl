"""What the importers SAY about the cordis-wasm tier, checked against the tier.

`revl import wit` writes a tier note into every file it generates, and `revl
import cordis` / `revl import openapi` refuse `--backend wasm` with a reason.
Both are claims about `backends/wasm/emit.py` made from another file, and both
are copied forward — a generated header lands in the user's repository, and a
refusal message is read as a diagnosis. All three said the tier was "i32-only"
long after `Int` became an i64 and rich values started crossing the service
boundary as canonical-ABI pointers (#218), so a reader was told to blame their
`Int` when the real cause was a `Float`, a `Map` or a function type.

These tests therefore do NOT pin the corrected strings — the next wrong string
would pass that just as well. They drive the wasm emitter to find out what it
actually refuses, and require the importers' text to name that set and nothing
else.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from _backend_import import backend_emitter  # noqa: E402
from revl import RevlError, compile_source  # noqa: E402
from revl.import_cordis import import_cordis  # noqa: E402
from revl.import_openapi import import_openapi  # noqa: E402
from revl.import_wit import _WASM_REFUSED_TYPES, import_wit_file  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "wit"

#: One revl service per name the tier note can reach for, as source the
#: frontend accepts on any tier. The wasm emitter is then asked to lower it;
#: whether it can is the ONLY authority these tests use.
_PROBES = {
    "Int": "service S { fn f(a: Int) -> Int }",
    "Bool": "service S { fn f(a: Bool) -> Int }",
    "Str": "service S { fn f(a: Str) -> Int }",
    "Bytes": "service S { fn f(a: Bytes) -> Int }",
    "List": "service S { fn f(a: List[Int]) -> Int }",
    "record": "type R = { x: Int }\nservice S { fn f(a: R) -> Int }",
    "Opt": "service S { fn f(a: Opt[Int]) -> Int }",
    "Result": "service S { fn f(a: Result[Int, Str]) -> Int }",
    "Float": "service S { fn f(a: Float) -> Int }",
    "Map": "service S { fn f(a: Map[Str, Int]) -> Int }",
    "function types": "service S { fn f(a: (Int) -> Int) -> Int }",
}

_COMPONENT = """
component C provides s: S {
  provide s {
    fn f(a) = 1
  }
}
"""


def _wasm_refusal(name: str) -> str | None:
    """The wasm emitter's refusal for a service taking `name`, or None."""
    emit = backend_emitter("wasm")
    ir = compile_source(_PROBES[name] + _COMPONENT)
    try:
        emit.emit(ir)
    except emit.EmitError as exc:
        return str(exc)
    return None


def _tier_split() -> tuple[set[str], set[str]]:
    """(what the wasm tier lowers, what it refuses) — measured, not declared."""
    refused = {n for n in _PROBES if _wasm_refusal(n) is not None}
    return set(_PROBES) - refused, refused


# ------------------------------------------------------- the tier's own answer

def test_the_wasm_tier_lowers_rich_values_and_refuses_only_float_map_and_fns():
    """The premise the importers' text rests on, measured at the emitter.

    If this fails the tier moved, and the importers' note (plus
    `_WASM_REFUSED_TYPES`) is what has to move with it.
    """
    lowers, refused = _tier_split()
    assert {"Int", "Bool", "Str", "Bytes", "List", "record", "Opt", "Result"} <= lowers
    assert refused == {"Float", "Map", "function types"}


def test_an_int_crosses_the_wasm_service_boundary_as_an_i64():
    """The specific fact the "i32-only" claim got wrong."""
    emit = backend_emitter("wasm")
    wat = emit.emit(compile_source(_PROBES["Int"] + _COMPONENT))["C"]
    assert '(export "provide:s.f") (param $p_a i64) (result i64)' in wat


# --------------------------------------------- the generated header's own claim

def _wasm_note(header: str) -> str:
    match = re.search(r"^// What that tier refuses is (.+)\.$", header, re.M)
    assert match is not None, (
        "the `--backend wasm` header no longer states what the tier refuses in "
        "the single line this test reads; keep the claim machine-checkable "
        "rather than dropping the anchor:\n" + header)
    return match.group(1)


def test_the_generated_wasm_header_names_exactly_what_the_tier_refuses():
    generated = import_wit_file(str(FIXTURES / "catalog.wit"), backend="wasm")
    named = {part.strip().strip("`") for part in _wasm_note(generated).split(",")}
    _lowers, refused = _tier_split()
    assert named == refused, (
        "the generated header tells the reader to blame a construct the wasm "
        f"tier accepts, or omits one it refuses: header says {sorted(named)}, "
        f"the emitter refuses {sorted(refused)}")
    assert set(_WASM_REFUSED_TYPES) == refused


def test_the_generated_wasm_header_does_not_blame_a_type_that_lowers():
    generated = import_wit_file(str(FIXTURES / "catalog.wit"), backend="wasm")
    header = generated.split("\ntype ")[0]
    assert "i32-only" not in header
    named = {part.strip().strip("`") for part in _wasm_note(header).split(",")}
    lowers, _refused = _tier_split()
    assert not (named & lowers), (
        f"the header names {sorted(named & lowers)} as refused, but the wasm "
        "emitter lowers it")


def test_the_py_backend_header_carries_no_wasm_tier_note():
    """The note is a wasm-tier fact; a `--backend py` file must not inherit it."""
    generated = import_wit_file(str(FIXTURES / "catalog.wit"), backend="py")
    assert "What that tier refuses" not in generated


# ------------------------------------- the two `--backend wasm` refusal hints

def _refusal_hint(call) -> str:
    with pytest.raises(RevlError) as excinfo:
        call()
    return str(excinfo.value)


@pytest.mark.parametrize("importer,call", [
    ("cordis", lambda: import_cordis("export function x() {}", filename="p.ts",
                                     backend="wasm")),
    ("openapi", lambda: import_openapi(
        {"openapi": "3.0.3", "info": {"title": "T"},
         "paths": {"/x": {"get": {"operationId": "getX",
                                  "responses": {"200": {"description": "ok"}}}}}},
        filename="p.json", backend="wasm")),
])
def test_the_backend_wasm_refusal_blames_a_missing_host_not_a_value_width(importer, call):
    """`wasm` is absent from these two importers because there is nothing on the
    other side of the tag — no node/TS runtime, no network seam. Neither reason
    has anything to do with which values the tier carries, so naming one of
    those values (or a wasm width) sends the reader to rewrite working code."""
    message = _refusal_hint(call)
    assert "i32" not in message, importer
    lowers, _refused = _tier_split()
    blamed = {name for name in lowers if name in message}
    assert not blamed, (
        f"the {importer} `--backend wasm` refusal names {sorted(blamed)}, which "
        "the wasm tier lowers — the reason is a missing host, not a value")
    assert re.search(r"node|TypeScript|network|HTTP client", message), (
        f"the {importer} `--backend wasm` refusal no longer says what is "
        f"missing on that tier: {message}")
