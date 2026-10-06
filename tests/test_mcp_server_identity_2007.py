"""The MCP server reports the compiler that answered (issue #2007).

A client can pin a revision, drive a server, and have no way to detect that the
server is a *different* compiler: `revl_state` and the `initialize` result
carried nothing that identified the process that answered, so a tool answering
from the wrong tree was indistinguishable from one answering correctly. These
pin the identity in both surfaces, on the not-loaded branch, and the client
helper that turns a pin mismatch into a hard error naming both values.

The two-revision test does not take the server's word for what it is: it copies
the package into two throwaway checkouts, commits them differently, starts a
server from each, and compares what each reported against that checkout's own
`git rev-parse HEAD`. The harness also prints where it imported `revl` from, so
a stray editable install shadowing the copy is a failure and not a false pass.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import cert  # noqa: E402
from revl.mcp import identity as ident  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the loaded branch needs the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh`")

CACHE = """
service Cache { fn size() -> Int }
component MemCache provides cache: Cache {
  let store = effect Map.new() undo store.drop()
  provide cache { fn size() = 0 }
}
"""

_HARNESS = r'''
import json
import revl
from revl.mcp.server import handle

init = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
state = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "revl_state", "arguments": {}}})
print(json.dumps({
    "package": revl.__file__,
    "serverInfo": init["result"]["serverInfo"],
    "state": state["result"]["structuredContent"],
}))
'''


def _initialize() -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {}})
    return response["result"]


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _git(where: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(where), *arguments], check=True,
                   capture_output=True, text=True)


def _git_head(where: Path) -> str:
    done = subprocess.run(["git", "-C", str(where), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _checkout(where: Path, message: str) -> Path:
    """A throwaway checkout of the running package, committed, at `where`."""
    source = where / "src"
    source.mkdir(parents=True)
    shutil.copytree(ident.package_root(), source / "revl",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    _git(where, "init", "-q")
    _git(where, "config", "user.email", "lane@example.invalid")
    _git(where, "config", "user.name", "lane")
    _git(where, "add", "-A")
    _git(where, "commit", "-qm", message)
    return source


def _serve(source: Path) -> dict:
    """Start a server from the package under `source` and read its answers."""
    done = subprocess.run(
        [sys.executable, "-c", _HARNESS], check=True, capture_output=True,
        text=True, env={**os.environ, "PYTHONPATH": str(source)})
    return json.loads(done.stdout.strip().splitlines()[-1])


# ------------------------------------------------- the identity, on the wire

def test_initialize_names_the_compiler_that_answered():
    info = _initialize()["serverInfo"]
    head = cert.tree_commit(ident.package_root())
    if head is None:                      # an installed package, not a checkout
        assert info["revision"] == ident.build_id()
        assert info["revision"].startswith("revl-")
    else:
        assert info["revision"] == head
    digest = info["source_digest"]
    assert len(digest) == 64 and int(digest, 16) >= 0


def test_state_carries_the_same_identity_with_nothing_loaded():
    """The acceptance's third clause: the not-loaded payload has it too, so a
    client can assert the server before it loads anything."""
    info = _initialize()["serverInfo"]
    state = _call("revl_state", {})
    assert state["loaded"] is False        # nothing loaded: the branch under test
    assert state["revision"] == info["revision"]
    assert state["source_digest"] == info["source_digest"]


@needs_runtime
def test_state_carries_the_same_identity_with_a_composition_loaded():
    """The loaded branch, not just the empty one: identity is a property of the
    answering process, so loading does not replace it."""
    info = _initialize()["serverInfo"]
    try:
        assert _call("revl_load", {"source": CACHE})["ok"] is True
        state = _call("revl_state", {})
        assert state["loaded"] is True
        assert state["revision"] == info["revision"]
        assert state["source_digest"] == info["source_digest"]
    finally:
        _call("revl_unload", {})


def test_a_pin_mismatch_is_a_hard_error_naming_both_values():
    info = _initialize()
    got = info["serverInfo"]["revision"]
    pinned = "0" * 40
    with pytest.raises(ident.IdentityMismatch) as raised:
        ident.assert_identity(info, revision=pinned)
    message = str(raised.value)
    assert pinned in message and got in message
    assert not isinstance(raised.value, Warning)
    assert raised.value.code == "MCP-IDENTITY"

    # a server reporting nothing at all is a mismatch too, and says so
    with pytest.raises(ident.IdentityMismatch) as silent:
        ident.assert_identity({"name": "revl", "version": "2.0"}, revision=got)
    assert got in str(silent.value)

    # the pin that matches is accepted, from either surface
    assert ident.assert_identity(info, revision=got)["revision"] == got
    assert ident.assert_identity(_call("revl_state", {}),
                                 revision=got)["revision"] == got
    # the digest is compared only when the caller pins one
    assert ident.assert_identity(info, revision=got,
                                 source_digest=info["serverInfo"]["source_digest"])
    with pytest.raises(ident.IdentityMismatch):
        ident.assert_identity(info, revision=got, source_digest="0" * 64)


def test_the_discover_result_can_be_pinned_too():
    """`server/discover` carries `serverInfo` under `_meta` and nowhere else.

    A client that pins off the modern introspection result must recognise the
    server, not read `None` off a top level that never carried it. The result
    here is the transport's own, not a hand-written shape.
    """
    from revl.mcp import server as server_module
    from revl.mcp.http_transport import (META_SERVER_INFO, HttpTransport,
                                         ServerDispatcher)

    transport = types.SimpleNamespace(dispatcher=ServerDispatcher(server_module))
    discover = HttpTransport._discover(transport, 1)
    assert "serverInfo" not in discover["result"]       # nowhere at the top
    carried = discover["result"]["_meta"][META_SERVER_INFO]
    assert carried["revision"] and carried["source_digest"]

    pinned = ident.identity()
    got = ident.assert_identity(discover, revision=pinned["revision"],
                                source_digest=pinned["source_digest"])
    assert got["revision"] == pinned["revision"]

    # and a discover result from a tree this is not is still refused loudly
    with pytest.raises(ident.IdentityMismatch) as raised:
        ident.assert_identity(discover, revision="0" * 40)
    assert "0" * 40 in str(raised.value)
    assert pinned["revision"] in str(raised.value)

    # the key is spelled twice by design (a stdio server must not import the
    # HTTP transport to read one string); the two spellings must not drift
    assert ident.META_SERVER_INFO == META_SERVER_INFO


def test_the_composed_wire_names_the_compiler_too():
    """`revl mcp serve --mcp <composition>` serves a composition's own tools and
    advertises no `revl_state`, so its `initialize` is the only surface a client
    can pin: the name says which composition, the identity which compiler.
    """
    from revl import compile_source
    from revl.mcp.composed import ComposedServer

    class _Session:                      # the projection needs the IR, not a runtime
        ir = compile_source(CACHE)

        def call(self, key, method, args):
            raise AssertionError("initialize must not reach the composition")

    info = ComposedServer(_Session(), composition="app").handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})["result"]
    assert info["serverInfo"]["name"] == "revl:app"     # still names the composition

    pinned = ident.identity()
    got = ident.assert_identity(info, revision=pinned["revision"],
                                source_digest=pinned["source_digest"])
    assert got["revision"] == pinned["revision"]
    assert got["source_digest"] == pinned["source_digest"]


def test_a_server_started_from_two_revisions_reports_two_identities(tmp_path):
    first = _checkout(tmp_path / "one", "revision one")
    second = _checkout(tmp_path / "two", "revision one")
    marker = second / "revl" / "mcp" / "identity.py"
    marker.write_text(marker.read_text() + "\n# a second revision\n")
    _git(tmp_path / "two", "commit", "-aqm", "revision two")

    one, two = _serve(first), _serve(second)
    head_one = _git_head(tmp_path / "one")
    head_two = _git_head(tmp_path / "two")
    assert head_one != head_two
    for payload, source, head in ((one, first, head_one), (two, second, head_two)):
        # the copy, not an editable install of this worktree
        assert Path(payload["package"]).is_relative_to(source)
        assert payload["serverInfo"]["revision"] == head
        assert payload["state"]["revision"] == head
        assert (payload["state"]["source_digest"]
                == payload["serverInfo"]["source_digest"])
    assert one["serverInfo"]["revision"] != two["serverInfo"]["revision"]
    assert (one["serverInfo"]["source_digest"]
            != two["serverInfo"]["source_digest"])


# ----------------------------------------------- what the digest is a digest of

def test_the_digest_is_over_the_source_and_moves_with_it(tmp_path):
    root = tmp_path / "revl"
    root.mkdir()
    (root / "a.py").write_text("A = 1\n")
    (root / "b.py").write_text("B = 1\n")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\x00\x01")

    first = ident.source_digest(root)
    assert ident.source_digest(root) == first        # a compiled tree agrees
    (root / "b.py").write_text("B = 2\n")
    edited = ident.source_digest(root)
    assert edited != first
    (root / "c.py").write_text("C = 1\n")
    added = ident.source_digest(root)
    assert added != edited
    (root / "c.py").rename(root / "d.py")
    assert ident.source_digest(root) != added        # a rename is not the same
    (root / "d.py").unlink()
    assert ident.source_digest(root) == edited

    if cert.tree_commit(root) is None:               # no commit to report
        assert ident.revision(root) == ident.build_id()


def test_identity_of_an_explicit_root_is_the_running_one():
    assert ident.identity(ident.package_root()) == ident.identity()
