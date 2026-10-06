"""`revl_source` fails closed on a path it cannot open (issue #2031).

`revl_source` read a file it could not open as an *empty* file. With one
missing path that dropped the buffer and reattributed the cause to the content
(`no top-level declaration matches \\`X\\``); with two or more paths the missing
one was not reported at all and the call returned `{"ok": true}` — a silent
partial success. `revl_check` on the same path, on the same surface, correctly
refused with `file not found: <path>`.

These tests hold the issue's exits:

* `files=[missing]` is refused, the message names the path, and it is NOT the
  content error;
* `files=[good, missing]` is refused and names the missing path — never
  `ok: true`;
* a genuinely missing declaration in a readable file still raises the CONTENT
  error it raises today (the path signal is not allowed to swallow it);
* the happy path is unchanged;
* `revl_source` and `revl_check` name the same cause for the same path, so the
  two verbs cannot drift apart again.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

GOOD = """service Greeter {
  fn greet(name: Str) -> Str
}

component OnlyOne provides greeter: Greeter {
  provide greeter {
    fn greet(name: Str) -> Str = name
  }
}
"""

CONTENT_ERROR = "no top-level declaration matches"
PATH_ERROR = "file not found"


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _diagnostics(result: dict) -> list[dict]:
    return list(result.get("diagnostics") or [])


def _message(result: dict) -> str:
    return " ".join(str(d.get("message", "")) for d in _diagnostics(result))


@pytest.fixture
def workdir(tmp_path):
    """A readable file and a path that does not exist, both inside the roots
    the authoring trust check sanctions."""
    good = tmp_path / "good.rvl"
    good.write_text(GOOD, encoding="utf-8")
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    try:
        yield good, tmp_path / "nope.rvl"
    finally:
        server_mod.AUTHORING = old
        if server_mod.SESSION.loaded:
            server_mod.SESSION.unload()


def test_source_single_missing_path_is_a_path_error(workdir):
    """Exit 1: one missing path is refused by PATH, not by content."""
    _good, missing = workdir
    result = _call("revl_source", {"symbol": "OnlyOne", "files": [str(missing)]})

    assert result["ok"] is False, result
    assert PATH_ERROR in _message(result), result
    assert str(missing) in _message(result), result
    assert CONTENT_ERROR not in _message(result), result


def test_source_multi_file_with_missing_path_is_not_ok(workdir):
    """Exit 2: the silent partial success — a readable file answering while a
    missing one goes unreported — must not come back `ok: true`."""
    good, missing = workdir
    result = _call("revl_source",
                   {"symbol": "OnlyOne", "files": [str(good), str(missing)]})

    assert result.get("ok") is not True, result
    assert result["ok"] is False, result
    assert str(missing) in _message(result), result
    assert PATH_ERROR in _message(result), result


def test_source_missing_path_is_attributed_per_file(workdir):
    """The refusal names WHICH listed path failed, so a multi-file caller can
    tell them apart."""
    good, missing = workdir
    result = _call("revl_source",
                   {"symbol": "OnlyOne", "files": [str(good), str(missing)]})

    files = [d.get("file") for d in _diagnostics(result)]
    assert str(missing) in files, result
    assert str(good) not in files, result


def test_source_content_error_is_still_a_content_error(workdir):
    """Exit 3: a genuinely unknown symbol in a readable file keeps the content
    signal — the path check must not swallow it."""
    good, _missing = workdir
    result = _call("revl_source", {"symbol": "NoSuchThing", "files": [str(good)]})

    assert result["ok"] is False, result
    assert CONTENT_ERROR in _message(result), result
    assert PATH_ERROR not in _message(result), result


def test_source_happy_path_unchanged(workdir):
    """Exit 4: reading a declaration from a readable file still answers."""
    good, _missing = workdir
    result = _call("revl_source", {"symbol": "OnlyOne", "files": [str(good)]})

    assert result["ok"] is True, result
    assert result["symbol"] == "OnlyOne", result
    assert "component OnlyOne" in result["text"], result


def test_source_and_check_name_the_same_cause(workdir):
    """The two verbs resolve a path the same way, so they cannot drift: for the
    same missing path, the same diagnostic file/line/message."""
    _good, missing = workdir
    source = _call("revl_source", {"symbol": "OnlyOne", "files": [str(missing)]})
    check = _call("revl_check", {"files": [str(missing)]})

    def first_path_error(result):
        for diagnostic in _diagnostics(result):
            if PATH_ERROR in str(diagnostic.get("message", "")):
                return {key: diagnostic.get(key)
                        for key in ("file", "line", "message", "category")}
        return None

    from_source = first_path_error(source)
    assert from_source is not None, source
    assert from_source == first_path_error(check), (source, check)
