"""Issue #2192: tests/_compiled_ir.py compiles a self-host document once per key.

The key must move with every input of the build, or a test would run against
the IR of sources that are no longer there; a hit must hand out a copy, so one
caller's mutation never reaches another; and the on-disk copy must stay whole
when parallel workers write it at once.
"""

from __future__ import annotations

import os
import pickle
import shutil
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import _compiled_ir  # noqa: E402
from _compiled_ir import Inputs, cache_key, compiled_ir  # noqa: E402


@pytest.fixture
def fresh(monkeypatch):
    """The helper with no in-process copy and no disk directory."""
    monkeypatch.setattr(_compiled_ir, "_memory", {})
    monkeypatch.setattr(_compiled_ir, "_cache_dir", None)
    monkeypatch.setattr(_compiled_ir, "_package_pin", {}, raising=False)
    return _compiled_ir


def _tree(tmp_path: Path) -> tuple[Path, Inputs]:
    """A document with one relative and one stdlib import, and a fake revl."""
    (tmp_path / "doc").mkdir()
    (tmp_path / "doc" / "main.rvl").write_text(
        'use "./dep.rvl" { f }\nuse "stdlib/str.rvl" { trim }\n'
        'ref k from "./host.py"\nfn main() -> Int = @py file "./body.py"\n')
    (tmp_path / "doc" / "dep.rvl").write_text('use "./deep.rvl" { h }\nfn f() -> Int { 2 }\n')
    (tmp_path / "doc" / "deep.rvl").write_text("fn h() -> Int { 0 }\n")
    (tmp_path / "doc" / "host.py").write_text("def k(): return 0\n")
    (tmp_path / "doc" / "body.py").write_text("return 0\n")
    (tmp_path / "doc" / "unrelated.rvl").write_text("fn g() -> Int { 3 }\n")
    (tmp_path / "stdlib").mkdir()
    (tmp_path / "stdlib" / "str.rvl").write_text("fn trim() -> Int { 4 }\n")
    (tmp_path / "revl").mkdir()
    (tmp_path / "revl" / "compiler.py").write_text("X = 1\n")
    (tmp_path / "backends" / "python").mkdir(parents=True)
    (tmp_path / "backends" / "python" / "emit.py").write_text("")
    inputs = Inputs(package_dir=tmp_path / "revl", stdlib_dir=tmp_path / "stdlib",
                    backends_python=tmp_path / "backends" / "python", import_path=())
    return tmp_path / "doc" / "main.rvl", inputs


def test_the_key_changes_when_lower_rvl_changes(tmp_path):
    """On the real compiler inputs: one byte of lower.rvl is a new key."""
    copy = tmp_path / "selfhost" / "lower.rvl"
    copy.parent.mkdir()
    shutil.copyfile(ROOT / "selfhost" / "lower.rvl", copy)
    before = cache_key(copy)
    assert cache_key(copy) == before
    copy.write_text(copy.read_text(encoding="utf-8") + "\n")
    assert cache_key(copy) != before


@pytest.mark.parametrize("edit", [
    "the document", "a relative import", "an import of an import",
    "a ref host module", "a backend body file", "a stdlib file", "the compiler",
    "a new backends/python module", "REVL_IMPORT_PATH", "the path spelling"])
def test_the_key_moves_with_every_input(tmp_path, fresh, edit):
    root, inputs = _tree(tmp_path)
    before = cache_key(root, inputs)
    if edit == "the document":
        root.write_text(root.read_text(encoding="utf-8") + "\n")
    elif edit == "a relative import":
        (root.parent / "dep.rvl").write_text('use "./deep.rvl" { h }\nfn f() -> Int { 5 }\n')
    elif edit == "an import of an import":
        (root.parent / "deep.rvl").write_text("fn h() -> Int { 11 }\n")
    elif edit == "a ref host module":
        (root.parent / "host.py").write_text("def k(): return 1\n")
    elif edit == "a backend body file":
        (root.parent / "body.py").write_text("return 1\n")
    elif edit == "a stdlib file":
        (inputs.stdlib_dir / "str.rvl").write_text("fn trim() -> Int { 6 }\n")
    elif edit == "the compiler":
        (inputs.package_dir / "compiler.py").write_text("X = 2\n")
        fresh._package_pin.clear()  # a new process: the package is pinned once
    elif edit == "a new backends/python module":
        (inputs.backends_python / "runtime_x.py").write_text("")
    elif edit == "REVL_IMPORT_PATH":
        inputs = Inputs(inputs.package_dir, inputs.stdlib_dir,
                        inputs.backends_python, (str(tmp_path / "extra"),))
    elif edit == "the path spelling":
        root = Path(os.path.relpath(root))  # what compile_files is handed differs
    assert cache_key(root, inputs) != before


def test_the_key_ignores_what_the_build_does_not_read(tmp_path, fresh):
    root, inputs = _tree(tmp_path)
    before = cache_key(root, inputs)
    (root.parent / "unrelated.rvl").write_text("fn g() -> Int { 7 }\n")
    pycache = inputs.package_dir / "__pycache__"
    pycache.mkdir()
    (pycache / "compiler.cpython-312.pyc").write_bytes(b"\0")
    fresh._package_pin.clear()
    assert cache_key(root, inputs) == before


def test_an_import_path_entry_is_followed(tmp_path, fresh):
    """A `use` that resolves under REVL_IMPORT_PATH is an input too."""
    root, inputs = _tree(tmp_path)
    extra = tmp_path / "extra"
    (extra / "stdlib").mkdir(parents=True)
    (extra / "stdlib" / "str.rvl").write_text("fn trim() -> Int { 8 }\n")
    inputs = Inputs(inputs.package_dir, inputs.stdlib_dir,
                    inputs.backends_python, (str(extra),))
    before = cache_key(root, inputs)
    (extra / "stdlib" / "str.rvl").write_text("fn trim() -> Int { 9 }\n")
    assert cache_key(root, inputs) != before


def _counting_compile(calls):
    def compile(root):
        calls.append(root)
        return {"functions": [{"name": "main", "body": [1, 2]}], "n": len(calls)}
    return compile


def test_a_second_call_does_not_compile_and_gets_its_own_copy(tmp_path, fresh):
    root, inputs = _tree(tmp_path)
    calls = []
    first = compiled_ir(root, _counting_compile(calls), inputs)
    first["functions"][0]["body"].append("mutated")
    second = compiled_ir(root, _counting_compile(calls), inputs)
    assert len(calls) == 1
    assert second == {"functions": [{"name": "main", "body": [1, 2]}], "n": 1}


def test_a_changed_input_compiles_again(tmp_path, fresh):
    root, inputs = _tree(tmp_path)
    calls = []
    compiled_ir(root, _counting_compile(calls), inputs)
    (root.parent / "dep.rvl").write_text("fn f() -> Int { 10 }\n")
    assert compiled_ir(root, _counting_compile(calls), inputs)["n"] == 2


def test_the_disk_copy_is_reused_by_a_new_process(tmp_path, fresh):
    root, inputs = _tree(tmp_path)
    cache = tmp_path / "cache"
    fresh.configure(cache)
    calls = []
    compiled_ir(root, _counting_compile(calls), inputs)
    assert [p.suffix for p in cache.iterdir()] == [".pickle"]
    fresh._memory.clear()  # what a new session, or another worker, starts with
    assert compiled_ir(root, _counting_compile(calls), inputs)["n"] == 1
    assert len(calls) == 1


def test_a_bad_disk_copy_is_a_miss(tmp_path, fresh):
    root, inputs = _tree(tmp_path)
    cache = tmp_path / "cache"
    fresh.configure(cache)
    calls = []
    compiled_ir(root, _counting_compile(calls), inputs)
    [stored] = cache.iterdir()
    stored.write_bytes(stored.read_bytes()[:20])  # a truncated write
    fresh._memory.clear()
    assert compiled_ir(root, _counting_compile(calls), inputs)["n"] == 2
    fresh._memory.clear()
    assert compiled_ir(root, _counting_compile(calls), inputs)["n"] == 2


def test_a_new_key_replaces_the_old_file(tmp_path, fresh):
    root, inputs = _tree(tmp_path)
    cache = tmp_path / "cache"
    fresh.configure(cache)
    calls = []
    compiled_ir(root, _counting_compile(calls), inputs)
    [old] = cache.iterdir()
    root.write_text(root.read_text(encoding="utf-8") + "\n")
    compiled_ir(root, _counting_compile(calls), inputs)
    [new] = cache.iterdir()
    assert new != old and new.suffix == ".pickle"


def test_parallel_writers_never_expose_a_partial_file(tmp_path):
    """Workers that miss together all write; a reader sees one whole file."""
    cache = tmp_path / "cache"
    path = cache / "doc-0-k.pickle"
    blobs = [pickle.dumps(("k", bytes([i]) * 2_000_000)) for i in range(8)]
    seen, stop = [], threading.Event()

    def read():
        while not stop.is_set():
            try:
                got = path.read_bytes()  # raw: a partial file is not a miss here
            except FileNotFoundError:
                continue
            seen.append(got in blobs)

    reader = threading.Thread(target=read)
    reader.start()
    writers = [threading.Thread(target=_compiled_ir._write,
                                args=(cache, "doc-0", path, blob)) for blob in blobs]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()
    stop.set()
    reader.join()
    assert path.read_bytes() in blobs
    assert seen and all(seen)
    assert [p.name for p in cache.iterdir()] == [path.name]  # no temp file left


def test_a_compiler_edit_mid_session_never_reaches_the_disk(tmp_path, fresh):
    """The compile runs the revl this process imported. After the package
    changes on disk, a new build would be old code under a key that names
    the new sources, and the next session would read it as current."""
    root, inputs = _tree(tmp_path)
    cache = tmp_path / "cache"
    fresh.configure(cache)
    calls = []
    compiled_ir(root, _counting_compile(calls), inputs)
    [first] = cache.iterdir()
    pinned = cache_key(root, inputs)
    (inputs.package_dir / "compiler.py").write_text("X = 'edited'\n")
    assert cache_key(root, inputs) == pinned  # the code that runs, not the disk
    assert fresh.package_drifted(inputs.package_dir)
    root.write_text(root.read_text(encoding="utf-8") + "\n")  # a miss after the edit
    compiled_ir(root, _counting_compile(calls), inputs)
    assert len(calls) == 2
    assert list(cache.iterdir()) == [first]  # nothing written, nothing pruned
    # The next session hashes the edited package: a new key, a fresh compile.
    fresh._memory.clear()
    fresh._package_pin.clear()
    assert cache_key(root, inputs) != pinned
    compiled_ir(root, _counting_compile(calls), inputs)
    assert len(calls) == 3


def test_an_mtime_preserving_edit_after_the_pin_keeps_the_pinned_key(tmp_path, fresh):
    """`cp -p`, `rsync -t` or `touch -r` change content but not size or
    mtime. Between the session-start pin and the first key, such an edit must
    not be hashed as the code this process runs."""
    root, inputs = _tree(tmp_path)
    compiler = inputs.package_dir / "compiler.py"
    fresh.pin_package(inputs.package_dir)  # what conftest does at session start
    before = compiler.stat()
    compiler.write_text("X = 9\n")  # same size as "X = 1\n"
    os.utime(compiler, ns=(before.st_atime_ns, before.st_mtime_ns))
    pinned = cache_key(root, inputs)
    fresh._package_pin.clear()  # a new process hashes the edited package
    edited = cache_key(root, inputs)
    compiler.write_text("X = 1\n")
    os.utime(compiler, ns=(before.st_atime_ns, before.st_mtime_ns))
    fresh._package_pin.clear()
    assert cache_key(root, inputs) == pinned != edited


def test_a_drifted_process_does_not_read_the_disk_either(tmp_path, fresh):
    root, inputs = _tree(tmp_path)
    cache = tmp_path / "cache"
    fresh.configure(cache)
    calls = []
    compiled_ir(root, _counting_compile(calls), inputs)
    fresh._memory.clear()
    (inputs.package_dir / "compiler.py").write_text("X = 'edited'\n")
    compiled_ir(root, _counting_compile(calls), inputs)
    assert len(calls) == 2


def test_a_real_document_compiles_to_what_compile_files_returns(fresh):
    from revl import compile_files
    lexer = ROOT / "selfhost" / "lexer.rvl"
    assert compiled_ir(lexer) == compile_files([str(lexer)])
    assert compiled_ir(lexer) == compile_files([str(lexer)])


def test_conftest_points_the_disk_copy_at_the_pytest_cache(pytestconfig):
    cache = getattr(pytestconfig, "cache", None)
    expected = cache.mkdir("revl-compiled-ir") if cache is not None else None
    assert _compiled_ir._cache_dir == expected


def test_conftest_pins_the_imported_package_at_session_start():
    import revl
    assert Path(revl.__file__).resolve().parent in _compiled_ir._package_pin
