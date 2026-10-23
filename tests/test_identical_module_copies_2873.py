"""Issue #2873: byte-identical modules in separate trees compose once."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_files  # noqa: E402


_JSON_MODULE = """pub extern pure fn json_parse(s: Str) -> Str
  = @py { return s }
"""


def _write(path: Path, contents: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    return path


def _two_tree_roots(tmp_path: Path, second_module: str) -> list[str]:
    product = tmp_path / "product"
    harness = tmp_path / "harness"
    _write(product / "stdlib" / "json.rvl", _JSON_MODULE)
    _write(harness / "stdlib" / "json.rvl", second_module)
    product_root = _write(
        product / "app.rvl",
        'use "stdlib/json.rvl" { json_parse }\n'
        "pub fn product_parse(s: Str) -> Str { return json_parse(s) }\n",
    )
    harness_root = _write(
        harness / "components" / "app.rvl",
        'use "../stdlib/json.rvl" { json_parse }\n'
        "pub fn harness_parse(s: Str) -> Str { return json_parse(s) }\n",
    )
    return [str(product_root), str(harness_root)]


def test_two_trees_can_compose_the_same_json_module_copy(tmp_path):
    ir = compile_files(_two_tree_roots(tmp_path, _JSON_MODULE))

    function_names = [fn["name"] for fn in ir["functions"]]
    extern_names = [extern["name"] for extern in ir["externs"]]
    assert {"product_parse", "harness_parse"} <= set(function_names)
    assert extern_names.count("json_parse") == 1


def test_two_trees_still_refuse_different_json_module_contents(tmp_path):
    different_module = _JSON_MODULE.replace("return s", 'return s + "!"')

    with pytest.raises(RevlError, match="duplicate extern"):
        compile_files(_two_tree_roots(tmp_path, different_module))
