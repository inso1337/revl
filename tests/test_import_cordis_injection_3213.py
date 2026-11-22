"""`revl import cordis`: issue 3213, codegen injection via a newline.

The importer copies the plugin's `name`, its `inject` entries and a few other
source-derived strings into `//` comments of the generated module. The
scanner's string-literal pattern accepts any character but a quote, so a
template literal with real newlines in it is accepted, the comment ends at the
first newline, and the rest lands as top-level revl source (an `@py` extern
whose host body runs on the first `revl run`). Every such value now goes
through `_comment_safe`, like the OpenAPI and A2A importers.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.import_cordis import import_cordis  # noqa: E402

_PAYLOAD = (
    "\nextern pure fn INJECTED() -> Int = @py { return 1 }\n"
    "component ZZZ { }\n// "
)

_TEMPLATE = """
export const name = '{name}'
export const inject = {inject}

export function apply(ctx: Context) {{
  ctx.provide('greeter')
  ctx.greeter = {{
    greet(name: string): string {{
      return `hello, ${{name}}`
    }},
  }}
}}
"""

_CASES = {
    "name": ("greeter" + _PAYLOAD, "['i18n']"),
    "inject": ("greeter", "['i18n" + _PAYLOAD + "']"),
    "inject-required": ("greeter",
                        "{ required: ['i18n" + _PAYLOAD + "'] }"),
    "inject-optional": ("greeter",
                        "{ optional: ['i18n" + _PAYLOAD + "'] }"),
}


def _compiled_names(out: str):
    ir = compile_source(out, "generated.rvl")
    externs = {e.get("name") for e in ir.get("externs") or []}
    components = {c.get("name") for c in ir.get("components") or []}
    return externs, components


@pytest.mark.parametrize("backend", ["py", "ts"])
@pytest.mark.parametrize("label", list(_CASES))
def test_plugin_strings_cannot_inject_source(label, backend):
    name, inject = _CASES[label]
    src = _TEMPLATE.format(name=name, inject=inject)
    out = import_cordis(src, filename="plugin.ts", backend=backend)
    externs, components = _compiled_names(out)
    assert "ZZZ" not in components, f"{label}: injected a top-level component"
    assert "INJECTED" not in externs, f"{label}: injected an extern"
    for line in out.splitlines():
        stripped = line.lstrip()
        assert not stripped.startswith("component ZZZ")
        assert not stripped.startswith("extern pure fn INJECTED")


def test_filename_cannot_inject_source():
    src = _TEMPLATE.format(name="greeter", inject="['i18n']")
    out = import_cordis(src, filename="plugin.ts" + _PAYLOAD, backend="py")
    externs, components = _compiled_names(out)
    assert "ZZZ" not in components
    assert "INJECTED" not in externs


def test_benign_plugin_header_is_unchanged():
    src = _TEMPLATE.format(name="greeter", inject="['i18n']")
    out = import_cordis(src, filename="plugin.ts", backend="py")
    assert "// Cordis plugin: greeter\n" in out
    assert "//   required: i18n\n" in out


_SERVICE = """class S extends Service {{
  constructor(ctx) {{ super(ctx, {key}) }}
  good(a: string): string {{ return '' }}
  {extra}
}}
"""

_SERVICE_CASES = {
    "super-key": dict(key="`g" + _PAYLOAD + "`", extra="",
                      mark_unrecovered=False),
    "unrecovered-type": dict(key="'g'",
                             extra="bad(a: Foo<'x" + _PAYLOAD + "'>): string "
                                   "{ return '' }",
                             mark_unrecovered=True),
}


@pytest.mark.parametrize("backend", ["py", "ts"])
@pytest.mark.parametrize("label", list(_SERVICE_CASES))
def test_service_strings_cannot_inject_source(label, backend):
    case = dict(_SERVICE_CASES[label])
    marked = case.pop("mark_unrecovered")
    src = _SERVICE.format(**case)
    out = import_cordis(src, filename="plugin.ts", backend=backend,
                        mark_unrecovered=marked)
    externs, components = _compiled_names(out)
    assert "ZZZ" not in components, f"{label}: injected a top-level component"
    assert "INJECTED" not in externs, f"{label}: injected an extern"
    for line in out.splitlines():
        stripped = line.lstrip()
        assert not stripped.startswith("component ZZZ")
        assert not stripped.startswith("extern pure fn INJECTED")


def test_provide_key_cannot_close_the_host_body():
    src = _SERVICE.format(key="`g}\ncomponent ZZZ { }`", extra="",
                          mark_unrecovered=False)
    out = import_cordis(src, filename="plugin.ts", backend="py")
    _, components = _compiled_names(out)
    assert "ZZZ" not in components
