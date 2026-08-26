"""Roadmap item 299 — audit rust/go/java/wasm for the item-279 dynamic-value
reserved-word field-access divergence.

Item 279 fixed the TS tier, where a reserved-word key on a `json_parse` / `Any`
(dynamic) value was renamed on ACCESS (`tc.function` -> `tc.function_`) but the
runtime JSON object still held the raw key, so the read was `undefined` while the
py tier read the raw key and worked — the same admitted program diverging
between tiers, SILENTLY (a wrong value, not a crash). The fix was scoped to the
TS emitter; this file is the follow-up audit of the other statically-typed
tiers.

FINDING — none of rust / go / java / wasm reproduce the 279 *silent* class.
The reason is structural, not a rename that this file has to undo:

  * `Any` erases to a type with no arbitrary members — Go `any`, Java `Object`,
    Rust `cordis::Value` — and none of these tiers carries a dynamic key reader
    (the py tier's `_revl_field(v, name)` / the TS tier's `obj["key"]`). A
    field access on a dynamic value therefore emits a STATIC field selection
    (`tc.<name>`), which the target compiler REJECTS (`type any has no field`,
    `cannot find symbol`, `no field on type Value`). wasm has no dynamic value
    at all and REFUSES `json_parse` at emit.
  * So the reserved-word sanitizer does fire on the access (Go/Java/Rust mangle
    a target-reserved key to `<name>_`), but it is MOOT: the access never
    reaches a runtime key/value bag whose raw key could be "missed". The
    divergence, where it is reachable, is a LOUD build error / emit refusal —
    never a silent wrong value. The 279 silent read was unique to JS, where
    `obj.function` / `obj["function"]` is a live dynamic lookup on any object.

Because nothing reproduces the silent class, item 299 needs no lower/typecheck
annotation and no emitter change — the shared "attach the receiver's static type
so a dynamic access uses the raw key" fix would be a no-op that only churns
byte-identity. This test LOCKS the finding: it proves py reads the raw key, that
the mangle-firing tiers emit a static selection (so no silent read is possible),
and — where a `go` toolchain is present — that the go tier fails LOUDLY rather
than passing with a wrong value.

    pytest tests/test_dynamic_reserved_key_cross_tier.py -q
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

# A two-line distillation of the item-279 lighthouse finding: read a reserved-word
# key off a dynamic (`json_parse` / `Any`) value. `{w}` is a word that is BOTH a
# valid revl identifier AND a reserved word in the tier under test, so the tier's
# sanitizer actually fires on the access.
_SRC = """
pub extern pure fn jp(s: Str) -> Any
  = @py {{ import json; return json.loads(s) }}
  = @ts {{ return JSON.parse(s) }}
  = @go {{ return nil }}
  = @java {{ return null; }}
  = @rs {{ cordis::Value::Null }}

pub fn read_key(s: Str) -> Str {{
  let tc: Any = jp(s)
  return tc.{w}.name
}}

test "dyn_reserved_key" {{
  assert read_key("{{\\"{w}\\": {{\\"name\\": \\"get_weather\\"}}}}") == "get_weather"
}}
"""


def _ir(word: str) -> dict:
    return compile_source(_SRC.format(w=word))


def _emit(backend: str, ir: dict):
    spec = importlib.util.spec_from_file_location(
        f"emit_{backend}_dyn299", ROOT / "backends" / backend / "emit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = module.emit(ir)
    if isinstance(out, dict):  # wasm returns {path: text}
        return "\n".join(out.values())
    return out


# --------------------------------------------------------------- py reference
#
# The py tier is the ground truth: it reads the raw dynamic key and works.

def test_py_reads_the_raw_reserved_key_unmangled():
    """py lowers a dynamic field access to `_revl_field(v, name)` — a dict/attr
    read by the RAW key string, never the append-`_` mangle. This is the
    reference every other tier is measured against."""
    out = _emit("python", _ir("range"))
    assert "_revl_field(_revl_field(tc, 'range'), 'name')" in out
    # the reserved-word mangle must NOT touch a dynamic-value read
    assert "'range_'" not in out


# ------------------------------------------ statically-typed tiers: no silent read
#
# Each of these mangles a target-reserved key on the ACCESS, but the access is a
# STATIC field selection on a type with no such member, so the outcome is a build
# error — never the 279 silent wrong value. We pin the emitted static selection
# and the ABSENCE of any raw-key dynamic reader, which is what makes a silent read
# structurally impossible.

@pytest.mark.parametrize("backend,word,expected", [
    ("go", "range", "tc.range_.name"),      # `range` is a Go keyword
    ("java", "class", "tc.class_.name"),    # `class` is a Java keyword
    ("rust", "move", "tc.move_.name"),      # `move` is a Rust keyword
])
def test_dynamic_access_emits_a_static_selection_not_a_raw_key_read(backend, word, expected):
    """The sanitizer fires (the key is mangled `<word>_`), but the access is a
    static field selection — there is no `_revl_field`-style raw-key bag read on
    these tiers, so no runtime key can be silently missed. The selection targets
    `Any` (Go `any` / Java `Object` / Rust `cordis::Value`), which has no such
    member, so the compiler rejects it LOUDLY (proven end-to-end for go below)."""
    out = _emit(backend, _ir(word))
    assert expected in out
    # no dynamic key/value reader exists on this tier — the thing that would be
    # needed for a 279-style silent read
    assert "_revl_field" not in out
    # and the access is NOT a quoted-key bag lookup (the TS `obj["key"]` shape)
    assert f'["{word}"]' not in out


def test_wasm_refuses_the_dynamic_value_at_emit():
    """wasm carries no dynamic `Any` value at all: it refuses `json_parse` at
    emit, so the 279 class cannot arise — a loud refusal, not a silent read."""
    with pytest.raises(Exception) as exc:
        _emit("wasm", _ir("range"))
    assert "jp" in str(exc.value) or "lowerable" in str(exc.value)


# ------------------------------------------------- end-to-end loud-failure proof
#
# The strongest form of the finding: run the go tier for real and assert it does
# NOT silently pass with a wrong value — it fails to build. Gated on a `go`
# toolchain (skips with a reason where absent), so it never spuriously reds.

@pytest.mark.skipif(shutil.which("go") is None, reason="needs a go toolchain")
def test_go_tier_fails_loudly_never_a_silent_wrong_value():
    """The 279 invariant on go: a reserved-word key on a dynamic value is a LOUD
    build error, never a silent pass. `go` compiles `tc.range_` against `any`
    (`type any has no field ...`), so the tier reports `fail`, not `pass`."""
    from revl.test import RUNNERS  # noqa: E402
    outcome, message = RUNNERS["go"](_ir("range"))
    # the invariant: a loud failure, never a silent pass with a wrong value. The
    # go runner surfaces the compiler's `tc.range_ undefined (type any has no
    # field ...)` to stdout and returns a `fail` verdict.
    assert outcome == "fail", (
        "go must fail loudly on a dynamic reserved-word read — it has no "
        f"dynamic key reader; got {outcome!r}: {message!r}")
