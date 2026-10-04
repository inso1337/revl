"""revl_edit checks the PATCHED buffer's imports against the path jail (#1709).

The pre-dispatch jail reads the `use` paths of transport-carried source before
any handler runs, but for `revl_edit` all it sees is each edit's `replacement`
on its own, and only when that string parses as a program by itself. The
compile reads the patched buffer. So an import split across two edits reached
the compiler unchecked.

Under the default untrusted authoring the compile's own confinement refused
such an import anyway, so the jail was the only guard only where the operator
runs `--author-trust trusted` (`host_code`): there the split edit compiled, and
the compile read the file outside the sanctioned roots. The jail does not
depend on authoring trust, so these tests run trusted, where the gap was real,
send exactly that split, inline and in a loaded file, and require a refusal
with nothing compiled. They also pin what must keep working: an in-root import
an edit adds, and an operator file's own out-of-root import.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import edit as E  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session tools need the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh`")

SERVICE = "service Tool { fn describe() -> Str }\n"
COMPONENT = ("component T provides tool: Tool {\n"
             '  provide tool { fn describe() = "in" }\n'
             "}\n")
OUTSIDE = 'pub fn leak() -> Str { return "outside" }\n'


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _describe() -> str:
    return _call("revl_call", {"key": "tool", "method": "describe"})["result"]


def _split_use(path: str, names: str = "{ leak }") -> list[dict]:
    """`use "<path>" <names>` written by two edits, neither of which lexes, let
    alone parses, on its own: the first opens the string, the second closes it."""
    head = 'use "' + path[:2]
    return [{"range": [0], "replacement": head},
            {"range": [len(head)], "replacement": path[2:] + '" ' + names + "\n"}]


def _jail_refusal(result: dict) -> None:
    assert result["ok"] is False and result["swapped"] is False, result
    message = result["diagnostics"][0]["message"]
    assert "sanctioned root" in message, message
    assert "nothing was compiled" in message, message


@pytest.fixture
def jailed(tmp_path, monkeypatch):
    """A sanctioned root `tmp/root` with the composition in it, and a revl
    module OUTSIDE the root that a split edit tries to import."""
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside.rvl").write_text(OUTSIDE, encoding="utf-8")
    svc, main = root / "svc.rvl", root / "main.rvl"
    svc.write_text(SERVICE, encoding="utf-8")
    main.write_text(COMPONENT, encoding="utf-8")
    monkeypatch.chdir(root)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(root),), host_code=True)
    try:
        yield {"root": root, "files": [str(svc), str(main)],
               "outside": str(tmp_path / "outside.rvl")}
    finally:
        if server_mod.SESSION.loaded:
            server_mod.SESSION.unload()
        server_mod.SESSION.draft = None
        server_mod.AUTHORING = old


# ------------------------------------------------ the gap, inline and in files


@needs_runtime
def test_a_split_edit_cannot_import_outside_the_roots_from_a_file(jailed):
    _call("revl_load", {"files": jailed["files"]})
    main = jailed["files"][1]
    edits = _split_use("../outside.rvl") + [
        {"anchor": '"in"', "replacement": "leak()"}]
    result = _call("revl_edit", {"target": main, "edits": edits})
    _jail_refusal(result)
    assert "../outside.rvl" in result["diagnostics"][0]["message"]
    assert _describe() == "in"


# Inline source resolves a `use` from disk only when the compile has an in-memory
# module set to read first (`compile_source` then goes through `compile_files`),
# so the inline cases load with one.
INLINE_MODULES = {"lib.rvl": 'pub fn label() -> Str { return "lib" }\n'}


@needs_runtime
def test_a_split_edit_cannot_import_an_absolute_path_inline(jailed):
    loaded = _call("revl_load", {"source": SERVICE + COMPONENT,
                                 "modules": INLINE_MODULES})
    assert loaded["ok"] is True, loaded
    edits = _split_use(jailed["outside"]) + [
        {"anchor": '"in"', "replacement": "leak()"}]
    result = _call("revl_edit", {"edits": edits})
    _jail_refusal(result)
    assert _describe() == "in"


@needs_runtime
def test_an_edit_completing_an_import_with_buffer_text_is_refused(jailed):
    """No edit writes `use` at all: the buffer already holds the `use`, and an
    anchor edit only changes its path."""
    loaded = _call("revl_load", {"source": 'use "lib.rvl" { label }\n'
                                           + SERVICE + COMPONENT,
                                 "modules": INLINE_MODULES})
    assert loaded["ok"] is True, loaded
    result = _call("revl_edit", {"edits": [
        {"anchor": '"lib.rvl" { label }', "replacement": '"../outside.rvl" { leak }'},
        {"anchor": '"in"', "replacement": "leak()"}]})
    _jail_refusal(result)
    assert _describe() == "in"


def test_a_patched_buffer_that_does_not_lex_is_refused_before_compiling():
    """Fail closed: when the patched text cannot be read for its imports, it
    is refused, not handed to a compile that would read them itself."""
    vs = {"source": 'use "unterminated\n' + SERVICE, "modules": {}}
    with pytest.raises(E.EditError, match="does not lex"):
        E.check_imports(vs, [("source", "source")])


# ------------------------------------------------ what keeps working


@needs_runtime
def test_an_in_root_import_an_edit_adds_still_compiles(jailed):
    lib = jailed["root"] / "lib.rvl"
    lib.write_text('pub fn label() -> Str { return "lib" }\n', encoding="utf-8")
    _call("revl_load", {"files": jailed["files"]})
    result = _call("revl_edit", {"target": jailed["files"][1], "edits": [
        {"range": [0], "replacement": 'use "lib.rvl" { label }\n'},
        {"anchor": '"in"', "replacement": "label()"}]})
    assert result["ok"] is True and result["swapped"] is True, result
    assert _describe() == "lib"


@needs_runtime
def test_an_operators_own_out_of_root_import_stays_editable(jailed, tmp_path):
    """The operator's file on disk already imports `../outside.rvl`; editing
    something else in that file does not newly refuse the import."""
    main = Path(jailed["files"][1])
    main.write_text('use "../outside.rvl" { leak }\n' + COMPONENT, encoding="utf-8")
    server_mod.set_authoring_trust(roots=(str(jailed["root"]), str(tmp_path)))
    assert _call("revl_load", {"files": jailed["files"]})["ok"] is True
    server_mod.set_authoring_trust(roots=(str(jailed["root"]),))
    result = _call("revl_edit", {"target": str(main), "edits": [
        {"anchor": '"in"', "replacement": "leak()"}]})
    assert result["ok"] is True and result["swapped"] is True, result
    assert _describe() == "outside"
