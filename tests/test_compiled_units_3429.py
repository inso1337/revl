"""Issue #3429: per-file compiled units kept between compiles, opt-in.

`compile_files(..., cache_dir=d)` keeps the parse of each file in `d`. These
tests hold the slice to its contract: the document and every refusal are the
ones a compile without the cache gives; editing one file rebuilds that file's
unit and nothing else; an entry that is unreadable, truncated, of another
version or names a class outside the parser's own is rebuilt; and with no
cache directory nothing is written.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl import unit_cache  # noqa: E402
from revl.errors import RevlError  # noqa: E402

# The examples, the stdlib and the top-level fixtures: documents and refusals
# both (a one-off run over every `.rvl` in the tree is in the pull request).
CORPUS = sorted([*(ROOT / "examples").glob("*.rvl"),
                 *(ROOT / "stdlib").glob("*.rvl"),
                 *(ROOT / "tests" / "fixtures").glob("*.rvl")])


def _outcome(paths, **kwargs):
    """The compiled document as JSON text, or the refusal's fingerprint: its
    rendering and the code, file, line and message of every error in it."""
    try:
        return "document", json.dumps(compile_files(paths, **kwargs))
    except RevlError as error:
        errors = getattr(error, "errors", [error])
        return "refusal", (str(error), [
            (one.code, one.filename, one.line, one.message, one.hint)
            for one in errors])


def _write(directory: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (directory / name).write_text(text, encoding="utf-8")


LIBRARY = {
    "leaf.rvl": "pub fn double(n: Int) -> Int {\n  return n * 2\n}\n",
    "mid.rvl": ('use "./leaf.rvl" { double }\n\n'
                "pub fn quad(n: Int) -> Int {\n  return double(double(n))\n}\n"),
    "main.rvl": ('use "./mid.rvl" { quad }\n\n'
                 "fn sixteen() -> Int {\n  return quad(4)\n}\n"),
}


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """Three files, main -> mid -> leaf, compiled from their own directory."""
    source = tmp_path / "src"
    source.mkdir()
    _write(source, LIBRARY)
    monkeypatch.chdir(source)
    return source


def _compile(tree: Path, cache: Path):
    report: dict = {}
    document = compile_files([str(tree / "main.rvl")], cache_dir=str(cache),
                             report=report)
    names = lambda paths: sorted(Path(p).name for p in paths)  # noqa: E731
    return (document, names(report["unit_cache"]["built"]),
            names(report["unit_cache"]["reused"]))


@pytest.mark.parametrize("example", CORPUS,
                         ids=lambda p: f"{p.parent.name}/{p.name}")
def test_cold_equals_cached_on_the_corpus(example, tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    cold = _outcome([str(example)])
    building = _outcome([str(example)], cache_dir=str(tmp_path / "units"))
    reading = _outcome([str(example)], cache_dir=str(tmp_path / "units"))
    assert building == cold
    assert reading == cold


REFUSALS = {
    "parse": "fn broken( -> Int {\n  return 1\n}\n",
    "type": "fn wrong() -> Int {\n  return \"text\"\n}\n",
    "missing import": 'use "./absent.rvl" { f }\n\nfn g() -> Int {\n  return 1\n}\n',
    "unknown name": "fn h() -> Int {\n  return nowhere(1)\n}\n",
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_a_refusal_keeps_its_code_and_message(case, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bad.rvl").write_text(REFUSALS[case], encoding="utf-8")
    cold = _outcome(["bad.rvl"])
    assert cold[0] == "refusal"
    cache = str(tmp_path / "units")
    assert _outcome(["bad.rvl"], cache_dir=cache) == cold
    assert _outcome(["bad.rvl"], cache_dir=cache) == cold


def test_a_refusal_in_an_importer_survives_a_kept_import(tree, tmp_path):
    """A kept unit of a good file never turns its importer's refusal into an
    acceptance: the importer still refuses, with the cold message."""
    cache = tmp_path / "units"
    _compile(tree, cache)
    (tree / "main.rvl").write_text(
        'use "./mid.rvl" { quad }\n\nfn sixteen() -> Int {\n  return quad("4")\n}\n',
        encoding="utf-8")
    cold = _outcome([str(tree / "main.rvl")])
    assert cold[0] == "refusal"
    assert _outcome([str(tree / "main.rvl")], cache_dir=str(cache)) == cold


def test_editing_one_file_rebuilds_that_file_only(tree, tmp_path):
    cache = tmp_path / "units"
    first, built, reused = _compile(tree, cache)
    assert (built, reused) == (["leaf.rvl", "main.rvl", "mid.rvl"], [])
    again, built, reused = _compile(tree, cache)
    assert (built, reused) == ([], ["leaf.rvl", "main.rvl", "mid.rvl"])
    assert again == first
    # a parse unit reads its own text only, so an edit to the widely imported
    # leaf rebuilds the leaf and its importers keep their units
    (tree / "leaf.rvl").write_text(
        "pub fn double(n: Int) -> Int {\n  return n + n\n}\n", encoding="utf-8")
    edited, built, reused = _compile(tree, cache)
    assert (built, reused) == (["leaf.rvl"], ["main.rvl", "mid.rvl"])
    assert edited == compile_files([str(tree / "main.rvl")])
    assert edited != first


def test_moving_the_working_directory_rebuilds(tree, tmp_path, monkeypatch):
    """Provenance is the path relative to the invocation directory, so it is
    part of the key: the same file compiled from elsewhere is a new unit."""
    cache = tmp_path / "units"
    _compile(tree, cache)
    monkeypatch.chdir(tmp_path)
    document, built, _reused = _compile(tree, cache)
    assert built == ["leaf.rvl", "main.rvl", "mid.rvl"]
    assert document == compile_files([str(tree / "main.rvl")])


def _entries(cache: Path) -> list[Path]:
    return sorted(cache.glob("parse-*.json"))


def _damage_garbage(entry: Path) -> None:
    entry.write_bytes(b"\x00\xffnot json")


def _damage_truncated(entry: Path) -> None:
    blob = entry.read_bytes()
    entry.write_bytes(blob[: len(blob) // 2])


def _damage_wrong_version(entry: Path) -> None:
    data = json.loads(entry.read_text(encoding="utf-8"))
    data["key"]["compiler"] = "0" * 64
    entry.write_text(json.dumps(data), encoding="utf-8")


def _damage_foreign_class(entry: Path) -> None:
    """A node naming a class that is not one of the parser's dataclasses: the
    decoder has nothing to construct it from and must not look further."""
    text = entry.read_text(encoding="utf-8")
    assert '{"n":["Program",' in text
    entry.write_text(text.replace('{"n":["Program",', '{"n":["Popen",', 1),
                     encoding="utf-8")


def _damage_dropped_field(entry: Path) -> None:
    data = json.loads(entry.read_text(encoding="utf-8"))
    del data["program"]["n"][1]["fn_decls"]
    entry.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("damage", [
    _damage_garbage, _damage_truncated, _damage_wrong_version,
    _damage_foreign_class, _damage_dropped_field],
    ids=lambda f: f.__name__.removeprefix("_damage_"))
def test_a_damaged_entry_is_rebuilt(damage, tree, tmp_path):
    cache = tmp_path / "units"
    first, _built, _reused = _compile(tree, cache)
    for entry in _entries(cache):
        damage(entry)
    document, built, reused = _compile(tree, cache)
    assert (built, reused) == (["leaf.rvl", "main.rvl", "mid.rvl"], [])
    assert document == first
    # the rebuild wrote whole entries back
    _again, built, _reused = _compile(tree, cache)
    assert built == []


def test_no_cache_directory_writes_nothing(tree, tmp_path):
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    report: dict = {}
    compile_files([str(tree / "main.rvl")], report=report)
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before
    assert "unit_cache" not in report


def test_the_key_moves_with_the_text_and_the_compiler(monkeypatch):
    base = unit_cache.unit_key("fn f() -> Int {\n  return 1\n}\n", "/a/f.rvl")
    assert unit_cache.unit_key("fn f() -> Int {\n  return 2\n}\n",
                               "/a/f.rvl") != base
    assert unit_cache.unit_key("fn f() -> Int {\n  return 1\n}\n",
                               "/b/f.rvl") != base
    monkeypatch.setattr(unit_cache, "compiler_digest", lambda: "other")
    assert unit_cache.unit_key("fn f() -> Int {\n  return 1\n}\n",
                               "/a/f.rvl") != base


def test_the_compiler_digest_covers_the_whole_package():
    sources = unit_cache._package_sources()
    names = {os.path.relpath(p, unit_cache._PACKAGE_DIR) for p in sources}
    assert {"parser.py", "lexer.py", "lower.py", "compiler.py",
            "unit_cache.py"} <= names
