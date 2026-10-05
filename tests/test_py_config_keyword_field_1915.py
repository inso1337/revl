"""A component config field named after a Python keyword (issue #1915).

`config { from: Str }` is legal revl, and `from` is a dict KEY everywhere it is
spelled: the body reads `_revl_config['from']`, the plug step
`load Hello with { from: "a" }` supplies `from`, and `runtime.py::ConfigSchema`
resolves the plug's mapping against the schema it was handed. The emitted
schema was the ONE site that ran the name through `_ident`, the injective
append-`_` rename that lifts a name out of a Python keyword, and rendered the
tuple `('from_', 'Str', None)`. The plug supplies `from`, the schema demands
`from_`, so the fiber landed FAILED on `missing required config field "from_"`:
loaded, and never ACTIVE.

A schema tuple's first element is a dict key rather than a binding, so the raw
name is the only correct spelling there — the `secret=[…]` list beside it
already spelled it raw. Asserted at both levels: the emitted text, and the
resolution the runtime actually performs.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# backends/python stays on sys.path for the EXEC'D module (`from runtime
# import …`) — emit itself is loaded by the shared unique-name loader below
sys.path.insert(0, str(ROOT / "backends" / "python"))

from _backend_import import backend_emitter  # noqa: E402
from revl import compile_source  # noqa: E402

# unique-name load: bare `import emit` binds the canonical name and collides
# with any other backend suite in the same process (tests/_backend_import.py)
emit = backend_emitter("python")

SOURCE = (
    "service Greeter {\n"
    "  fn hello() -> Str\n"
    "}\n"
    "\n"
    "component Hello provides greeter: Greeter {\n"
    "  config { from: Str }\n"
    "  provide greeter {\n"
    "    fn hello() { return `hello from ${config.from}` }\n"
    "  }\n"
    "}\n"
)


def _emit_and_exec():
    code = emit.emit(compile_source(SOURCE, "t.rvl"))
    namespace = {}
    exec(compile(code, "<emitted>", "exec"), namespace)
    return code, namespace


def test_config_key_is_the_raw_name_in_the_schema_and_the_body_read():
    code, _ = _emit_and_exec()
    assert "    ('from', 'Str', None),\n" in code
    assert "_revl_config['from']" in code
    # the keyword rename belongs to BINDINGS; a dict key must not take it
    assert "from_" not in code


def test_the_plug_supplying_the_raw_field_name_resolves():
    # the exact resolution cordis-py performs on `load Hello with { from: … }`
    # (backends/python/runtime.py::ConfigSchema.validate); a non-empty
    # `issues` is the FAILED fiber of the issue
    _, namespace = _emit_and_exec()
    schema = namespace["Hello"]["Config"]
    assert schema.fields == [("from", "Str", None)]
    supplied = {"from": "a"}
    result = schema.validate(supplied)
    assert not result.issues, list(result.issues)
    assert result.value == supplied
