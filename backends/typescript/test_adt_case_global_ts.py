"""Issue #1910: an ADT case named after a JavaScript global.

A case name reaches the emitted TS as a module-scope `export function <Case>()`,
so a case called `Date`, `Boolean`, `JSON`, … used to shadow that global inside
every `@ts` body of the same module:

    export function Date(): Kind { return { kind: "Date" } }
    export function year_of(ms: bigint): bigint {
      return BigInt(new Date(Number(ms)).getUTCFullYear())   // TS2339
    }

The case DECLARATION is what shadows, so it is the one position escaped
(`JS_GLOBAL_RESERVED` through `_mangle`'s `extra`). The tag string it returns —
`{ kind: "Date" }` — and the match label it is compared under — `case "Date":` —
stay RAW: both are string values that bind no name, and escaping them would
break the tag contract the type union declares.

These are toolchain-free checks (the main venv, no `npm`); the end-to-end proof
is the emitted module compiling and running under Node in the vitest suite.
"""

import importlib.util
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

SOURCE = (
    "type Kind = Text | Date | Boolean(Int)\n"
    "\n"
    "extern pure fn year_of(ms: Int) -> Int\n"
    "  = @ts { return BigInt(new Date(Number(ms)).getUTCFullYear()) }\n"
    "\n"
    "fn label(k: Kind) -> Str = match k {\n"
    '  Text => "text",\n'
    '  Date => "date",\n'
    '  Boolean(v) => "boolean",\n'
    "}\n"
    "\n"
    "fn plain() -> Kind { return Text }\n"
)


def _load():
    spec = importlib.util.spec_from_file_location("revl_ts_emit_case_global", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _emit(src: str) -> str:
    return _load().emit(compile_source(src))


def test_global_named_case_declaration_is_escaped():
    """The declaration is renamed, so the global stays reachable in every `@ts`
    body of the module."""
    out = _emit(SOURCE)
    assert "export function Date_(): Kind {" in out
    assert "export function Boolean_(value: bigint): Kind {" in out
    # the shadowing declarations are gone, at either arity
    assert "export function Date(" not in out
    assert "export function Boolean(" not in out
    # ...and the global the module needs is still the global
    assert "new Date(Number(ms))" in out


def test_the_tag_string_and_the_match_label_stay_raw():
    """Only the binding is escaped: the tag is a string the union declares and
    the switch compares, so it keeps the case's own spelling."""
    out = _emit(SOURCE)
    assert '| { kind: "Date" }' in out
    assert '| { kind: "Boolean"; value: bigint }' in out
    assert 'return { kind: "Date" }' in out
    assert 'case "Date":' in out
    assert 'case "Boolean":' in out
    assert '"Date_"' not in out


def test_a_case_whose_name_is_not_a_global_is_untouched():
    """The escape is not a rename pass: an ordinary case name is the identity,
    so the corpus's byte-identical output cannot move."""
    m = _load()
    out = _emit(SOURCE)
    assert "export function Text(): Kind {" in out
    assert m._mangle("Text", m.JS_GLOBAL_RESERVED) == "Text"
    assert m._mangle("Date", m.JS_GLOBAL_RESERVED) == "Date_"
    # the keyword family and the global family are disjoint, so a name is
    # escaped for exactly one reason
    assert not (m.JS_RESERVED & m.JS_GLOBAL_RESERVED)
    assert m._mangle("Date") == "Date"  # reserved-words-only: a global is not one
