"""A record field of type `Value` must build on the Java tier (#3019).

`Value` (stdlib/value.rvl, docs/stdlib-value.md) is the NAMED erased-dynamic
builtin: the checker reserves it, accepts it in a record field, and every tier
erases it to that host's dynamic value representation (py `Any`, Go `any`).
`_java_v3_type` erased only the wildcard `Any`, so `Value` fell through to
`_ident` and a record emitted `java.util.List<Value>` / `Value v` with no class
of that name. javac rejects every use with `cannot find symbol` (issue #3019).

The mapping is asserted directly, and emission goes through
``javac_gate.compile_check``, which turns the assertion into a claim about a
program javac accepted (a no-op off CI, where the ``backend-java`` job
provisions the JDK).
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "tests") not in sys.path:
    sys.path.append(str(ROOT / "tests"))

from _load_by_path import load_by_path  # noqa: E402

javac_gate = load_by_path("javac_gate", HERE / "javac_gate.py")
java_emit = load_by_path("revl_java_emit_value3019", HERE / "emit.py")

from revl.compiler import compile_source  # noqa: E402

VALUE_RECORD = """\
pub type Box = { tags: List[Value], v: Value, m: Map[Str, Value] }
"""


def test_value_maps_to_object_in_every_position():
    """`Value` is the erased dynamic value; Java has no word for it but the
    universal supertype, which is what `Any` already lowers to."""
    assert java_emit._java_v3_type("Value") == "Object"
    assert java_emit._java_v3_type("List[Value]") == "java.util.List<Object>"
    assert java_emit._java_v3_type("Opt[Value]") == "java.util.Optional<Object>"
    assert java_emit._java_v3_type("Map[Str, Value]") == "java.util.Map<String, Object>"


def test_record_field_of_value_emits_object_fields():
    out = java_emit.emit(compile_source(VALUE_RECORD, "value_record.rvl"))
    assert "java.util.List<Object> tags" in out
    assert "Object v" in out
    assert "java.util.Map<String, Object> m" in out
    assert "<Value>" not in out
    assert " Value v" not in out


def test_record_field_of_value_is_accepted_by_javac():
    out = java_emit.emit(compile_source(VALUE_RECORD, "value_record.rvl"))
    javac_gate.compile_check(out, "record field of type Value (#3019)")
