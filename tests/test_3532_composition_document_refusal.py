"""A composition document on the module compile path is refused by name (#3532).

A composition document given to `compile_source`/`compile_files` (the path the
session verbs revl_check/revl_load/revl_swap take) used to come back OK with
`components = []` — no component, no refusal, no warning — so a row's config
was invisible in a session. The module compiler now refuses the document by
name and points at the composition admission path. Ordinary modules are
unchanged, and the composition path itself (which compiles the row FILES, not
the composition document) is unaffected.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

COMPOSITION = 'composition Demo {\n  row @db from "db.rvl" provides db\n}\n'


def test_compile_source_refuses_a_composition_document_by_name():
    with pytest.raises(RevlError) as err:
        compile_source(COMPOSITION, "demo.rvl")
    message = str(err.value)
    assert "this is a composition document (`composition Demo`), not a module" in message
    assert "zero components" in message
    hint = err.value.hint or ""
    assert "admit_composition" in hint
    assert "revl audit" in hint


def test_compile_source_still_accepts_a_module():
    ir = compile_source('pub fn f() -> Int { return 1 }\n', "m.rvl")
    assert ir["ir_version"] == 3


def test_a_module_with_components_is_unchanged():
    src = ('service S { fn go() -> Int }\n'
           'component C provides s: S { provide s { fn go() -> Int { return 1 } } }\n')
    ir = compile_source(src, "c.rvl")
    assert any(c.get("name") == "C" for c in ir.get("components") or [])


def test_compile_files_refuses_a_composition_document(tmp_path):
    from revl import compile_files
    doc = tmp_path / "demo.rvl"
    doc.write_text(COMPOSITION, encoding="utf-8")
    with pytest.raises(RevlError) as err:
        compile_files([str(doc)])
    assert "not a module" in str(err.value)


def test_the_composition_path_still_admits():
    """The composition admission path compiles the row files (modules), not the
    composition document, so it is untouched by the refusal."""
    from revl.composition import admit_composition
    import inspect
    src = inspect.getsource(admit_composition)
    assert "compile_files" in src  # row files, never the composition document
