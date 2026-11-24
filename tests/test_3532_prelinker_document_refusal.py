"""Issue #3532: a composition document given to the module compile path used to
return OK with zero components, so the session verbs checked/planned/swapped an
empty document. Now `compile_source`/`compile_files` refuse a pure
composition/layer document by name; a file that also declares module items
keeps the module path.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files, compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

COMPOSITION = (
    'composition Demo {\n  use "services.rvl"\n'
    '  row @db from "sqlite.rvl" provides db\n    config { url: "sqlite://local" }\n}\n')

MODULES = {
    "services.rvl": "service Db { fn get(k: Str) -> Str }\n",
    "sqlite.rvl": ("component Sqlite provides db: Db "
                   "{ provide db { fn get(k) = k } }\n"),
}


def test_a_composition_document_is_refused_by_name_on_the_module_path():
    with pytest.raises(RevlError) as excinfo:
        compile_source(COMPOSITION, "c.rvl", modules=MODULES)
    err = excinfo.value
    assert "this is a composition document (`composition Demo`), not a module" \
        in err.message
    assert "admit_composition" in err.hint


def test_a_composition_document_file_is_refused_by_compile_files(tmp_path):
    (tmp_path / "services.rvl").write_text(MODULES["services.rvl"])
    (tmp_path / "sqlite.rvl").write_text(MODULES["sqlite.rvl"])
    (tmp_path / "base.rvl").write_text(COMPOSITION)
    with pytest.raises(RevlError) as excinfo:
        compile_files([str(tmp_path / "base.rvl")])
    assert "composition document" in excinfo.value.message


def test_a_layer_document_is_refused_by_name_too():
    layer = ('layer Patch for Demo {\n'
             '  configure @db with { pool: 32 }\n}\n')
    with pytest.raises(RevlError) as excinfo:
        compile_source(layer, "l.rvl", modules={})
    assert "this is a layer document" in excinfo.value.message


def test_a_module_document_still_compiles(tmp_path):
    (tmp_path / "m.rvl").write_text(
        "service Db { fn get(k: Str) -> Str }\n"
        "component Sqlite provides db: Db { provide db { fn get(k) = k } }\n")
    ir = compile_files([str(tmp_path / "m.rvl")])
    assert [c["name"] for c in ir["components"]] == ["Sqlite"]


def test_a_mixed_file_keeps_the_module_path(tmp_path):
    """A file with real module decls AND a composition block is not the
    silent-empty case: the module compiler still admits its components."""
    (tmp_path / "m.rvl").write_text(
        "service Db { fn get(k: Str) -> Str }\n"
        "component Sqlite provides db: Db { provide db { fn get(k) = k } }\n"
        'composition Demo {\n  use "m.rvl"\n  row @db from "m.rvl" provides db\n}\n')
    ir = compile_files([str(tmp_path / "m.rvl")])
    assert [c["name"] for c in ir["components"]] == ["Sqlite"]
