"""`revl import openapi`: item 416f, codegen injection via document metadata.

The OpenAPI surface was on record in item 416f as UNATTEMPTED. Attacked here.

The defect: `_Generator.header`, `_host_comment` and the notes render
interpolate document-derived strings — `info.title`, `info.version`,
`openapi`, the source filename, resolved server URLs, `$ref`/enum note text —
verbatim into `//` comments and into `@<backend> { ... }` stub bodies of the
generated `.rvl`. A `//` comment runs to the next newline (`lexer.py:351`), so
a newline embedded in any of those values ended the comment and dropped
whatever followed onto its own line of COMPILED SOURCE.

Executed before the fix: a document supplying
`info.title = "W\\nextern pure fn INJECTED() -> Int = @py { ... }\\ncomponent
ZZZ { }\\n// "` (and the same through `info.version`, `openapi` and
`servers[].url`) produced a generated `.rvl` that COMPILED with a top-level
`component ZZZ` and an `@py` extern whose host body ran arbitrary code on the
importing developer's first `revl run`. This is a supply-chain RCE: the
ordinary use of the importer is to project a vendor's PUBLISHED spec, which is
untrusted input.

The fix (`_comment_safe`) forces every document-derived string that reaches a
comment to a single line (CR/LF/U+2028/U+2029/U+0085 collapse to a space), so a
newline can no longer terminate the comment. It narrows comment text only and
does not touch the typed boundary the importer generates.
"""

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_source  # noqa: E402
from revl.import_openapi import _comment_safe, import_openapi  # noqa: E402

# a payload that, once a `//` comment is broken, is legal top-level revl: an
# `@py` extern whose body would run arbitrary code, plus a bare component so a
# successful injection is visible in the compiled IR as a NEW component.
_PAYLOAD = (
    "\nextern pure fn INJECTED() -> Int = @py { return 1 }\n"
    "component ZZZ { }\n// "
)

_STR = {"type": "string"}
_OK = {"200": {"description": "ok",
               "content": {"application/json": {"schema": _STR}}}}
_BASE = {
    "openapi": "3.0.3",
    "info": {"title": "Weather", "version": "1.0.0"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {"/ping": {"get": {"operationId": "ping", "responses": _OK}}},
}


def _compiled_names(out: str):
    ir = compile_source(out, "generated.rvl")
    externs = {e.get("name") for e in ir.get("externs") or []}
    components = {c.get("name") for c in ir.get("components") or []}
    return externs, components


def _mutations():
    def title(d):
        d["info"]["title"] = "W" + _PAYLOAD
    def version(d):
        d["info"]["version"] = "1.0" + _PAYLOAD
    def openapi(d):
        d["openapi"] = "3.0.3" + _PAYLOAD
    def server(d):
        d["servers"] = [{"url": "https://a.example.com" + _PAYLOAD}]
    return {"info.title": title, "info.version": version,
            "openapi": openapi, "servers[].url": server}


@pytest.mark.parametrize("label", list(_mutations()))
def test_document_metadata_cannot_inject_source(label):
    doc = copy.deepcopy(_BASE)
    _mutations()[label](doc)
    out = import_openapi(doc, backend="py")
    # the generated file compiles (the importer still produces valid revl)...
    externs, components = _compiled_names(out)
    # ...and the payload reached NO top-level declaration: no injected component
    # and no injected extern named INJECTED.
    assert "ZZZ" not in components, f"{label}: injected a top-level component"
    assert "INJECTED" not in externs, f"{label}: injected an extern"
    # the raw payload never appears as the START of a source line (a `//` line
    # or an `@py {` body is fine; a bare `component`/`extern` at column 0 is not)
    for line in out.splitlines():
        stripped = line.lstrip()
        assert not stripped.startswith("component ZZZ")
        assert not stripped.startswith("extern pure fn INJECTED")


def test_filename_cannot_inject_source():
    doc = copy.deepcopy(_BASE)
    out = import_openapi(doc, filename="spec.json" + _PAYLOAD, backend="py")
    externs, components = _compiled_names(out)
    assert "ZZZ" not in components
    assert "INJECTED" not in externs


def test_comment_safe_collapses_every_line_terminator():
    for sep in ("\n", "\r", "\r\n", " ", " ", "\x85"):
        got = _comment_safe(f"a{sep}b")
        assert "\n" not in got and "\r" not in got
        assert " " not in got and " " not in got and "\x85" not in got
        assert got == "a b"


def test_benign_document_is_unchanged_and_compiles():
    out = import_openapi(copy.deepcopy(_BASE), backend="py")
    externs, components = _compiled_names(out)
    assert externs == {"http_weather_ping"}
    assert components == {"WeatherProvider"}
    assert "// API: Weather 1.0.0" in out
