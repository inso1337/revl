"""issue #3033: a duplicate `type` across files names the files that declare it.

A `type` declaration recorded no file of its own, so `_lower_type_decls` raised
every refusal against the merged program's filename, the first file of the
compile. Two files that both declared `pub type T` were refused at
`a.rvl:<line of the duplicate>` when a.rvl held neither declaration. A
duplicate service across files already names both files; a duplicate type now
does too. Within one file the message is unchanged (the self-host checker
emits it byte for byte).
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.errors import RevlError  # noqa: E402


def _refusal(tmp_path, monkeypatch, files: dict[str, str]) -> str:
    monkeypatch.chdir(tmp_path)
    for name, body in files.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    with pytest.raises(RevlError) as err:
        compile_files(list(files))
    return str(err.value)


def test_duplicate_type_across_files_names_both_declarations(tmp_path, monkeypatch):
    # the issue's reproducer: the first file of the compile declares no type
    message = _refusal(tmp_path, monkeypatch, {
        "a.rvl": "pub fn a() -> Int { 1 }\n",
        "b.rvl": "pub type T = { x: Int }\n",
        "c.rvl": "\npub type T = { y: Int }\n",
    })
    assert message == "c.rvl:2: duplicate type `T` (also declared in b.rvl:1)"


def test_duplicate_type_in_one_file_keeps_its_message(tmp_path, monkeypatch):
    message = _refusal(tmp_path, monkeypatch, {
        "a.rvl": "pub fn a() -> Int { 1 }\n",
        "b.rvl": "pub type T = { x: Int }\npub type T = { y: Int }\n",
    })
    assert message == "b.rvl:2: duplicate type `T`"


def test_a_type_refusal_names_the_declaring_file(tmp_path, monkeypatch):
    # the other refusals of the type table name the declaring file too
    message = _refusal(tmp_path, monkeypatch, {
        "a.rvl": "pub fn a() -> Int { 1 }\n",
        "b.rvl": "pub type T = {\n  x: Int,\n  x: Int\n}\n",
    })
    assert message.startswith("b.rvl:3: duplicate field `x` in record `T`"), message


def test_a_reserved_name_refusal_names_the_declaring_file(tmp_path, monkeypatch):
    message = _refusal(tmp_path, monkeypatch, {
        "a.rvl": "pub fn a() -> Int { 1 }\n",
        "b.rvl": "\npub type Principal = { x: Int }\n",
    })
    assert message.startswith("b.rvl:2: `Principal` is the reserved"), message
