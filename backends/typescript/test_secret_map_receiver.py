"""The ts tier's `Secret[Map[K, V]]` receiver (roadmap item 421 F6).

`runtime.rememberSecret` dispatched on `ArrayBuffer.isView`, then `Array.isArray`,
then `Object.values(value)`. A revl `Map` is this tier's built-in JS `Map` —
`emit.py` lowers `Map.new()` to `new Map()` and renders the type as
`Map<K, V>` — and a `Map`'s entries are not OWN enumerable properties, so
`Object.values(map)` is `[]` and the walk registered nothing at all.

The receiver marking is real, which is what makes the gap reachable: the emitter
writes `host.markSecret(m)` at the head of every provide method that declares a
`Secret[T]` parameter, so a `Secret[Map[Str, Str]]` parameter is an ordinary
author-reachable argument whose whole value set crossed every sink `redactText`
covers verbatim — `Map.get`, `JSON.stringify` and the `Map(n) { … }` rendering
alike. The py tier registers `dict.values()` and the java tier registers
`Map.values()` for the same rule, so this tier was the only one whose registry
could not reach a map's values.

These checks are toolchain-free: they pin the emitted SHAPE, so the runtime
assertions in `tests/secret_map.test.ts` (which need node) are testing a walk the
emitter actually hands a `Map`. Split the two because a `Map` rendered by the
emitter as something else would make the runtime test pass vacuously.

    .venv/bin/pytest backends/typescript/test_secret_map_receiver.py -q
"""

import importlib.util
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402


def _load_ts_emit():
    spec = importlib.util.spec_from_file_location(
        "revl_ts_emit_secretmap", BACKEND / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_TS_EMIT = _load_ts_emit()

# The seam-visible method keeps arity 1 (`_run_seam`-style probes hardcode one
# argument); the map-typed receiver door sits on a second method of the same
# service, which is what makes the marking reachable without an origin.
_SOURCE = (
    "service Cache {\n"
    "  emission fn put(u: Str) -> Str\n"
    "  emission fn store(token: Secret[Map[Str, Str]]) -> Str\n"
    "}\n"
    "component UserCache provides cache: Cache {\n"
    "  let store = effect Map.new() undo store.drop()\n"
    "\n"
    "  provide cache {\n"
    "    fn put(u) {\n"
    "      return 'ok'\n"
    "    }\n"
    "    fn store(token) {\n"
    "      effect store.insert('PUBLIC-KEY-421', 'PUBLIC-VALUE-421')\n"
    "      undo   store.remove('PUBLIC-KEY-421')\n"
    "      return 'ok'\n"
    "    }\n"
    "  }\n"
    "}\n"
)


def _emitted() -> str:
    return _TS_EMIT.emit(compile_source(_SOURCE, "secret_map_receiver.rvl"))


def test_a_map_typed_secret_parameter_still_renders_as_a_js_map():
    """The premise the runtime walk depends on: the marked value IS a `Map`."""
    out = _emitted()
    assert "token: Map<string, string>" in out


def test_the_receiver_is_marked_and_the_mark_is_handed_the_map():
    """`host.markSecret(token)` at the head of the declaring method, with no
    projection: the walk is what must descend, not the call site."""
    out = _emitted()
    assert "host.markSecret(token)" in out


def test_the_unmarked_method_gets_no_marking():
    """Non-vacuity: `put` declares no `Secret[T]`, so it must be untouched and
    the module stays byte-comparable for every document that declares none."""
    out = _emitted()
    assert out.count("host.markSecret(") == 1


def test_a_secretless_document_carries_no_marking():
    """The marking is emitted ONLY for a method that declares one."""
    src = (
        "service Cache {\n"
        "  emission fn put(u: Str) -> Str\n"
        "}\n"
        "component UserCache provides cache: Cache {\n"
        "  provide cache {\n"
        "    fn put(u) {\n"
        "      return 'ok'\n"
        "    }\n"
        "  }\n"
        "}\n"
    )
    out = _TS_EMIT.emit(compile_source(src, "secretless.rvl"))
    assert "host.markSecret(" not in out
