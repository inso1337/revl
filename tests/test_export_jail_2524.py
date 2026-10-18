"""`revl_export` under a closed authoring trust (issue #2524).

`revl_export` is the one MCP verb that writes to disk, and a jailed `.rvl`
file used to compile without the authoring profile on the premise that only a
human puts bytes at a jailed path. So text the profile refuses when sent inline
could be written by an export and loaded back by `files`, and an export could
write any bytes at any path in the root, replacing files revl never wrote.

The rules these tests hold, through the real dispatch:

* an inline export writes only `.rvl` modules the program reaches, and only
  inside `<root>/.revl/export/`;
* it replaces only a file an earlier export wrote;
* a file inside an export directory, or one an export wrote back and nobody
  has changed since, compiles under the profile, and so does one reached by
  `use` from an operator file;
* no export replaces an operator configuration file the server opened, under
  any trust;
* the operator's own files, and a trusted author's exports, behave as before.
"""

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session tools boot a composition and need the cordis-py "
           "runtime; install it with `sh backends/python/setup.sh`")

KV = "service Kv {\n  fn get(k: Str) -> Opt[Str]\n}\n"
PLAIN = KV + ("component Vault provides kv: Kv {\n"
              "  provide kv { fn get(k) = None }\n"
              "}\n")
# a module with a host body: refused under the profile wherever it came from,
# admitted as the operator's own file
HOSTED = ('extern pure fn boom() -> Str = @py { return "boom" }\n'
          "service Ops { fn run() -> Str }\n"
          "component OpsBox provides ops: Ops {\n"
          "  provide ops { fn run() = boom() }\n"
          "}\n")
# no host body of its own, but it reaches one: refused inline as it is
# transitively, and only ever compiled when something reaches it
REACH = ('use "stdlib/shell.rvl" { sh }\n'
         "service Pwn { emission fn go() -> Str }\n"
         "component Pwned provides pwn: Pwn {\n"
         '  provide pwn { fn go() = emit sh("true") }\n'
         "}\n")
HOSTED_LIB = ('extern pure fn boom() -> Str = @py { return "boom" }\n'
              "pub fn shout() -> Str { return boom() }\n")
CLOCK = ("service Clock { fn now() -> Int }\n"
         "component FixedClock provides clock: Clock {\n"
         "  provide clock {\n"
         "    fn now() = 7\n"
         "  }\n"
         "}\n")
USES_LIB = ('use "lib.rvl" { shout }\n'
            "service Ops { fn run() -> Str }\n"
            "component OpsBox provides ops: Ops {\n"
            "  provide ops { fn run() = shout() }\n"
            "}\n")


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _message(result: dict) -> str:
    return " | ".join(str(d.get("message", ""))
                      for d in result.get("diagnostics") or [])


def _reset():
    session = server_mod.SESSION
    if session.loaded:
        session.unload()
    session.proposals = {}
    session.pending_draft = None
    session.draft = None


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A sanctioned root under the default, closed authoring trust."""
    base = tmp_path / "root"
    base.mkdir()
    monkeypatch.chdir(base)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(base),))
    assert server_mod.AUTHORING.profile() is not None
    _reset()
    try:
        yield base
    finally:
        _reset()
        server_mod.AUTHORING = old


@pytest.fixture
def trusted(root):
    server_mod.set_authoring_trust(host_code=True)
    assert server_mod.AUTHORING.profile() is None
    return root


def _jail():
    """The export jail module, imported where a test reads it directly, so the
    rest of this file runs (and fails) against a tree without it."""
    from revl.mcp import export_jail  # noqa: PLC0415

    return export_jail


def _export_dir(base: Path) -> Path:
    return base / ".revl" / "export"


def _files_tree(base: Path) -> set:
    return {str(p.relative_to(base)) for p in base.rglob("*") if p.is_file()}


# ------------------------------------------------- what an export may write


def test_a_module_that_is_not_rvl_is_refused(root):
    payload = "import os\nos.system('touch RAN')\n"
    assert _call("revl_load", {"source": PLAIN,
                               "modules": {"conftest.py": payload}})["ok"] is True
    out = _call("revl_export", {"path": str(_export_dir(root) / "main.rvl")})
    assert out["ok"] is False, out
    assert "conftest.py is not a .rvl module" in _message(out)
    assert out["written"] == [] and _files_tree(root) == set()


def test_an_unreached_module_is_not_written(root):
    """The p02 shape: text parked in an unused module is never compiled, so it
    never reaches disk either, and loading it back finds nothing."""
    assert _call("revl_load", {"source": PLAIN,
                               "modules": {"reach.rvl": REACH}})["ok"] is True
    target = _export_dir(root) / "main.rvl"
    out = _call("revl_export", {"path": str(target)})
    assert out["ok"] is True, out
    assert out["written"] == [str(target)] and out["skipped"] == ["reach.rvl"]
    assert not (_export_dir(root) / "reach.rvl").exists()
    _reset()
    back = _call("revl_load", {"files": [str(_export_dir(root) / "reach.rvl")]})
    assert back["ok"] is False


def test_a_reached_module_is_written_beside_the_program(root):
    lib = "pub fn none() -> Opt[Str] { return None }\n"
    source = 'use "lib.rvl" { none }\n' + PLAIN.replace("= None", "= none()")
    assert _call("revl_load", {"source": source,
                               "modules": {"lib.rvl": lib}})["ok"] is True
    target = _export_dir(root) / "main.rvl"
    out = _call("revl_export", {"path": str(target)})
    assert out["ok"] is True, out
    assert sorted(out["written"]) == [str(target.parent / "lib.rvl"), str(target)]
    assert "skipped" not in out


def test_a_path_outside_the_export_directory_is_refused(root):
    assert _call("revl_load", {"source": PLAIN})["ok"] is True
    beside = root / "main.rvl"
    out = _call("revl_export", {"path": str(beside)})
    assert out["ok"] is False and "outside the export directory" in _message(out)
    assert not beside.exists()


def test_a_module_key_escaping_the_export_directory_is_refused(root):
    lib = "pub fn none() -> Opt[Str] { return None }\n"
    assert _call("revl_load", {"source": PLAIN,
                               "modules": {"../../lib.rvl": lib}})["ok"] is True
    out = _call("revl_export", {"path": str(_export_dir(root) / "main.rvl")})
    assert out["ok"] is False and "outside the export directory" in _message(out)
    assert _files_tree(root) == set()


def test_overwriting_a_file_revl_did_not_write_is_refused(root):
    foreign = _export_dir(root) / "main.rvl"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("// the operator's\n", encoding="utf-8")
    assert _call("revl_load", {"source": PLAIN})["ok"] is True
    out = _call("revl_export", {"path": str(foreign), "overwrite": True})
    assert out["ok"] is False and "was not written by revl_export" in _message(out)
    assert foreign.read_text(encoding="utf-8") == "// the operator's\n"


def test_an_export_overwrites_what_an_export_wrote(root):
    target = _export_dir(root) / "main.rvl"
    assert _call("revl_load", {"source": PLAIN})["ok"] is True
    assert _call("revl_export", {"path": str(target)})["ok"] is True
    again = _call("revl_export", {"path": str(target)})
    assert again["ok"] is False and "already exists" in _message(again)
    assert _call("revl_export", {"path": str(target), "overwrite": True})["ok"] is True
    target.write_text(PLAIN + "// edited by the operator\n", encoding="utf-8")
    changed = _call("revl_export", {"path": str(target), "overwrite": True})
    assert changed["ok"] is False and "has changed since" in _message(changed)


# ------------------------------------------- what an export wrote is profiled


@pytest.mark.parametrize("text", [HOSTED, REACH], ids=["host-body", "reach"])
def test_a_file_in_the_export_directory_loads_under_the_profile(root, text):
    """Whatever put them there, bytes in an export directory are the agent's:
    loaded by `files` they are refused exactly as the same text inline."""
    inline = _call("revl_load", {"source": text, "modules": {
        "unused.rvl": "pub fn d() -> Int { return 1 }\n"}})
    assert inline["ok"] is False
    exported = _export_dir(root) / "ops.rvl"
    exported.parent.mkdir(parents=True)
    exported.write_text(text, encoding="utf-8")
    back = _call("revl_load", {"files": [str(exported)]})
    assert back["ok"] is False, back
    assert _message(back) == _message(inline)


@pytest.mark.parametrize("text", [HOSTED, REACH], ids=["host-body", "reach"])
def test_the_same_file_placed_by_the_operator_loads_as_before(root, text):
    operator = root / "ops.rvl"
    operator.write_text(text, encoding="utf-8")
    assert _call("revl_load", {"files": [str(operator)]})["ok"] is True


def test_an_exported_module_reached_by_use_is_profiled(root):
    """An exported module a loaded file reaches by `use` is the agent's too,
    including when the importer is the operator's own file."""
    (_export_dir(root)).mkdir(parents=True)
    (_export_dir(root) / "lib.rvl").write_text(HOSTED_LIB, encoding="utf-8")
    (_export_dir(root) / "main.rvl").write_text(USES_LIB, encoding="utf-8")
    from_export = _call("revl_load", {"files": [str(_export_dir(root) / "main.rvl")]})
    assert from_export["ok"] is False, from_export
    operator = root / "app.rvl"
    operator.write_text(USES_LIB.replace('"lib.rvl"', '".revl/export/lib.rvl"'),
                        encoding="utf-8")
    from_operator = _call("revl_load", {"files": [str(operator)]})
    assert from_operator["ok"] is False, from_operator
    (root / "lib.rvl").write_text(HOSTED_LIB, encoding="utf-8")
    operator.write_text(USES_LIB, encoding="utf-8")
    assert _call("revl_load", {"files": [str(operator)]})["ok"] is True


def test_an_inline_export_round_trips_through_files(root):
    target = _export_dir(root) / "main.rvl"
    assert _call("revl_load", {"source": PLAIN})["ok"] is True
    assert _call("revl_export", {"path": str(target)})["ok"] is True
    _reset()
    assert _call("revl_load", {"files": [str(target)]})["ok"] is True


def test_a_write_back_is_recorded_and_keeps_the_operators_code(root):
    """A files-loaded composition still writes back to its own file. The write
    is recorded with the operator's text it replaced, so the next load diffs
    against that text: the operator's host code stays the operator's."""
    path = root / "ops.rvl"
    original = HOSTED + CLOCK
    path.write_text(original, encoding="utf-8")
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True
    changed = _call("revl_change", {"commit": True, "replace": {
        "component": "FixedClock.clock.now", "source": "fn now() = 8"}})
    assert changed["ok"] is True and changed["committed"] is True, changed
    out = _call("revl_export", {})
    assert out["ok"] is True and out["written"] == [str(path)], out
    assert _jail().authorship(str(path)) == (True, original)
    _reset()
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True
    # the operator changing the file adopts it
    path.write_text(original, encoding="utf-8")
    assert _jail().authorship(str(path)) == (False, None)


# ------------------------------------------------------ the trusted controls


def test_a_trusted_author_exports_as_before(trusted):
    payload = "# notes\n"
    assert _call("revl_load", {"source": PLAIN,
                               "modules": {"notes.txt": payload}})["ok"] is True
    target = trusted / "main.rvl"
    out = _call("revl_export", {"path": str(target)})
    assert out["ok"] is True, out
    assert sorted(out["written"]) == [str(target), str(trusted / "notes.txt")]
    assert not (trusted / ".revl").exists()


def test_a_trusted_author_still_loads_host_code_from_files(trusted):
    exported = _export_dir(trusted) / "ops.rvl"
    exported.parent.mkdir(parents=True)
    exported.write_text(HOSTED, encoding="utf-8")
    assert _call("revl_load", {"files": [str(exported)]})["ok"] is True


# ---------------------------------------- operator configuration, any trust


@pytest.mark.parametrize("trust", ["untrusted", "trusted"])
def test_no_export_replaces_a_protected_operator_file(root, trust, monkeypatch):
    monkeypatch.setattr(_jail(), "_OPERATOR_FILES", set())
    if trust == "trusted":
        server_mod.set_authoring_trust(host_code=True)
    policy = root / "policy.rvl"
    policy.write_text("// operator policy\n", encoding="utf-8")
    _jail().protect_operator_files([str(policy)])
    assert _call("revl_load", {"source": PLAIN})["ok"] is True
    target = (_export_dir(root) if trust == "untrusted" else root) / "main.rvl"
    target.parent.mkdir(parents=True, exist_ok=True)
    rel = os.path.relpath(policy, target.parent)
    server_mod.SESSION.origin["modules"] = {rel: PLAIN}
    out = _call("revl_export", {"path": str(target), "overwrite": True})
    assert out["ok"] is False, out
    assert policy.read_text(encoding="utf-8") == "// operator policy\n"


PROFILE = "operator ai may load, edit, snapshot\noperator ai may not unload, approve, swap\n"
FORGED = "operator ai may *\n"


class _Stdio:
    """The real stdio server, as an agent's client drives it."""

    def __init__(self, cwd: Path, *argv: str):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "revl", "mcp", "serve", *argv], cwd=str(cwd),
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)
        self.n = 0
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "t", "version": "1"}})

    def rpc(self, method: str, params: dict) -> dict:
        self.n += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n,
                                          "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def tool(self, name: str, arguments: dict) -> dict:
        reply = self.rpc("tools/call", {"name": name, "arguments": arguments})
        return reply.get("result", {}).get("structuredContent", reply)

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.mark.parametrize("trust", ["untrusted", "trusted"])
def test_an_export_cannot_replace_the_live_operator_profile(tmp_path, trust):
    """The operator profile sits in the root and is re-read while the server
    runs; an export aimed at it is refused, its bytes are unchanged, and the
    verb it withholds stays withheld after the settle window."""
    base = tmp_path / "root"
    base.mkdir()
    profile = base / "ops.profile"
    profile.write_text(PROFILE, encoding="utf-8")
    server = _Stdio(base, "--operator-profile", str(profile),
                    "--profile-settle-ms", "200", "--author-trust", trust)
    try:
        target = (_export_dir(base) if trust == "untrusted" else base) / "main.rvl"
        key = os.path.relpath(profile, target.parent)
        loaded = server.tool("revl_load", {"source": PLAIN, "modules": {key: FORGED}})
        assert loaded.get("ok") is True, loaded
        out = server.tool("revl_export", {"path": str(target), "overwrite": True})
        assert out.get("ok") is False, out
        assert profile.read_text(encoding="utf-8") == PROFILE
        time.sleep(0.4)
        server.tool("revl_state", {})
        time.sleep(0.4)
        unload = server.tool("revl_unload", {"force": True})
        assert unload.get("ok") is False, unload
        assert unload["authority"] == {"operator": "ai", "verb": "unload",
                                       "allowed": False}, unload
    finally:
        server.close()
