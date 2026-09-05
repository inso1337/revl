"""Admission confinement of `use` at the library doors.

The MCP transport refuses an absolute or upward-traversing `use` path in
source that arrived over the wire (roadmap 425 F2, `mcp.server._jail_refusal`).
The same source reaches the compiler through doors that have no transport in
front of them: `Session.admit` (item 330, and the in-language `admit` crossing
behind it), `Gate.propose` (item 334), and `compile_under_authoring` called as
a library. Each compiled under the untrusted-author profile and followed the
`use` to disk, so the refusal handed back as the verdict disclosed whether the
path exists and what its first token is.

The confinement now lives in the compile itself, keyed on `profile.untrusted`,
which is the one fact every door already states. These tests hold it at each
door, prove the refusal is not itself an existence oracle, and pin the shapes
that must keep working: a trusted author's compile of the same source, an
in-memory module at an absolute path, the search-path spelling of an installed
module, and a file read from disk resolving its own upward import.

The second half: `_ModuleLoader` normalises sources keys to abspath, so a
relative key stands in for the file it names instead of being silently
substituted by the on-disk file of the same name (`gate_service.admit`).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from revl import AdmissionProfile, compile_files, compile_source
from revl import compiler as _compiler
from revl import gate_service
from revl.errors import RevlError
from revl.mcp import server

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the live Session.admit door needs a cordis-py composition",
)

# The running composition a per-turn source admits against.
_BASE = (
    "service S { fn f() -> Int }\n"
    "component Base provides s: S { provide s { fn f() = 1 } }\n"
)
# A trusted provider handed to `Gate.propose` as an in-memory module.
_PROVIDER = "pub fn helper(n: Int) -> Int { return n }\n"
# The admitted source: one import of the probed path, one inert component.
_TURN = 'use "{path}" {{ q }}\ncomponent T {{ }}\n'
# What a legitimately imported module looks like.
_LIB = "pub fn q(n: Int) -> Int { return n }\n"

_RULE = "admission confinement"


def _base_ir() -> dict:
    return compile_source(_BASE)


# ----------------------------------------------------------------- the doors
#
# Each entry compiles `source` exactly as the named door does (the call is
# copied, not approximated), under that door's own profile constructor.

def _session_admit(source: str) -> dict:
    # `mcp.session.Session.admit` (item 330; the in-language crossing delegates
    # to it): manifest-linked, no modules, `untrusted_author`.
    return compile_source(source, "<turn>.rvl", manifest=_base_ir(),
                          modules=None,
                          profile=AdmissionProfile.untrusted_author(()))


def _gate_propose(source: str) -> dict:
    # `gate.Gate.propose` (item 334): the standalone decision compile with the
    # trusted providers handed in as `modules=`, under `self_extension`.
    return compile_source(source, "<candidate>.rvl",
                          modules={"prov.rvl": _PROVIDER},
                          profile=AdmissionProfile.self_extension(()))


def _authoring_library(source: str) -> dict:
    # `mcp.server.compile_under_authoring` called as a library with the
    # default `over_the_transport=True` and no dispatch jail in front of it.
    return server.compile_under_authoring(source, None,
                                          modules={"unused.rvl": "// x\n"})


_DOORS = {
    "Session.admit": _session_admit,
    "Gate.propose": _gate_propose,
    "compile_under_authoring": _authoring_library,
}


@pytest.fixture
def secret(tmp_path):
    """A readable non-revl file whose first token must never come back."""
    p = tmp_path / "secret.txt"
    p.write_text("SECRET_TOKEN_hunter2 more stuff\n", encoding="utf-8")
    return p


@pytest.fixture
def probes(secret):
    return [
        str(secret),                                   # absolute, exists
        str(secret.parent / "definitely-not-here.txt"),  # absolute, absent
        "/etc/passwd",
        "../../../../../etc/passwd",
        "../outside.rvl",
    ]


@pytest.mark.parametrize("door", sorted(_DOORS))
def test_an_untrusted_use_probe_is_refused_at_every_library_door(door, probes):
    for path in probes:
        with pytest.raises(RevlError) as err:
            _DOORS[door](_TURN.format(path=path))
        text = str(err.value)
        assert _RULE in text, (door, path, text)
        assert "SECRET_TOKEN" not in text, (door, path)
        # neither of the two messages the probe used to come back as
        assert "cannot find imported module" not in text, (door, path)
        assert "expected a top-level declaration" not in text, (door, path)
        assert getattr(err.value, "category", None) == "admission"


def test_the_refusal_is_not_itself_an_existence_oracle(secret, monkeypatch):
    """Byte-identical modulo the path, whether or not the file exists, and
    nothing on disk is consulted on the way to it."""
    present = str(secret)
    absent = str(secret.parent / "definitely-not-here.txt")

    def _boom(*a, **k):
        raise AssertionError("the loader touched the filesystem under confinement")

    monkeypatch.setattr(_compiler, "parse_file", _boom)
    monkeypatch.setattr(_compiler._ModuleLoader, "_exists", _boom)

    with pytest.raises(RevlError) as a:
        _session_admit(_TURN.format(path=present))
    with pytest.raises(RevlError) as b:
        _session_admit(_TURN.format(path=absent))
    assert str(a.value).replace(present, "X") == str(b.value).replace(absent, "X")


def test_a_use_inside_a_supplied_module_is_refused_too():
    """The importer need not be the admitted root: a `modules=` entry is
    agent-authored as well and its imports are confined the same way."""
    with pytest.raises(RevlError) as err:
        compile_source('use "m.rvl" { q }\ncomponent T { }\n', "<turn>.rvl",
                       manifest=_base_ir(),
                       modules={"m.rvl": 'use "/etc/passwd" { p }\n' + _LIB},
                       profile=AdmissionProfile.untrusted_author(()))
    assert _RULE in str(err.value)
    assert "/etc/passwd" in str(err.value)


def test_the_refusal_names_what_the_author_can_do_instead():
    with pytest.raises(RevlError) as err:
        _session_admit(_TURN.format(path="/etc/passwd"))
    hint = err.value.hint or ""
    assert "`modules=`" in hint and "stdlib/str.rvl" in hint


# ------------------------------------------ the shapes that must keep working

def test_a_trusted_author_compiles_the_same_source(tmp_path):
    """The distinction is the author, not the call: the operator's own compile
    of a source with an absolute import still resolves it from disk."""
    lib = tmp_path / "lib.rvl"
    lib.write_text(_LIB, encoding="utf-8")
    source = _TURN.format(path=str(lib))
    trusted = compile_source(source, "<turn>.rvl", manifest=_base_ir(),
                             modules=None, profile=None)
    assert [c["name"] for c in trusted["components"]] == ["T"]
    with pytest.raises(RevlError) as err:
        compile_source(source, "<turn>.rvl", manifest=_base_ir(), modules=None,
                       profile=AdmissionProfile.untrusted_author(()))
    assert _RULE in str(err.value)


def test_an_in_memory_module_at_an_absolute_path_still_resolves(tmp_path):
    """A path the caller also supplies in-memory resolves out of the sources
    map, so it names nothing on the filesystem and there is nothing to
    confine."""
    at = str(tmp_path / "lib.rvl")
    assert not Path(at).exists()
    ir = compile_source(_TURN.format(path=at), "<turn>.rvl",
                        manifest=_base_ir(), modules={at: _LIB},
                        profile=AdmissionProfile.untrusted_author(()))
    assert [c["name"] for c in ir["components"]] == ["T"]


def test_the_search_path_spelling_still_resolves(tmp_path, monkeypatch):
    """An installed module (a `REVL_IMPORT_PATH` entry, or the stdlib) is
    reached through the operator's search path by a relative spelling, which
    is not escaping and is untouched."""
    (tmp_path / "lib.rvl").write_text(_LIB, encoding="utf-8")
    monkeypatch.setenv("REVL_IMPORT_PATH", str(tmp_path))
    ir = compile_source(_TURN.format(path="lib.rvl"), "<turn>.rvl",
                        manifest=_base_ir(), modules=None,
                        profile=AdmissionProfile.untrusted_author(()))
    assert [c["name"] for c in ir["components"]] == ["T"]


def test_a_file_read_from_disk_resolves_its_own_upward_import(tmp_path):
    """A `.rvl` on disk was put there by a human; its `../lib.rvl` is the
    operator's composition layout and compiles even under the profile."""
    (tmp_path / "lib.rvl").write_text(_LIB, encoding="utf-8")
    root = tmp_path / "app" / "app.rvl"
    root.parent.mkdir()
    root.write_text(_TURN.format(path="../lib.rvl"), encoding="utf-8")
    ir = compile_files([str(root)],
                       profile=AdmissionProfile.untrusted_author(()))
    assert [c["name"] for c in ir["components"]] == ["T"]


def test_the_transport_predicate_is_the_compiler_predicate():
    """One definition of "escaping", so the two doors cannot drift apart."""
    for path, escaping in [("/etc/passwd", True), ("..", True),
                           ("../x.rvl", True), ("a/../../x.rvl", True),
                           ("lib.rvl", False), ("a/../lib.rvl", False),
                           ("stdlib/str.rvl", False)]:
        assert server._escaping_use(path) is escaping, path
        assert _compiler.escaping_use_path(path) is escaping, path


# ------------------------------------------ relative sources keys stand in

_ON_DISK = (
    "extern pure fn leak(x: Str) -> Str = @py { return x }\n"
    "service S { fn f() -> Int }\n"
    "component OnDisk provides s: S { provide s { fn f() = 7 } }\n"
)


def test_a_relative_sources_key_stands_in_for_the_file(tmp_path, monkeypatch):
    """The submitted text is what compiles, never the on-disk file of the same
    relative name."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ondisk.rvl").write_text(_ON_DISK, encoding="utf-8")
    ir = compile_files(["ondisk.rvl"],
                       sources={"ondisk.rvl": "component Bogus { }\n"})
    assert [c["name"] for c in ir["components"]] == ["Bogus"]


def test_gate_service_admits_the_submitted_text_not_the_on_disk_file(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "ondisk.rvl").write_text(_ON_DISK, encoding="utf-8")
    verdict = json.loads(gate_service.admit(
        json.dumps({"ondisk.rvl": "component Bogus { }\n"}), ""))
    assert verdict["ok"] is True, verdict
    assert verdict["admitted"] == ["Bogus"]


# ------------------------------------------ the live per-turn door

@needs_cordis
def test_a_live_per_turn_admit_returns_the_refusal_as_a_verdict(tmp_path):
    """The verdict is the observable: `Session.admit` hands the refusal back
    as data, and that data names the rule rather than the file."""
    from revl.mcp.session import Session

    session = Session()
    try:
        session.load(_base_ir())
        verdict = session.admit(_TURN.format(path="/etc/passwd"))
        assert verdict.admitted is False
        assert _RULE in (verdict.message or "")
        assert "revl has no" not in (verdict.message or "")
    finally:
        try:
            session.unload()
        except Exception:  # noqa: BLE001 — teardown only
            pass
