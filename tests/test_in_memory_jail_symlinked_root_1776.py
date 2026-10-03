"""An in-memory compile under a symlinked root resolves its assets and refs
(issue #1776).

The in-memory arms built their lookup key lexically, as the `sources` map is
keyed, but checked it against roots resolved through `realpath`. Under a
symlinked root, such as macOS's `/var`, which is `/private/var`, every asset and
`@py ref` was refused as "outside the root compile tree". The jail of a compile
that reads nothing from disk is lexical, as `hostfile.read_body_file_memory`'s
already is; these pin that, and that an escape is still refused.
"""

import hashlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_files, compile_source  # noqa: E402
from revl.errors import RevlError  # noqa: E402

ASSET = ('type Asset = {{ path: Str, sha256: Str }}\n'
         'pub fn entry() -> Asset {{ return asset "{written}" }}\n')
REF = 'pub extern pure fn f() -> Int = @py ref f from "{written}"\n'
HOST = "def f():\n    return 1\n"


@pytest.fixture
def linked(tmp_path):
    """`<tmp>/link` -> `<tmp>/real`; the composition is named through the link."""
    real = tmp_path / "real"
    (real / "frontend").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    assert os.path.realpath(link) != str(link)
    return link


def _handle(ir: dict) -> dict:
    decl = next(f for f in ir["functions"] if f["name"] == "entry")
    return {name: node["value"] for name, node in decl["body"][0]["expr"]["fields"]}


def test_an_asset_under_a_symlinked_root_resolves_in_memory(linked):
    app = str(linked / "a.rvl")
    source = ASSET.format(written="./frontend/e.ts")
    ir = compile_source(source, app, modules={
        app: source, str(linked / "frontend" / "e.ts"): "export default 1\n"})
    assert _handle(ir) == {"path": "frontend/e.ts",
                           "sha256": hashlib.sha256(b"export default 1\n").hexdigest()}


def test_it_resolves_to_the_handle_the_disk_compile_gives(linked):
    app = linked / "a.rvl"
    source = ASSET.format(written="./frontend/e.ts")
    app.write_text(source, encoding="utf-8")
    (linked / "frontend" / "e.ts").write_text("export default 1\n", encoding="utf-8")
    on_disk = _handle(compile_files([str(app)]))
    in_memory = _handle(compile_source(source, str(app), modules={
        str(app): source, str(linked / "frontend" / "e.ts"): "export default 1\n"}))
    assert in_memory == on_disk


def test_a_ref_under_a_symlinked_root_resolves_in_memory(linked):
    app = str(linked / "a.rvl")
    source = REF.format(written="h.py")
    ir = compile_source(source, app, modules={app: source, str(linked / "h.py"): HOST})
    (ext,) = [e for e in ir["externs"] if e["name"] == "f"]
    assert ext["refs"]["py"]["path"] == "h.py"
    assert ext["refs"]["py"]["sha256"] == hashlib.sha256(HOST.encode()).hexdigest()


@pytest.mark.parametrize("template,written,name", [
    (ASSET, "../outside.ts", "outside.ts"),
    (REF, "../outside.py", "outside.py"),
])
def test_an_escape_from_a_symlinked_root_is_still_refused(linked, template, written, name):
    app = str(linked / "a.rvl")
    source = template.format(written=written)
    with pytest.raises(RevlError, match="outside the root compile tree"):
        compile_source(source, app, modules={app: source,
                                             str(linked.parent / name): HOST})
