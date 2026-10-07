"""A wire record cannot forge the prototype of the record it decodes into.

WHAT WAS WRONG. `bridge.ts` built every decoded record with a bare assignment,
`for (const k of Object.keys(o)) rec[k] = decodeValue(o[k])`. For `k ===
"__proto__"` that is not a property write: it invokes the inherited accessor,
which REPLACES `rec`'s prototype with the attacker's object and defines no own
property at all. `JSON.parse` makes `__proto__` an own enumerable key, so a peer
that sends `{"__proto__": {"isAdmin": true, "toString": "pwned"}}` got a record
whose `isAdmin` is attacker-chosen and whose `toString` is a string, while
`Object.keys(rec)` stayed EMPTY — invisible to every structural check the value
passed afterwards. CodeQL alerts 94 (`decodeValue`) and 95 (`decodeAs`).

The `decodeAs` record path had a second, worse defect on the same line. It chose
the declared field type with `k in fields`, and `"__proto__" in fields` is TRUE
through `Object.prototype`, so `fields["__proto__"]` handed `substitute` the
prototype OBJECT where a type name belongs, and `typeHead` called `.trim()` on
it: a peer could crash the node runner with a remote `TypeError`. A DoS, not
just a confusion.

WHAT HAPPENS NOW. Every dynamic key in the codec is written with
`Object.defineProperty` (`ownSet`), which sets the own property the key names,
and the field table is consulted with `Object.hasOwn`. A `__proto__` key is
carried as an ordinary own data property — which is what the wire said — and the
decoded record's prototype is always `Object.prototype`.

These run the REAL `backends/typescript/bridge.ts`, imported by node directly.
That module's only runtime dependencies are `node:*` builtins and its sibling
`runtime.ts`/`estop.ts`, so this needs `node` and NOT `node_modules/cordis` —
unlike the placement tests, it does not skip on a bare checkout.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "backends" / "typescript" / "bridge.ts"

_REQUIRED = {t.strip() for t in os.environ.get("REVL_REQUIRE_TIERS", "").split(",") if t.strip()}


def _need_node() -> None:
    reason = None
    if shutil.which("node") is None:
        reason = "node is not on PATH"
    elif not BRIDGE.is_file():
        reason = f"{BRIDGE} is missing"
    if reason is None:
        return
    if "ts" in _REQUIRED:
        pytest.fail(f"REVL_REQUIRE_TIERS names ts, but {reason}")
    pytest.skip(reason)


#: The probe, run by node against the real module. `__MODULE__` is replaced with
#: the module's file URL. Every check is expressed as a value so python owns the
#: assertions; a thrown error is reported rather than swallowed, so the
#: `decodeAs` remote `TypeError` shows up as a failure and not as a skip.
_PROBE = r"""
const m = await import("__MODULE__");
const TYPES = { Pair: { kind: "record", fields: { a: "Int", b: "Str" } } };
const out = {};
const attempt = (name, fn) => { try { out[name] = { ok: true, value: fn() }; }
  catch (e) { out[name] = { ok: false, error: String(e && e.message || e) }; } };
const plain = (x) => Object.getPrototypeOf(x) === Object.prototype;

attempt("decodeValue", () => {
  const r = m.decodeValue(JSON.parse('{"__proto__": {"isAdmin": true, "toString": "pwned"}}'));
  return {
    protoIsPlain: plain(r),
    protoKeys: Object.keys(Object.getPrototypeOf(r)),
    isAdmin: r.isAdmin === undefined ? null : r.isAdmin,
    toStringIsFunction: typeof r.toString === "function",
    hasOwnProto: Object.hasOwn(r, "__proto__"),
    ownKeys: Object.keys(r),
  };
});

attempt("decodeAs", () => {
  const r = m.decodeAs(JSON.parse('{"__proto__": {"smuggled": "yes"}, "a": 1, "b": "x"}'),
    "Pair", TYPES);
  return {
    protoIsPlain: plain(r),
    smuggled: r.smuggled === undefined ? null : r.smuggled,
    a: typeof r.a === "bigint" ? String(r.a) : "not-a-bigint:" + typeof r.a,
    b: r.b,
  };
});

attempt("encodeValue", () => {
  const r = m.encodeValue(JSON.parse('{"__proto__": {"boom": 1}, "k": 2}'));
  return { protoIsPlain: plain(r), boom: r.boom === undefined ? null : r.boom };
});

attempt("scalarProto", () => {
  const r = m.decodeValue(JSON.parse('{"__proto__": "not-an-object", "z": 1}'));
  return { protoIsPlain: plain(r), z: r.z };
});

attempt("nested", () => {
  const r = m.decodeValue({ x: [1, 2], y: { z: "w" } });
  return { json: JSON.stringify(r) };
});

attempt("variant", () => {
  const r = m.decodeValue({ "$kind": "Ok", "$value": 7 });
  return { kind: r.kind, value: r.value };
});

attempt("roundTrip", () => {
  return { json: JSON.stringify(m.decodeValue(m.encodeValue({ a: 1, b: [2, 3] }))) };
});

process.stdout.write("__PROBE__" + JSON.stringify(out) + "\n");
"""


def _probe(tmp_path: Path) -> dict:
    script = tmp_path / "probe.mjs"
    script.write_text(_PROBE.replace("__MODULE__", BRIDGE.as_uri()), encoding="utf-8")
    proc = subprocess.run([shutil.which("node"), str(script)], capture_output=True,
                          text=True, timeout=120)
    for line in proc.stdout.splitlines():
        if line.startswith("__PROBE__"):
            return json.loads(line[len("__PROBE__"):])
    raise AssertionError(f"the probe produced no result\nstdout:\n{proc.stdout}\n"
                         f"stderr:\n{proc.stderr}")


@pytest.fixture(scope="module")
def probe(tmp_path_factory) -> dict:
    _need_node()
    return _probe(tmp_path_factory.mktemp("proto"))


def test_the_probe_ran(probe: dict) -> None:
    """Non-vacuity: every probe must have produced a value or an error, never
    nothing, so a rename cannot turn this file green by silence."""
    assert set(probe) == {"decodeValue", "decodeAs", "encodeValue", "scalarProto",
                          "nested", "variant", "roundTrip"}
    assert all(probe.values())


def test_decode_value_does_not_forge_the_prototype(probe: dict) -> None:
    """Alert 94: a peer's `__proto__` key must not become the record's
    prototype, and the forgery must not be reachable as an inherited field."""
    got = probe["decodeValue"]
    assert got["ok"], got
    v = got["value"]
    assert v["protoIsPlain"], f"the prototype was replaced: {v['protoKeys']}"
    assert v["isAdmin"] is None, "an inherited attacker field is reachable"
    assert v["toStringIsFunction"], "an inherited attacker field shadowed toString"
    assert v["hasOwnProto"], "the wire's own key was dropped instead of written"


def test_decode_as_record_does_not_forge_or_throw(probe: dict) -> None:
    """Alert 95. `k in fields` made `fields["__proto__"]` the prototype OBJECT,
    so `substitute` returned it and `typeHead` threw `type.trim is not a
    function`: a remote crash. The declared fields must still decode."""
    got = probe["decodeAs"]
    assert got["ok"], f"decodeAs threw: {got.get('error')}"
    v = got["value"]
    assert v["protoIsPlain"], "the prototype was replaced"
    assert v["smuggled"] is None, "an inherited attacker field is reachable"
    assert v["a"] == "1", f"a declared Int must still decode to a bigint: {v['a']}"
    assert v["b"] == "x"


def test_encode_value_does_not_forge_the_prototype(probe: dict) -> None:
    """The same assignment shape on the outbound path, for the same reason."""
    got = probe["encodeValue"]
    assert got["ok"], got
    assert got["value"]["protoIsPlain"]
    assert got["value"]["boom"] is None


def test_a_non_object_proto_value_is_still_an_own_key(probe: dict) -> None:
    """`{"__proto__": "x"}` is a legal wire record. It must decode, and the key
    must be an ordinary own property rather than silently dropped."""
    got = probe["scalarProto"]
    assert got["ok"], got
    assert got["value"]["protoIsPlain"]
    assert got["value"]["z"] == 1


def test_ordinary_records_still_decode(probe: dict) -> None:
    """No regression: the containers, the ADT codec and the round trip."""
    assert probe["nested"]["value"]["json"] == '{"x":[1,2],"y":{"z":"w"}}'
    assert probe["variant"]["value"] == {"kind": "Ok", "value": 7}
    assert probe["roundTrip"]["value"]["json"] == '{"a":1,"b":[2,3]}'
