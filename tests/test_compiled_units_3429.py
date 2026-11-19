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
    built: dict = {}
    read: dict = {}
    building = _outcome([str(example)], cache_dir=str(tmp_path / "units"),
                        report=built)
    reading = _outcome([str(example)], cache_dir=str(tmp_path / "units"),
                       report=read)
    assert building == cold
    assert reading == cold
    if cold[0] == "document":
        # the second compile read back every unit the first one built
        assert built["unit_cache"]["built"]
        assert read["unit_cache"]["built"] == []
        assert (sorted(read["unit_cache"]["reused"])
                == sorted(built["unit_cache"]["built"]))


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


def _damage_deep_array(entry: Path) -> None:
    """Not JSON a writer makes: nested deeper than the stack allows."""
    entry.write_text("[" * 200000, encoding="utf-8")


def _damage_deep_program(entry: Path) -> None:
    """The right key, then a program that is lists nested 30000 deep."""
    data = json.loads(entry.read_text(encoding="utf-8"))
    entry.write_text('{"key":' + json.dumps(data["key"]) + ',"program":'
                     + "[" * 30000 + "]" * 30000 + "}", encoding="utf-8")


def _damage_oversized(entry: Path, cap: int = 1024) -> None:
    entry.write_bytes(entry.read_bytes() + b" " * cap)


@pytest.mark.parametrize("damage", [
    _damage_garbage, _damage_truncated, _damage_wrong_version,
    _damage_foreign_class, _damage_dropped_field, _damage_deep_array,
    _damage_deep_program],
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


def test_a_deep_but_well_formed_program_is_a_miss(monkeypatch, tmp_path):
    """The decoder recurses; a tree deeper than the stack is a miss, not a
    crash, even when the JSON reader let it through."""
    deep: list = []
    for _ in range(100000):
        deep = [deep]
    key = unit_cache.unit_key("fn f() -> Int {\n  return 1\n}\n", "/a/f.rvl")
    entry = tmp_path / "entry.json"
    entry.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(unit_cache.json, "loads",
                        lambda _blob: {"key": key, "program": deep})
    assert unit_cache._read_entry(str(entry), key) is None


def test_an_entry_past_the_size_cap_is_rebuilt(tree, tmp_path, monkeypatch):
    cache = tmp_path / "units"
    first, _built, _reused = _compile(tree, cache)
    monkeypatch.setattr(unit_cache, "MAX_ENTRY_BYTES",
                        max(e.stat().st_size for e in _entries(cache)))
    for entry in _entries(cache):
        _damage_oversized(entry)
    document, built, _reused = _compile(tree, cache)
    assert built == ["leaf.rvl", "main.rvl", "mid.rvl"]
    assert document == first


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt",
                    reason="symlinks")
def test_an_entry_that_is_a_symlink_is_not_followed(tree, tmp_path):
    cache = tmp_path / "units"
    first, _built, _reused = _compile(tree, cache)
    for entry in _entries(cache):
        real = entry.with_suffix(".real")
        entry.rename(real)
        entry.symlink_to(real)
    document, built, _reused = _compile(tree, cache)
    assert built == ["leaf.rvl", "main.rvl", "mid.rvl"]
    assert document == first
    # the rewrite replaced each link with a plain file
    assert not any(e.is_symlink() for e in _entries(cache))


def test_a_new_cache_directory_is_private(tree, tmp_path):
    cache = tmp_path / "units"
    _compile(tree, cache)
    if os.name != "nt":
        assert cache.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("table", ["decl_files", "fn_scopes",
                                   "fn_alias_scopes"])
def test_a_unit_with_an_identity_table_is_not_written(table, tree, tmp_path,
                                                      monkeypatch):
    real = unit_cache._parser.Parser.parse

    def parse_with_table(self):
        program = real(self)
        setattr(program, table, {id(program): {"x"}})
        return program

    monkeypatch.setattr(unit_cache._parser.Parser, "parse", parse_with_table)
    cache = tmp_path / "units"
    unit = unit_cache.UnitCache(str(cache))
    unit.parse((tree / "leaf.rvl").read_text(encoding="utf-8"),
               str(tree / "leaf.rvl"))
    assert _entries(cache) == []


def test_a_failed_write_leaves_no_temporary_file(tree, tmp_path, monkeypatch):
    def refuse(_src, _dst):
        raise OSError("refused")

    monkeypatch.setattr(unit_cache.os, "replace", refuse)
    cache = tmp_path / "units"
    _compile(tree, cache)
    assert list(cache.iterdir()) == []


def test_no_sources_means_no_cache(tree, tmp_path, monkeypatch):
    """A bytecode-only install has nothing to digest: nothing is read or
    written, and the compile is the plain one."""
    monkeypatch.setattr(unit_cache, "has_sources", lambda: False)
    cache = tmp_path / "units"
    document, built, reused = _compile(tree, cache)
    assert (built, reused) == (["leaf.rvl", "main.rvl", "mid.rvl"], [])
    assert not cache.exists()
    assert document == compile_files([str(tree / "main.rvl")])


def test_the_key_carries_the_revl_version(monkeypatch):
    unit_cache.compiler_digest.cache_clear()
    try:
        base = unit_cache.compiler_digest()
        monkeypatch.setattr(unit_cache, "_revl_version", lambda: "0.0.0-other")
        unit_cache.compiler_digest.cache_clear()
        assert unit_cache.compiler_digest() != base
    finally:
        monkeypatch.undo()
        unit_cache.compiler_digest.cache_clear()


def _reverse_scalar_sets(value):
    """Every stored set or frozenset of plain values, in reversed order. Only
    scalar members, so the numbering of shared objects is unchanged."""
    if isinstance(value, list):
        return [_reverse_scalar_sets(x) for x in value]
    if isinstance(value, dict):
        if (len(value) == 1 and next(iter(value)) in ("s", "f")
                and all(not isinstance(x, (list, dict))
                        for x in next(iter(value.values())))):
            tag, members = next(iter(value.items()))
            return {tag: list(reversed(members))}
        return {k: _reverse_scalar_sets(v) for k, v in value.items()}
    return value


def test_set_order_does_not_reach_the_document(tmp_path, monkeypatch):
    """Decoded sets come back in another order (measured at `FnDecl.idents`,
    read by `_lower_fns` for membership only). Rebuild every stored set in
    reversed order and the document is still the cold one."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "t.rvl").write_text(
        "fn risky(n: Int) -> Result[Int, Str] {\n"
        "  if (n > 0) {\n    return Ok(n)\n  }\n  return Err(\"neg\")\n}\n\n"
        "fn twice(n: Int) -> Result[Int, Str] {\n"
        "  let a = try risky(n)\n  let b = try risky(a)\n  return Ok(a + b)\n}\n",
        encoding="utf-8")
    cold = _outcome(["t.rvl"])
    assert cold[0] == "document"
    cache = tmp_path / "units"
    _outcome(["t.rvl"], cache_dir=str(cache))
    reversed_any = False
    for entry in _entries(cache):
        data = json.loads(entry.read_text(encoding="utf-8"))
        flipped = _reverse_scalar_sets(data)
        reversed_any = reversed_any or flipped != data
        entry.write_text(json.dumps(flipped), encoding="utf-8")
    assert reversed_any
    report: dict = {}
    assert _outcome(["t.rvl"], cache_dir=str(cache), report=report) == cold
    assert report["unit_cache"]["built"] == []
