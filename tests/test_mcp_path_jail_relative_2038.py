"""A RELATIVE path argument is resolved against the sanctioned root, not the cwd.

Issue #2038. `_within_roots` resolved a relative path with `os.path.abspath`,
which bases it on the server process's cwd — an accident of how the operator
launched the process and, under `--root`, deliberately NOT the sanctioned tree.
So a relative path that WAS inside the sanctioned root was measured against the
wrong base, found outside, and refused with a message accusing the caller of a
path escape it never attempted.

The fix keeps the jail exactly as strong and moves the base: a relative path
argument is joined to the sanctioned roots (in the operator's declared order,
first join that lands inside the sanctioned set wins) before the jail's own
`realpath` comparison, and the resolved absolute path is what the handler then
opens. Every real escape is still refused, with the caller's own spelling:

  * `../` traversal out of the root,
  * a symlink inside the root pointing out of it,
  * an absolute path outside every root.

And the cwd is never the base — not even when the cwd itself is inside a root,
which is the case the old behaviour got wrong in the caller's favour.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

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

OUTSIDE_ROOT = "outside the operator-sanctioned root(s)"


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _refusal(arguments: dict) -> dict:
    payload = server_mod._jail_refusal(arguments)
    assert payload is not None, arguments
    message = payload["diagnostics"][0]["message"]
    assert OUTSIDE_ROOT in message, message
    return payload


@pytest.fixture
def sanctioned(tmp_path, monkeypatch):
    """A sanctioned root `tmp/root` holding the composition, and a cwd OUTSIDE
    it — the `--root` case, where the two candidate bases disagree."""
    root = tmp_path / "root"
    (root / "src" / "components").mkdir(parents=True)
    (root / "svc.rvl").write_text(SERVICE, encoding="utf-8")
    (root / "src" / "components" / "memory_store.rvl").write_text(
        COMPONENT, encoding="utf-8")
    (tmp_path / "outside.rvl").write_text(OUTSIDE, encoding="utf-8")
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(root),), host_code=True)
    try:
        yield {"root": root, "cwd": cwd, "outside": tmp_path / "outside.rvl"}
    finally:
        if server_mod.SESSION.loaded:
            server_mod.SESSION.unload()
        server_mod.SESSION.draft = None
        server_mod.AUTHORING = old


def _real(path) -> str:
    return os.path.realpath(str(path))


# --------------------------------------------------- the relative path, accepted


def test_a_relative_path_inside_the_root_is_accepted(sanctioned):
    arguments = {"files": ["src/components/memory_store.rvl"]}
    assert server_mod._jail_refusal(arguments) is None


def test_a_relative_path_is_absolutised_against_the_root(sanctioned):
    """Not merely accepted: rewritten, so the handler opens the file the caller
    named rather than a cwd-relative namesake (or nothing at all)."""
    arguments = {"files": ["src/components/memory_store.rvl"]}
    assert server_mod._jail_refusal(arguments) is None
    assert arguments["files"] == [
        _real(sanctioned["root"] / "src" / "components" / "memory_store.rvl")]


def test_a_dot_slash_relative_path_is_accepted(sanctioned):
    """The issue's second spelling, `./src/...`."""
    arguments = {"files": ["./src/components/memory_store.rvl"]}
    assert server_mod._jail_refusal(arguments) is None
    assert arguments["files"] == [
        _real(sanctioned["root"] / "src" / "components" / "memory_store.rvl")]


def test_a_relative_path_nested_in_a_snapshot_document_is_accepted(sanctioned):
    """`revl_restore`'s `snapshot.sources.files` is a path argument by another
    carrier, so it is resolved by the same rule."""
    arguments = {"snapshot": {"sources": {"files": ["svc.rvl"]}}}
    assert server_mod._jail_refusal(arguments) is None
    assert arguments["snapshot"]["sources"]["files"] == [
        _real(sanctioned["root"] / "svc.rvl")]


# ------------------------------------------------ the real escapes, still refused


def test_a_relative_path_that_traverses_out_is_still_refused(sanctioned):
    """Acceptance test 2 of #2038: this message keeps its current wording."""
    payload = _refusal({"files": ["../outside.rvl"]})
    assert "`../outside.rvl`" in payload["diagnostics"][0]["message"]


def test_a_relative_path_that_normalises_out_is_still_refused(sanctioned,
                                                              monkeypatch):
    """Acceptance test 4: `../..` from the root lands outside it."""
    monkeypatch.chdir(sanctioned["root"] / "src")
    _refusal({"files": ["../.."]})


def test_a_symlink_pointing_out_of_the_root_is_still_refused(sanctioned):
    link = sanctioned["root"] / "link.rvl"
    link.symlink_to(sanctioned["outside"])
    _refusal({"files": ["link.rvl"]})
    _refusal({"files": [str(link)]})


def test_an_absolute_path_outside_every_root_is_still_refused(sanctioned):
    payload = _refusal({"files": [str(sanctioned["outside"])]})
    assert f"`{sanctioned['outside']}`" in payload["diagnostics"][0]["message"]


def test_an_absolute_path_inside_the_root_still_works(sanctioned):
    """Acceptance test 3: the regression guard."""
    arguments = {"files": [_real(sanctioned["root"] / "svc.rvl")]}
    assert server_mod._jail_refusal(arguments) is None


def test_the_cwd_is_never_the_base_even_when_the_cwd_is_inside_a_root(
        sanctioned, monkeypatch):
    """`../plain.rvl` from `<root>/src` names `<tmp>/plain.rvl` — outside. The
    old cwd base resolved it to `<root>/plain.rvl`, INSIDE, and would have
    accepted a path the root rule does not admit."""
    (sanctioned["root"] / "plain.rvl").write_text(COMPONENT, encoding="utf-8")
    (sanctioned["root"].parent / "plain.rvl").write_text(COMPONENT, encoding="utf-8")
    monkeypatch.chdir(sanctioned["root"] / "src")
    _refusal({"files": ["../plain.rvl"]})


# ------------------------------------------------- several roots: the stated rule


def test_a_relative_path_under_a_later_root_is_accepted(sanctioned, tmp_path):
    """With several roots the path is joined to each in declaration order and
    the first join inside the sanctioned set wins — so an upward path that
    escapes the first root still lands in the second."""
    one, two = tmp_path / "one", tmp_path / "two"
    for where in (one, two):
        where.mkdir()
    (two / "only.rvl").write_text(COMPONENT, encoding="utf-8")
    server_mod.set_authoring_trust(roots=(str(one), str(two)), host_code=True)
    arguments = {"files": ["../two/only.rvl"]}
    assert server_mod._jail_refusal(arguments) is None
    assert arguments["files"] == [_real(two / "only.rvl")]


def test_a_relative_path_under_both_roots_resolves_to_the_first(
        sanctioned, tmp_path):
    second = tmp_path / "second"
    second.mkdir()
    for where in (sanctioned["root"], second):
        (where / "both.rvl").write_text(COMPONENT, encoding="utf-8")
    server_mod.set_authoring_trust(
        roots=(str(sanctioned["root"]), str(second)), host_code=True)
    arguments = {"files": ["both.rvl"]}
    assert server_mod._jail_refusal(arguments) is None
    assert arguments["files"] == [_real(sanctioned["root"] / "both.rvl")]


def test_a_relative_path_no_root_admits_is_refused(sanctioned, tmp_path):
    """`../../x` from either nested root is the same `tmp/x` — outside both."""
    one, two = tmp_path / "one" / "sub", tmp_path / "two" / "sub"
    for where in (one, two):
        where.mkdir(parents=True)
    server_mod.set_authoring_trust(roots=(str(one), str(two)), host_code=True)
    _refusal({"files": ["../../x.rvl"]})


# ------------------------------------------------------------- the whole call


@needs_runtime
def test_revl_load_accepts_a_relative_path_from_a_foreign_cwd(sanctioned):
    """The issue's reproduction, end to end: a relative path is not merely
    unrefused, the composition loads."""
    loaded = _call("revl_load", {"files": ["svc.rvl",
                                           "src/components/memory_store.rvl"]})
    assert loaded["ok"] is True, loaded
    assert loaded["loaded"] is True, loaded
