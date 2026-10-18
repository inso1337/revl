"""The command line compiles files under `.revl/export/` as author-written
(issue #2547).

Every MCP export lands under `<root>/.revl/export/`, and the server compiles a
file there under the authoring profile by its path alone. The same file is
author-written whoever loads it later, so the command line applies the same
rule: a file that lies under a `.revl/export/` directory, named on the command
line or reached by `use`, compiles under the authoring profile. One flag,
`--trust-exported`, given to the one command, compiles it as the operator's
own and says so on stderr. Nothing else turns the rule off.

These tests drive the real entry point, `revl.__main__.main`.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.__main__ import main  # noqa: E402

# a module with a host body: refused under the authoring profile, admitted as
# the operator's own file
HOSTED = ('extern pure fn boom() -> Str = @py { return "boom" }\n'
          "service Ops { fn run() -> Str }\n"
          "component OpsBox provides ops: Ops {\n"
          "  provide ops { fn run() = boom() }\n"
          "}\n")
HOSTED_LIB = ('extern pure fn boom() -> Str = @py { return "boom" }\n'
              "pub fn shout() -> Str { return boom() }\n")
USES_EXPORTED_LIB = ('use ".revl/export/lib.rvl" { shout }\n'
                     "service Ops { fn run() -> Str }\n"
                     "component OpsBox provides ops: Ops {\n"
                     "  provide ops { fn run() = shout() }\n"
                     "}\n")

PROFILED = "untrusted-author profile forbids"


@pytest.fixture
def root(tmp_path, monkeypatch):
    base = tmp_path / "root"
    base.mkdir()
    monkeypatch.chdir(base)
    return base


def _export_dir(base: Path) -> Path:
    return base / ".revl" / "export"


def _write(path: Path, text: str = HOSTED) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _compile(capsys, path, *flags) -> tuple[int, str, dict | None]:
    out = Path(os.getcwd()) / "ir.json"
    if out.exists():
        out.unlink()
    rc = main(["compile", str(path), "-o", str(out), *flags])
    err = capsys.readouterr().err
    ir = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return rc, err, ir


def _assert_profiled(rc: int, err: str, ir) -> None:
    assert rc != 0, err
    assert ir is None
    assert PROFILED in err, err


def _assert_compiled(rc: int, err: str, ir) -> None:
    assert rc == 0, err
    assert ir is not None and ir["components"], ir
    assert PROFILED not in err, err


# ------------------------------------------------------ refused by default


def test_compile_refuses_an_exported_host_body(root, capsys):
    exported = _write(_export_dir(root) / "x.rvl")
    _assert_profiled(*_compile(capsys, exported))


def test_run_refuses_an_exported_host_body(root, capsys):
    exported = _write(_export_dir(root) / "x.rvl")
    rc = main(["run", str(exported)])
    err = capsys.readouterr().err
    assert rc != 0, err
    assert PROFILED in err, err


def test_an_exported_module_reached_by_use_is_profiled(root, capsys):
    """The operator's own file may import an exported module; the module is
    still author-written, as it is on the server."""
    _write(_export_dir(root) / "lib.rvl", HOSTED_LIB)
    operator = _write(root / "app.rvl", USES_EXPORTED_LIB)
    _assert_profiled(*_compile(capsys, operator))


# ------------------------------------------------------ the one opt-out


def test_trust_exported_compiles_it_and_names_the_file(root, capsys):
    exported = _write(_export_dir(root) / "x.rvl")
    rc, err, ir = _compile(capsys, exported, "--trust-exported")
    _assert_compiled(rc, err, ir)
    lines = [line for line in err.splitlines() if "x.rvl" in line]
    assert len(lines) == 1, err
    assert "operator" in lines[0], err


def test_trust_exported_applies_only_to_the_command_it_is_given_to(root, capsys):
    exported = _write(_export_dir(root) / "x.rvl")
    _assert_compiled(*_compile(capsys, exported, "--trust-exported"))
    _assert_profiled(*_compile(capsys, exported))


@pytest.mark.parametrize("name", ["REVL_TRUST_EXPORTED", "TRUST_EXPORTED",
                                  "REVL_CLI_TRUST_EXPORTED"])
def test_the_flag_is_not_read_from_the_environment(root, capsys, monkeypatch,
                                                   name):
    monkeypatch.setenv(name, "1")
    exported = _write(_export_dir(root) / "x.rvl")
    _assert_profiled(*_compile(capsys, exported))


# ------------------------------------------------------ the operator's files


def test_the_operator_file_compiles_as_before(root, capsys):
    """Positive control: the same text in the operator's own file."""
    _assert_compiled(*_compile(capsys, _write(root / "ops.rvl")))


def test_a_lookalike_directory_is_not_the_export_directory(root, capsys):
    lookalike = _write(root / ".revl" / "exportX" / "x.rvl")
    _assert_compiled(*_compile(capsys, lookalike))


def test_a_path_that_leaves_the_export_directory_is_the_operators(root, capsys):
    _write(root / "ops.rvl")
    _export_dir(root).mkdir(parents=True)
    named = os.path.join(str(_export_dir(root)), "..", "..", "ops.rvl")
    _assert_compiled(*_compile(capsys, named))


# ------------------------------------------------------ links and spellings


def test_a_link_to_an_exported_file_is_profiled(root, capsys):
    """The file the link resolves to lies under the export directory: the
    bytes are the author's whatever name the operator reaches them by."""
    exported = _write(_export_dir(root) / "x.rvl")
    link = root / "ops2.rvl"
    link.symlink_to(exported)
    _assert_profiled(*_compile(capsys, link))


def test_a_link_inside_the_export_directory_is_profiled(root, capsys):
    """The path the operator named lies under the export directory, so it is
    profiled even though the link points at an operator file. Deciding by the
    named path can only add the profile, never remove it: the other reading
    would let the target of a link inside the export directory, which the
    operator did not write, decide how that file compiles."""
    operator = _write(root / "ops.rvl")
    _export_dir(root).mkdir(parents=True)
    link = _export_dir(root) / "y.rvl"
    link.symlink_to(operator)
    _assert_profiled(*_compile(capsys, link))
    # the operator's own name for the same bytes is unchanged
    _assert_compiled(*_compile(capsys, operator))


def test_a_case_alias_of_the_export_directory_is_still_it(root, capsys):
    _write(_export_dir(root) / "x.rvl")
    alias = root / ".REVL" / "Export" / "x.rvl"
    rc, err, ir = _compile(capsys, alias)
    # on a case-sensitive filesystem the alias names nothing and the compile
    # fails for that reason; on a case-folding one it is the exported file
    assert rc != 0 and ir is None, err
    if alias.exists():
        assert PROFILED in err, err
