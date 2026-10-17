"""Issue #2513: a multi-file unit compiled from in-memory sources must record
each component's own file.

`compile_files(..., sources=...)` is how the MCP server compiles a working set
(`compile_under_authoring`, which `revl_edit` admits through). Each module read
from the sources map was parsed without the per-declaration provenance that a
disk read stamps, so every component fell back to the merged program's
filename, the FIRST path. A diagnostic that names a component's file then
pointed at the wrong file: the realm-removal refusal of issue #2487 named
`a.rvl` line 0 for a provider declared in `b.rvl`.

The files here exist only in the sources map, never on disk, so the compile
cannot take the disk branch.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.diagnostics import report  # noqa: E402
from revl.errors import RevlError  # noqa: E402

_A = """\
service A { fn ping(t: Str) -> Str }

component Alpha provides a: A {
  provide a { fn ping(t) = t }
}
"""

# The provider sits at line 7 of the SECOND file; `a.rvl` has only 5 lines.
_B = """\
service B { fn pong(t: Str) -> Str }




component Beta provides b: B {
  isolate b in realm("left")
  provide b { fn pong(t) = t }
}
"""

_B_LINE = 7


def _sources(b_text: str = _B) -> dict[str, str]:
    return {os.path.abspath("a.rvl"): _A, os.path.abspath("b.rvl"): b_text}


def _component(ir: dict, name: str) -> dict:
    return next(c for c in ir["components"] if c["name"] == name)


def test_in_memory_component_ir_names_its_own_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ir = compile_files(["a.rvl", "b.rvl"], sources=_sources())

    assert _component(ir, "Alpha")["source"] == "a.rvl"
    # the bug recorded `a.rvl` (paths[0]) for a component declared in b.rvl
    assert _component(ir, "Beta")["source"] == "b.rvl"
    assert _component(ir, "Beta")["isolate"] == {"b": "left"}
    # the manifest and the component IR agree on the file
    files = {e["name"]: e["file"] for e in ir["manifest"]["components"]}
    assert files == {"Alpha": "a.rvl", "Beta": "b.rvl"}


def test_in_memory_matches_the_disk_compile(tmp_path, monkeypatch):
    """The sources map and a disk read of the same texts give one provenance."""
    monkeypatch.chdir(tmp_path)
    virtual = compile_files(["a.rvl", "b.rvl"], sources=_sources())
    (tmp_path / "a.rvl").write_text(_A)
    (tmp_path / "b.rvl").write_text(_B)
    disk = compile_files(["a.rvl", "b.rvl"])

    def sources(ir):
        return [(c["name"], c["source"]) for c in ir["components"]]

    assert sources(virtual) == sources(disk)


def test_in_memory_component_diagnostic_names_its_file_and_line(tmp_path, monkeypatch):
    """A refusal raised inside the second file's component names that file at
    the component's real line, in the error and in the JSON report."""
    monkeypatch.chdir(tmp_path)
    broken = _B.replace("fn pong(t) = t", "fn pong(t) = gone.pong(t)")

    with pytest.raises(RevlError) as excinfo:
        compile_files(["a.rvl", "b.rvl"], sources=_sources(broken))

    error = excinfo.value
    assert error.filename == "b.rvl", error.filename
    assert error.line == _B_LINE + 1
    assert len(_A.splitlines()) < error.line
    diag = report(error)["diagnostics"][0]
    assert diag["file"] == "b.rvl"
    assert diag["line"] == _B_LINE + 1


def test_server_authoring_compile_names_the_second_file(tmp_path, monkeypatch):
    """The MCP server's working-set compile, which `revl_edit` admits through,
    carries the same per-component file. The test server's authoring profile
    may forbid naming a realm, so this provider carries no realm clause."""
    from revl.mcp.server import compile_under_authoring

    monkeypatch.chdir(tmp_path)
    shared = _B.replace('  isolate b in realm("left")\n', "")
    ir = compile_under_authoring(None, ["a.rvl", "b.rvl"], modules=_sources(shared))

    assert _component(ir, "Beta")["source"] == "b.rvl"
