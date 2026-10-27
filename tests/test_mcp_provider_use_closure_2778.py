"""Issue #2778: an agent-supplied module may not take the place of a file an
operator `--provider` module `use`s.

`_refuse_provider_displacement` compared the agent's modules with the provider
files themselves. A provider's own imports are ordinary files on disk and are
not registered providers, while the composition compile resolves a provider's
`use` through its in-memory sources map first: a module keyed at the
dependency's path composed in the place of the file the provider depends on,
with the provider's text unchanged. The refusal now covers the provider's
transitive `use` closure, under any spelling of a dependency's path.

Every test here compiles only; none needs the cordis-py runtime.
"""

from __future__ import annotations

import os

import pytest

from revl.errors import RevlError
from revl.mcp import server
from revl.mcp.session import Session

DECLS = "service Tool { fn describe() -> Str }\n"
# the sibling the provider reaches, and the sibling's own dependency
LEAF = "pub fn leaf(p: Str) -> Str { return p }\n"
DEP = ('use "./leaf.rvl" { leaf }\n'
       "pub fn helper(p: Str) -> Str { return leaf(p) }\n")
# the operator's host code: a provider that reaches its own sibling module
PROVIDER = ('use "./dep.rvl" { helper }\n'
            "service Ops { emission[notify] fn stash(p: Str) }\n"
            "extern emission[notify] fn notify(p: Str) = @py { return }\n"
            "component OpsProvider provides ops: Ops {\n"
            "  provide ops { fn stash(p) { emit notify(helper(p)) } }\n"
            "}\n")
# the agent's text for a dependency: same signature, another behaviour, so it
# would compose silently in the sibling's place without the refusal
HIJACK_DEP = 'pub fn helper(p: Str) -> Str { return "mine" }\n'
HIJACK_LEAF = 'pub fn leaf(p: Str) -> Str { return "mine" }\n'
CANDIDATE = (DECLS + "component T provides tool: Tool {\n"
             '  provide tool { fn describe() = "t" }\n'
             "}\n")
# an agent module of its own, used by a candidate through its absolute path
LIB = 'pub fn helper() -> Str { return "h" }\n'


@pytest.fixture(autouse=True)
def _provider_registered_by_relative_path(monkeypatch, tmp_path):
    """The operator starts the server in `tmp_path` and registers the provider
    the way `revl mcp serve --provider prov.rvl` stores it: by the path as
    typed, relative to the cwd. Restored afterwards."""
    before = server.AUTHORING
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server, "SESSION", Session())
    (tmp_path / "prov.rvl").write_text(PROVIDER, encoding="utf-8")
    (tmp_path / "dep.rvl").write_text(DEP, encoding="utf-8")
    (tmp_path / "leaf.rvl").write_text(LEAF, encoding="utf-8")
    server.set_authoring_trust(host_code=False, granted=None,
                               providers={"prov.rvl": PROVIDER},
                               roots=(str(tmp_path),))
    yield
    server.AUTHORING = before


def _compile(modules: dict) -> dict:
    return server.compile_under_authoring(CANDIDATE, None, modules=modules)


@pytest.mark.parametrize("spelling", [
    lambda d, name: str(d / name),
    lambda d, name: os.path.join(".", name),
], ids=["absolute", "dot-slash"])
def test_a_module_at_a_provider_dependency_is_refused(tmp_path, spelling):
    key = spelling(tmp_path, "dep.rvl")
    with pytest.raises(RevlError) as refused:
        _compile({key: HIJACK_DEP})
    message = refused.value.message
    assert "provider" in message, message
    assert str(tmp_path / "prov.rvl") in message, message
    assert "./dep.rvl" in message, message
    assert key in message, message


def test_a_module_at_a_transitive_dependency_is_refused(tmp_path):
    """The relation is transitive: the provider imports dep.rvl, and dep.rvl
    imports leaf.rvl, so both are part of what the provider's own text reads."""
    key = str(tmp_path / "leaf.rvl")
    with pytest.raises(RevlError) as refused:
        _compile({key: HIJACK_LEAF})
    message = refused.value.message
    assert str(tmp_path / "prov.rvl") in message, message
    assert "./leaf.rvl" in message, message
    assert key in message, message


def test_a_symlink_to_a_provider_dependency_is_refused(tmp_path):
    link = tmp_path / "alias.rvl"
    os.symlink(tmp_path / "dep.rvl", link)
    with pytest.raises(RevlError) as refused:
        _compile({str(link): HIJACK_DEP})
    assert str(tmp_path / "prov.rvl") in refused.value.message


def test_the_refusal_compiles_nothing_with_the_agents_text(tmp_path):
    """The refusal comes before any compile, so no report is produced for a
    composition in which the agent's text stands in for the dependency."""
    report: dict = {}
    with pytest.raises(RevlError):
        server.compile_under_authoring(
            CANDIDATE, None, modules={str(tmp_path / "dep.rvl"): HIJACK_DEP},
            report=report)
    assert report == {}, report


def test_an_agent_module_of_its_own_still_compiles_by_absolute_path(tmp_path):
    """The refusal is scoped to the provider's own reads: a module of the
    agent's own, at a path the provider never reads, still compiles."""
    ir = _compile({str(tmp_path / "own.rvl"): LIB})
    assert ir["components"][0]["name"] == "T", ir["components"][0]


def test_a_dependency_resolved_through_the_search_path_is_refused(
        tmp_path, monkeypatch):
    """A provider's `use` with no local file resolves through
    `REVL_IMPORT_PATH`, and the compile reads that file: an agent module at
    the resolved path is the same read path and refuses too."""
    imports = tmp_path / "imports"
    imports.mkdir()
    shared = imports / "shared.rvl"
    shared.write_text(LEAF, encoding="utf-8")
    provider = ('use "shared.rvl" { leaf }\n'
                "service Ops2 { fn ping() -> Str }\n"
                "component OpsProvider2 provides ops2: Ops2 {\n"
                '  provide ops2 { fn ping() = leaf("p") }\n'
                "}\n")
    (tmp_path / "path-prov.rvl").write_text(provider, encoding="utf-8")
    server.set_authoring_trust(host_code=False, granted=None,
                               providers={"prov.rvl": PROVIDER,
                                          "path-prov.rvl": provider},
                               roots=(str(tmp_path),))
    monkeypatch.setenv("REVL_IMPORT_PATH", str(imports))

    with pytest.raises(RevlError) as refused:
        _compile({os.path.realpath(shared): HIJACK_LEAF})
    message = refused.value.message
    assert str(tmp_path / "path-prov.rvl") in message, message
    assert "shared.rvl" in message, message


def test_a_module_at_the_provider_file_is_still_refused(tmp_path):
    """The #2763 half is unchanged: the provider file itself is refused with
    the provider named."""
    with pytest.raises(RevlError) as refused:
        _compile({str(tmp_path / "prov.rvl"): PROVIDER})
    message = refused.value.message
    assert "provider" in message, message
    assert str(tmp_path / "prov.rvl") in message, message
