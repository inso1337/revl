"""The ts event schema check counts only OWN keys, like its python mirror (#3210).

WHAT WAS WRONG. `_jsonSchemaError` in `backends/typescript/runtime.ts` tested
`key in props` and `name in obj`, which walk the prototype chain. An item key
named like an `Object.prototype` member (`constructor`, `__proto__`,
`hasOwnProperty`, ...) was therefore "declared" by a schema that never named it:
the key was validated against the inherited function (which the walker treats as
"no schema") and passed `additionalProperties: false`, which the python
`_json_schema_error` refuses. In the other direction a `required` property with
such a name was satisfied by inheritance, so an item missing it was admitted.

WHAT HAPPENS NOW. Every membership test is `Object.hasOwn`, the same semantics
as python's dict membership. These run the REAL `EventContract.admit` from
`backends/typescript/runtime.ts` under node, with items built by `JSON.parse` (so
`__proto__` is an own key, as it is for a peer's payload), and compare with the
python mirror on the same schema and items.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from _load_by_path import load_by_path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "backends" / "typescript" / "runtime.ts"

_REQUIRED = {t.strip() for t in os.environ.get("REVL_REQUIRE_TIERS", "").split(",") if t.strip()}

SCHEMA = {"type": "object",
          "properties": {"id": {"type": "string"}, "amount": {"type": "integer"}},
          "required": ["id", "amount"], "additionalProperties": False}
SCHEMA_TOSTRING = {"type": "object",
                   "properties": {"id": {"type": "string"}, "toString": {"type": "string"}},
                   "required": ["id", "toString"], "additionalProperties": False}
SCHEMA_PROTO = {"type": "object",
                "properties": {"id": {"type": "string"}, "__proto__": {"type": "string"}},
                "required": ["id", "__proto__"], "additionalProperties": False}

SCHEMA_NESTED = {"type": "object",
                 "properties": {"inner": SCHEMA}, "required": ["inner"],
                 "additionalProperties": False}
SCHEMA_OPEN = {"type": "object", "properties": {"id": {"type": "string"}},
               "required": ["id", "constructor"]}
SCHEMA_EXTRA_SCHEMA = {"type": "object", "properties": {"id": {"type": "string"}},
                       "required": ["id"], "additionalProperties": {"type": "integer"}}
SCHEMA_CONST_ENUM = {"type": "object",
                     "properties": {"k": {"const": "a"}, "e": {"enum": ["x", "y"]}},
                     "required": ["k", "e"], "additionalProperties": False}

#: name -> (schema, raw JSON text of the item, admitted)
CASES = {
    "valid": (SCHEMA, '{"id": "a", "amount": 1}', True),
    "extra_plain": (SCHEMA, '{"id": "a", "amount": 1, "evil": 1}', False),
    "extra_constructor": (SCHEMA, '{"id": "a", "amount": 1, "constructor": {}}', False),
    "extra___proto__": (SCHEMA, '{"id": "a", "amount": 1, "__proto__": {"x": 1}}', False),
    "extra_hasOwnProperty": (SCHEMA, '{"id": "a", "amount": 1, "hasOwnProperty": "s"}', False),
    "extra_toString": (SCHEMA, '{"id": "a", "amount": 1, "toString": "s"}', False),
    "extra_valueOf": (SCHEMA, '{"id": "a", "amount": 1, "valueOf": 1}', False),
    "required_toString_missing": (SCHEMA_TOSTRING, '{"id": "a"}', False),
    "required_toString_present": (SCHEMA_TOSTRING, '{"id": "a", "toString": "s"}', True),
    "required_toString_wrong_type": (SCHEMA_TOSTRING, '{"id": "a", "toString": 1}', False),
    "required___proto___missing": (SCHEMA_PROTO, '{"id": "a"}', False),
    "required___proto___present": (SCHEMA_PROTO, '{"id": "a", "__proto__": "s"}', True),
    "required___proto___wrong_type": (SCHEMA_PROTO, '{"id": "a", "__proto__": 1}', False),
    "nested_extra_constructor": (
        SCHEMA_NESTED, '{"inner": {"id": "a", "amount": 1, "constructor": 1}}', False),
    "nested_valid": (SCHEMA_NESTED, '{"inner": {"id": "a", "amount": 1}}', True),
    "open_required_constructor_missing": (SCHEMA_OPEN, '{"id": "a"}', False),
    "open_required_constructor_present": (SCHEMA_OPEN, '{"id": "a", "constructor": 1}', True),
    "extra_schema_accepts_integer_extra": (
        SCHEMA_EXTRA_SCHEMA, '{"id": "a", "other": 1}', True),
    "extra_schema_checks_inherited_name": (
        SCHEMA_EXTRA_SCHEMA, '{"id": "a", "constructor": "s"}', False),
    "const_enum_valid": (SCHEMA_CONST_ENUM, '{"k": "a", "e": "x"}', True),
    "const_enum_bad_const": (SCHEMA_CONST_ENUM, '{"k": "b", "e": "x"}', False),
    "const_enum_bad_enum": (SCHEMA_CONST_ENUM, '{"k": "a", "e": "z"}', False),
}

#: the reason each refused case must give, so a wrong rejection cannot pass
REASON = "unexpected property"
REASONS = {
    "required_toString_missing": "missing required property",
    "required___proto___missing": "missing required property",
    "open_required_constructor_missing": "missing required property",
    "required_toString_wrong_type": "expected type",
    "required___proto___wrong_type": "expected type",
    "extra_schema_checks_inherited_name": "expected type",
    "const_enum_bad_const": "expected const",
    "const_enum_bad_enum": "is not one of",
}


def _need_node() -> None:
    reason = None
    if shutil.which("node") is None:
        reason = "node is not on PATH"
    elif not RUNTIME.is_file():
        reason = f"{RUNTIME} is missing"
    if reason is None:
        return
    if "ts" in _REQUIRED:
        pytest.fail(f"REVL_REQUIRE_TIERS names ts, but {reason}")
    pytest.skip(reason)


_PROBE = r"""
const m = await import("__MODULE__");
const cases = JSON.parse(process.argv[2]);
const out = {};
for (const [name, [schema, raw]] of Object.entries(cases)) {
  const ec = new m.EventContract("E0", schema, "id", 8);
  try { out[name] = { admitted: ec.admit(JSON.parse(raw)) }; }
  catch (e) { out[name] = { admitted: false, error: String(e && e.message || e) }; }
}
process.stdout.write("__PROBE__" + JSON.stringify(out) + "\n");
"""


@pytest.fixture(scope="module")
def ts_results(tmp_path_factory) -> dict:
    _need_node()
    tmp = tmp_path_factory.mktemp("own_keys")
    script = tmp / "probe.mjs"
    script.write_text(_PROBE.replace("__MODULE__", RUNTIME.as_uri()), encoding="utf-8")
    cases = {n: [s, raw] for n, (s, raw, _) in CASES.items()}
    proc = subprocess.run([shutil.which("node"), str(script), json.dumps(cases)],
                          capture_output=True, text=True, timeout=120)
    for line in proc.stdout.splitlines():
        if line.startswith("__PROBE__"):
            return json.loads(line[len("__PROBE__"):])
    raise AssertionError(f"the probe produced no result\nstdout:\n{proc.stdout}\n"
                         f"stderr:\n{proc.stderr}")


@pytest.mark.parametrize("name", sorted(CASES))
def test_ts_admits_exactly_what_the_schema_declares(ts_results, name):
    _schema, _raw, expected = CASES[name]
    got = ts_results[name]
    assert got["admitted"] is expected, (name, got)
    if not expected:
        assert "failed its schema" in got["error"], (name, got)
        assert REASONS.get(name, REASON) in got["error"], (name, got)


@pytest.mark.parametrize("name", sorted(CASES))
def test_python_mirror_agrees_with_the_ts_verdict(ts_results, name):
    py_runtime = load_by_path("py_runtime_3210", ROOT / "backends" / "python" / "runtime.py")

    schema, raw, expected = CASES[name]
    err = py_runtime._json_schema_error(json.loads(raw), schema)
    assert (err is None) is expected, (name, err)
    assert (err is None) is ts_results[name]["admitted"], (name, err, ts_results[name])
