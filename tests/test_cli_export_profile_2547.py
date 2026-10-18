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
import threading
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


# ------------------------------------------------------ the note and the flag


def test_the_note_names_the_flag_the_command_takes(root, capsys):
    exported = _write(_export_dir(root) / "x.rvl")
    rc, err, ir = _compile(capsys, exported)
    _assert_profiled(rc, err, ir)
    assert "pass --trust-exported" in err, err


@pytest.mark.parametrize("command", [["compile", "-o", "ir.json"], ["run"],
                                     ["audit"], ["fmt"]])
@pytest.mark.parametrize("spelling", ["--trust-e", "--trust-export",
                                      "--trust-exporte"])
def test_an_abbreviation_of_the_flag_is_refused(root, capsys, command,
                                                spelling):
    """Only the full spelling turns the rule off for a command."""
    exported = _write(_export_dir(root) / "x.rvl")
    with pytest.raises(SystemExit) as stopped:
        main([command[0], str(exported), *command[1:], spelling])
    assert stopped.value.code != 0
    assert "unrecognized arguments" in capsys.readouterr().err
    assert not (root / "ir.json").exists()


# ------------------------------------------------------ other entries


def test_a_library_caller_gets_the_rule_by_default(root):
    """The rule is on for every compile in the process, not only one that
    starts in `main`: a program that imports the compiler and names an
    exported file gets the authoring profile, and the operator's file
    compiles as before."""
    from revl.compiler import compile_files, compile_source  # noqa: PLC0415
    from revl.errors import RevlError  # noqa: PLC0415

    exported = _write(_export_dir(root) / "x.rvl")
    with pytest.raises(RevlError, match=PROFILED):
        compile_files([str(exported)])
    with pytest.raises(RevlError, match=PROFILED):
        compile_source(HOSTED, str(exported))
    operator = _write(root / "ops.rvl")
    assert compile_files([str(operator)])["components"]
    assert compile_source(HOSTED, str(operator))["components"]


def _admit(path: Path) -> dict:
    """Admit `path` as a vendored row, with the host code trusted. A named
    row is admitted as an untrusted author (#2560), and HOSTED carries host
    code, so the command's `trust_host_code` (truc's --trust-host-code) is
    what lets the text through; the export-area rule is a separate check
    that this trust does not lift."""
    from revl.truc._host import admit_all  # noqa: PLC0415

    return json.loads(admit_all(json.dumps(
        [{"path": str(path), "source": HOSTED, "name": "x"}]),
        trust_host_code=True))


def test_the_truc_host_entry_profiles_an_exported_file(root, capsys):
    """truc's admission loop compiles in-memory text under the file's own
    path, so an exported file is profiled there too."""
    exported = _export_dir(root) / "trucs" / "x" / "component.rvl"
    _write(exported)
    refused = _admit(exported)
    assert refused["ok"] is False, refused
    assert PROFILED in refused["diagnostic"], refused
    # truc takes no --trust-exported, so the note does not offer it
    err = capsys.readouterr().err
    assert "copy it out of the export directory" in err, err
    assert "--trust-exported" not in err, err
    # control: the same text, trusted the same way, at a path outside the
    # export directory is admitted, so only the location refused it above
    admitted = _admit(_write(root / "trucs" / "x" / "component.rvl"))
    assert admitted["ok"] is True, admitted


# ------------------------------------------------------ the server's door


@pytest.fixture
def trusted_server(root):
    from revl.mcp import server  # noqa: PLC0415

    old = server.AUTHORING
    server.set_authoring_trust(roots=(str(root),))
    server.set_authoring_trust(host_code=True)
    assert server.AUTHORING.profile() is None
    try:
        yield server
    finally:
        server.AUTHORING = old


def test_the_server_decides_inside_its_door_and_the_rule_holds_outside(
        trusted_server, root):
    """A trusted server compiles an exported file by its own rule alone, and
    the same file outside a request is the author's again."""
    from revl.compiler import compile_files  # noqa: PLC0415
    from revl.errors import RevlError  # noqa: PLC0415

    exported = _write(_export_dir(root) / "x.rvl")
    reply = trusted_server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "revl_check",
                   "arguments": {"files": [str(exported)]}}})
    assert reply["result"]["structuredContent"]["ok"] is True, reply
    with pytest.raises(RevlError, match=PROFILED):
        compile_files([str(exported)])


def test_the_rule_is_back_after_a_request_that_raises(root):
    from revl import compiler  # noqa: PLC0415
    from revl.export_area import server_door  # noqa: PLC0415

    inside = []

    def failing(message):
        inside.append(compiler._export_rule())
        raise RuntimeError("request failed")

    with pytest.raises(RuntimeError):
        server_door(failing)({})
    assert inside == [None]
    assert compiler._export_rule() is not None


def test_turning_the_rule_off_is_unseen_by_another_thread(root):
    from revl import compiler  # noqa: PLC0415
    from revl.export_area import cli_rule  # noqa: PLC0415

    seen = []
    with cli_rule(off=True):
        assert compiler._export_rule() is None
        other = threading.Thread(
            target=lambda: seen.append(compiler._export_rule()))
        other.start()
        other.join()
    assert seen and seen[0] is not None
    assert compiler._export_rule() is not None


@pytest.mark.parametrize("argv", [["mcp", "serve", "--trust-exported"],
                                  ["truc", "--trust-exported"]])
def test_the_commands_without_the_flag_refuse_it(capsys, argv):
    """`mcp serve` decides by `--author-trust` and `truc` forwards its tail
    to the component manager: neither takes `--trust-exported`."""
    from revl.cli.parser import build_parser  # noqa: PLC0415

    with pytest.raises(SystemExit) as stopped:
        build_parser().parse_args(argv)
    assert stopped.value.code != 0
    assert "--trust-exported" in capsys.readouterr().err


def test_other_options_still_abbreviate():
    """Only `--trust-exported` must be spelled in full; every other option
    keeps the abbreviations argparse gives it (`--json` for
    `--json-diagnostics`)."""
    from revl.cli.parser import build_parser  # noqa: PLC0415

    args = build_parser().parse_args(["compile", "--json", "x.rvl"])
    assert args.json_diagnostics is True
    assert args.trust_exported is False


@pytest.mark.parametrize("bindings", [(), (("db", "realm-a"),)])
def test_the_rule_uses_the_servers_default_authoring_profile(bindings):
    """The compile side builds the authoring profile itself, so compiling
    never imports the server; it is the server's default one exactly."""
    from revl.admit_profile import AdmissionProfile  # noqa: PLC0415
    from revl.export_area import author_profile  # noqa: PLC0415
    from revl.mcp.server import AuthoringTrust  # noqa: PLC0415

    base = AdmissionProfile(realm_bindings=bindings)
    assert author_profile(base) == AuthoringTrust(
        realm_bindings=bindings).profile()
    assert author_profile(base).untrusted


def test_text_read_through_a_link_into_the_export_directory_is_profiled(
        root):
    """`revl adapt` and the formatter read a file themselves and compile its
    text under the name they read it by. A link outside the export directory
    to an exported file is still the author's file; the operator's own file
    compiles as before."""
    from revl.cli.adapt import _load  # noqa: PLC0415
    from revl.errors import RevlError  # noqa: PLC0415
    from revl.formatter import _compile_ir  # noqa: PLC0415

    exported = _write(_export_dir(root) / "x.rvl")
    link = root / "ops.rvl"
    link.symlink_to(exported)
    with pytest.raises(RevlError, match=PROFILED):
        _load(str(link))
    ir, error = _compile_ir(HOSTED, str(link))
    assert ir is None and PROFILED in str(error), error
    operator = _write(root / "own.rvl")
    assert _load(str(operator))["components"]
    ir, error = _compile_ir(HOSTED, str(operator))
    assert error is None and ir["components"], error
