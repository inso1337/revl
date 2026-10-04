"""Trust for edited files follows the operator's text, declaration by
declaration (issue #1715).

`revl_edit` on a files-loaded composition, and `revl_swap {files, modules}`,
used to compile the whole composition under the untrusted-author profile as
soon as any file's text came over the transport. The operator's own externs
were then refused as if the agent had written them, so a composition with host
externs of its own could not be edited at all, and an edited operator file lost
its own upward `use`.

The boundary now: a declaration in transport-carried text that is identical to
one in the operator's text of the same file (the file on disk, inside the
sanctioned roots) is the operator's. Everything else in that text is the
agent's and gets the full profile. These tests hold both halves: what the
operator wrote stays usable, and nothing the agent writes gains anything.

Most of this is a pure compile decision through the one compiler door
(`server.compile_under_authoring`) and needs no runtime; the `revl_edit` end to
end tests boot one.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.errors import RevlError  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import compile_under_authoring, handle  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session tools need the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh`")

SERVICES = ("service Tool { fn describe() -> Str }\n"
            "service Ops { fn run() -> Str }\n")
# the operator's library: a host extern, a function that wraps it, a plain
# function, and a component of the operator's own that calls the extern
LIB = ('extern pure fn boom() -> Str = @py { return "boom" }\n'
       "pub fn shout() -> Str { return boom() }\n"
       'pub fn label() -> Str { return "v1" }\n'
       "component OpsBox provides ops: Ops {\n"
       "  provide ops { fn run() = boom() }\n"
       "}\n")
MAIN = ('use "lib.rvl" { label, shout }\n'
        "component T provides tool: Tool {\n"
        "  provide tool { fn describe() = label() }\n"
        "}\n")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """The operator's files in a sanctioned root, under the default untrusted
    authoring trust (the profile is on)."""
    root = tmp_path / "root"
    root.mkdir()
    for name, text in (("svc.rvl", SERVICES), ("lib.rvl", LIB), ("main.rvl", MAIN)):
        (root / name).write_text(text, encoding="utf-8")
    monkeypatch.chdir(root)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(root),))
    assert server_mod.AUTHORING.profile() is not None
    try:
        yield root
    finally:
        if server_mod.SESSION.loaded:
            server_mod.SESSION.unload()
        server_mod.SESSION.draft = None
        server_mod.AUTHORING = old


def _files(root, *names):
    return [str(root / n) for n in names] or [str(root / n) for n in
                                              ("svc.rvl", "lib.rvl", "main.rvl")]


ALL = ("svc.rvl", "lib.rvl", "main.rvl")


def _compile(root, overlay: dict, roots=ALL):
    """Compile the composition with `overlay` (file name -> text) sent as the
    transport-carried modules, as `revl_swap {files, modules}` and `revl_edit`
    do."""
    modules = {str(root / name): text for name, text in overlay.items()}
    return compile_under_authoring(None, [str(root / n) for n in roots],
                                   modules=modules)


def _refused(root, overlay, roots=ALL) -> str:
    with pytest.raises(RevlError) as caught:
        _compile(root, overlay, roots)
    return str(caught.value)


# ------------------------------------------------ the operator's text stays usable


def test_editing_a_file_beside_an_operator_extern_admits(tree):
    """Before: G8 on `boom`, an extern in a file the edit never touched."""
    ir = _compile(tree, {"main.rvl": MAIN.replace("= label()", '= "edited"')})
    assert {c["name"] for c in ir["components"]} == {"T", "OpsBox"}


def test_editing_the_extern_file_keeps_its_unchanged_extern_and_users(tree):
    """`boom`, `shout` and `OpsBox` are untouched; only `label` changed."""
    _compile(tree, {"lib.rvl": LIB.replace('"v1"', '"v2"')})


def test_an_operator_declaration_moved_down_the_file_is_still_the_operators(tree):
    moved = LIB.replace('pub fn label() -> Str { return "v1" }\n', "")
    moved = 'pub fn label() -> Str { return "v3" }\n\n\n' + moved
    _compile(tree, {"lib.rvl": moved})


def test_an_unchanged_operator_upward_use_keeps_operator_confinement(tree):
    """The operator's main.rvl imports `../shared.rvl`, outside the admitting
    directory. Editing another line of main.rvl does not turn that import into
    the agent's: same path, same file, the operator's layout."""
    (tree.parent / "shared.rvl").write_text(
        'pub fn far() -> Str { return "far" }\n', encoding="utf-8")
    operator_main = 'use "../shared.rvl" { far }\n' + MAIN.replace("= label()", "= far()")
    (tree / "main.rvl").write_text(operator_main, encoding="utf-8")
    _compile(tree, {"main.rvl": operator_main.replace("= far()", '= "edited"')})


def test_revl_swap_with_files_and_modules_follows_the_same_rule(tree):
    """The swap path is the same door: an overlay of main.rvl with the
    operator's externs elsewhere admits."""
    modules = {str(tree / "main.rvl"): MAIN.replace("= label()", '= "swapped"')}
    ir = compile_under_authoring(None, _files(tree, *ALL), modules=modules,
                                 manifest=None)
    assert "T" in {c["name"] for c in ir["components"]}


# ------------------------------------------------ nothing the agent writes gains


def test_an_added_host_extern_is_refused_by_name(tree):
    added = 'extern pure fn mine() -> Str = @py { return "x" }\n' + MAIN
    message = _refused(tree, {"main.rvl": added})
    assert "G8" in message or "untrusted-author profile" in message
    assert "`mine`" in message


def test_a_changed_operator_extern_is_refused_by_name(tree):
    changed = LIB.replace('return "boom"', 'return "changed"')
    message = _refused(tree, {"lib.rvl": changed})
    assert "untrusted-author profile" in message and "`boom`" in message


def test_agent_code_reaching_the_operators_extern_is_refused(tree):
    """`shout` is the operator's and wraps `boom`; the agent's changed
    component calling it is agent code reaching host code, transitively."""
    message = _refused(tree, {"main.rvl": MAIN.replace("= label()", "= shout()")})
    assert "reaches host-block extern `boom`" in message


def test_a_changed_operator_function_reaching_host_code_is_refused(tree):
    changed = LIB.replace('pub fn label() -> Str { return "v1" }',
                          "pub fn label() -> Str { return boom() }")
    message = _refused(tree, {"lib.rvl": changed})
    assert "reaches host-block extern `boom`" in message


def test_an_imported_file_rewritten_under_an_operator_root_is_swept(tree):
    """lib.rvl is not a root here, only imported by main.rvl, which the call
    leaves as the operator's. The agent rewrites lib's `label` to reach `boom`:
    the sweep starts from the agent's declarations wherever they are, so the
    operator root having nothing of the agent's does not hide it."""
    rewritten = LIB.replace('pub fn label() -> Str { return "v1" }',
                            "pub fn label() -> Str { return boom() }")
    message = _refused(tree, {"lib.rvl": rewritten}, roots=("svc.rvl", "main.rvl"))
    assert "reaches host-block extern `boom`" in message


def test_a_new_upward_use_in_an_edited_file_is_still_confined(tree):
    (tree.parent / "shared.rvl").write_text(
        'pub fn far() -> Str { return "far" }\n', encoding="utf-8")
    message = _refused(tree, {"main.rvl": 'use "../shared.rvl" { far }\n' + MAIN})
    assert "admission confinement" in message


def test_text_for_a_path_outside_the_roots_has_no_operator_text(tree):
    """An overlay of a file outside the sanctioned roots is never compared with
    that file: everything sent for it is the agent's."""
    outside = tree.parent / "outside.rvl"
    outside.write_text(LIB, encoding="utf-8")
    modules = {str(outside): LIB}
    with pytest.raises(RevlError) as caught:
        compile_under_authoring(None, [str(tree / "svc.rvl"), str(outside)],
                                modules=modules)
    assert "`boom`" in str(caught.value)


def test_positions_do_not_change_a_declarations_identity():
    from revl.operator_text import shape
    from revl.parser import Parser

    one = Parser("pub fn f() -> Int { return 1 }\n", "a.rvl").parse().fn_decls[0]
    two = Parser("\n\n\npub fn f() -> Int { return 1 }\n", "a.rvl").parse().fn_decls[0]
    other = Parser("pub fn f() -> Int { return 2 }\n", "a.rvl").parse().fn_decls[0]
    assert shape(one) == shape(two) != shape(other)


# ------------------------------------------------ revl_edit end to end


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@needs_runtime
def test_revl_edit_changes_a_composition_with_operator_externs(tree):
    assert _call("revl_load", {"files": _files(tree, *ALL)})["ok"] is True
    result = _call("revl_edit", {"target": str(tree / "lib.rvl"), "edits": [
        {"anchor": '"v1"', "replacement": '"v2"'}]})
    assert result["ok"] is True and result["swapped"] is True, result
    described = _call("revl_call", {"key": "tool", "method": "describe"})
    assert described["result"] == "v2"
    assert _call("revl_call", {"key": "ops", "method": "run"})["result"] == "boom"


@needs_runtime
def test_revl_edit_refuses_a_changed_extern_and_keeps_serving(tree):
    _call("revl_load", {"files": _files(tree, *ALL)})
    refused = _call("revl_edit", {"target": str(tree / "lib.rvl"), "edits": [
        {"anchor": 'return "boom"', "replacement": 'return "pwned"'}]})
    assert refused["ok"] is False and refused["swapped"] is False
    assert "`boom`" in refused["diagnostics"][0]["message"]
    assert _call("revl_call", {"key": "ops", "method": "run"})["result"] == "boom"
    assert os.path.exists(tree / "lib.rvl")
    assert (tree / "lib.rvl").read_text(encoding="utf-8") == LIB
