"""`revl_export` under a closed authoring trust (issue #2524).

`revl_export` is the one MCP verb that writes to disk, and a jailed `.rvl`
file used to compile without the authoring profile on the premise that only a
human puts bytes at a jailed path. So text the profile refuses when sent inline
could be written by an export and loaded back by `files`, and an export could
write any bytes at any path in the root, replacing files revl never wrote.

The rule these tests hold, through the real dispatch: an export writes only
under `<root>/.revl/export/` and never over a file revl did not write.

* an inline export writes only `.rvl` modules the program reaches, and only
  inside the export directory (under a closed trust);
* a files-loaded write-back lands in the file's copy there, under any trust,
  never over the operator's file;
* it replaces only a file an earlier export wrote, as the manifest records
  it; a missing or unreadable manifest refuses (fails closed);
* a file inside an export directory compiles under the profile by its path,
  named in `files` or reached by `use`;
* a rendered note cannot carry a line break the lexer does not see;
* no export creates or replaces an operator configuration file or an
  emergency-stop latch, under any trust (issue #2535);
* the operator's own files, and a trusted author's exports, behave as before.
"""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import unicodedata
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
EVIL = ('extern pure fn boom() -> Str = @py { return "host code ran" }\n'
        "service Ops { fn run() -> Str }\n"
        "component OpsBox provides ops: Ops { provide ops { fn run() = boom() } }\n")
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


def test_an_exported_module_reached_through_the_import_path_is_profiled(
        root, monkeypatch):
    """A `use` that resolves through `REVL_IMPORT_PATH` reaches an
    install-origin module; one an export wrote is still the agent's."""
    vendor = _export_dir(root) / "vendor"
    vendor.mkdir(parents=True)
    (vendor / "lib.rvl").write_text(HOSTED_LIB, encoding="utf-8")
    monkeypatch.setenv("REVL_IMPORT_PATH", str(vendor))
    operator = root / "app.rvl"
    operator.write_text(USES_LIB, encoding="utf-8")
    refused = _call("revl_load", {"files": [str(operator)]})
    assert refused["ok"] is False, refused


def test_a_case_alias_of_the_export_directory_is_still_it(root):
    exported = _export_dir(root) / "ops.rvl"
    exported.parent.mkdir(parents=True)
    exported.write_text(HOSTED, encoding="utf-8")
    alias = root / ".REVL" / "Export" / "ops.rvl"
    # on a case-sensitive filesystem the alias names nothing and the load
    # fails for that reason; on a case-folding one it is the exported file
    assert _call("revl_load", {"files": [str(alias)]})["ok"] is False


def test_a_write_back_through_a_symlink_lands_in_the_export_copy(root):
    real = root / "clock.rvl"
    real.write_text(CLOCK, encoding="utf-8")
    link = root / "alias.rvl"
    link.symlink_to(real.name)
    assert _call("revl_load", {"files": [str(link)]})["ok"] is True
    changed = _call("revl_change", {"commit": True, "replace": {
        "component": "FixedClock.clock.now", "source": "fn now() = 8"}})
    assert changed["ok"] is True, changed
    out = _call("revl_export", {})
    copy = _export_dir(root).resolve() / "clock.rvl"
    assert out["ok"] is True and out["written"] == [str(copy)], out
    assert link.is_symlink() and real.read_text(encoding="utf-8") == CLOCK
    assert "fn now() = 8" in copy.read_text(encoding="utf-8")


def test_an_export_copy_that_is_a_symbolic_link_is_refused(root):
    real = root / "clock.rvl"
    real.write_text(CLOCK, encoding="utf-8")
    _export_dir(root).mkdir(parents=True)
    (_export_dir(root) / "clock.rvl").symlink_to(real)
    assert _call("revl_load", {"files": [str(real)]})["ok"] is True
    assert _call("revl_change", {"commit": True, "replace": {
        "component": "FixedClock.clock.now", "source": "fn now() = 8"}})["ok"]
    out = _call("revl_export", {})
    assert out["ok"] is False, out
    assert real.read_text(encoding="utf-8") == CLOCK


def test_an_inline_export_round_trips_through_files(root):
    target = _export_dir(root) / "main.rvl"
    assert _call("revl_load", {"source": PLAIN})["ok"] is True
    assert _call("revl_export", {"path": str(target)})["ok"] is True
    _reset()
    assert _call("revl_load", {"files": [str(target)]})["ok"] is True


@pytest.mark.parametrize("trust", ["untrusted", "trusted"])
def test_a_write_back_lands_in_the_export_copy(root, trust):
    """A files-loaded composition writes its changed file to the file's copy
    under the export directory, never over the operator's file, under any
    trust. The copy is the agent's: loaded back it compiles under the profile,
    while the operator's file still loads as the operator's."""
    if trust == "trusted":
        server_mod.set_authoring_trust(host_code=True)
    path = root / "ops.rvl"
    original = HOSTED + CLOCK
    path.write_text(original, encoding="utf-8")
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True

    def change(value):
        changed = _call("revl_change", {"commit": True, "replace": {
            "component": "FixedClock.clock.now", "source": f"fn now() = {value}"}})
        assert changed["ok"] is True and changed["committed"] is True, changed

    change(8)
    copy = _export_dir(root).resolve() / "ops.rvl"
    out = _call("revl_export", {})
    assert out["ok"] is True and out["written"] == [str(copy)], out
    assert out["disk"]["inSync"] is True, out
    assert path.read_text(encoding="utf-8") == original
    change(9)  # the next export replaces the copy an export wrote
    assert _call("revl_export", {})["written"] == [str(copy)]
    copy.write_text(copy.read_text(encoding="utf-8") + "// mine\n",
                    encoding="utf-8")
    change(10)  # but not once someone else changed it
    refused = _call("revl_export", {})
    assert refused["ok"] is False and "has changed since" in _message(refused)
    assert path.read_text(encoding="utf-8") == original
    _reset()
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True
    _reset()
    # the copy is the agent's by its path, whatever the manifest says
    (_export_dir(root) / "manifest.json").unlink()
    back = _call("revl_load", {"files": [str(copy)]})
    assert back["ok"] is (trust == "trusted"), back


@pytest.mark.parametrize("damage", ["deleted", "garbage", "directory",
                                    "deep", "wrong-shape"])
def test_a_missing_or_unreadable_manifest_refuses_the_overwrite(root, damage):
    target = _export_dir(root) / "main.rvl"
    assert _call("revl_load", {"source": PLAIN})["ok"] is True
    assert _call("revl_export", {"path": str(target)})["ok"] is True
    manifest = _export_dir(root) / "manifest.json"
    manifest.unlink()
    if damage == "garbage":
        manifest.write_text("{not json", encoding="utf-8")
    elif damage == "directory":
        manifest.mkdir()
    elif damage == "deep":  # nesting the decoder cannot finish: records nothing
        manifest.write_text("[" * 200_000, encoding="utf-8")
    elif damage == "wrong-shape":
        digest = hashlib.sha256(PLAIN.encode("utf-8")).hexdigest()
        manifest.write_text(json.dumps({"files": {"main.rvl": {
            "sha256": digest, "extra": [[[]]]}}}), encoding="utf-8")
    out = _call("revl_export", {"path": str(target), "overwrite": True})
    assert out["ok"] is False and "was not written by revl_export" in _message(out)
    assert target.read_text(encoding="utf-8") == PLAIN


@pytest.mark.parametrize("brk", ["\r", "\u2028", "\x85", "\ufeff"],
                         ids=["cr", "line-separator", "nel", "bom"])
def test_a_note_body_with_another_line_break_is_refused(root, brk):
    path = root / "clock.rvl"
    path.write_text(CLOCK, encoding="utf-8")
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True
    note = _call("revl_knowledge", {"op": "add", "symbol": "FixedClock",
                                    "kind": "rationale", "body": f"why{brk}x"})
    assert note["ok"] is True, note
    out = _call("revl_export", {"with_knowledge": True})
    assert out["ok"] is False and "line separator" in _message(out), out
    assert not _export_dir(root).exists()
    assert path.read_text(encoding="utf-8") == CLOCK


# ------------------------------------------------------ the trusted controls


def test_a_trusted_author_is_jailed_like_any_other(trusted):
    """Trust decides how an exported file compiles, never where an export
    writes: a trusted author's inline export lands only in the export
    directory, and only as the `.rvl` modules the program reaches."""
    assert _call("revl_load", {"source": PLAIN,
                               "modules": {"notes.txt": "# notes\n"}})["ok"] is True
    outside = _call("revl_export", {"path": str(trusted / "main.rvl")})
    assert outside["ok"] is False and "not a .rvl module" in _message(outside)
    _reset()
    assert _call("revl_load", {"source": PLAIN, "modules": {
        "lib.rvl": "pub fn d() -> Int { return 1 }\n"}})["ok"] is True
    outside = _call("revl_export", {"path": str(trusted / "main.rvl")})
    assert outside["ok"] is False and "outside the export directory" in _message(outside)
    target = _export_dir(trusted) / "main.rvl"
    out = _call("revl_export", {"path": str(target)})
    assert out["ok"] is True and out["written"] == [str(target)], out
    assert out["skipped"] == ["lib.rvl"]
    assert _files_tree(trusted) == {os.path.join(".revl", "export", name)
                                    for name in ("main.rvl", "manifest.json")}


def test_a_trusted_author_still_loads_host_code_from_files(trusted):
    exported = _export_dir(trusted) / "ops.rvl"
    exported.parent.mkdir(parents=True)
    exported.write_text(HOSTED, encoding="utf-8")
    assert _call("revl_load", {"files": [str(exported)]})["ok"] is True


# ---------------------------------------- operator configuration, any trust


# every kind of file an export may never create or replace: one the server
# opened as operator configuration, and an emergency-stop latch (issue #2535),
# whether the serve wiring named it, the live session's WAL implies it, or it
# merely has the latch's suffix
PROTECTED = ["policy", "profile-case-variant", "policy-nfd", "wal-latch",
             "live-latch", "any-latch"]


@pytest.mark.parametrize("trust", ["untrusted", "trusted"])
@pytest.mark.parametrize("kind", PROTECTED)
def test_no_export_creates_or_replaces_a_protected_file(root, trust, kind,
                                                        monkeypatch):
    jail = _jail()
    monkeypatch.setattr(jail, "_OPERATOR_IDS", set())
    monkeypatch.setattr(jail, "_OPERATOR_NAMES", set())
    if trust == "trusted":
        server_mod.set_authoring_trust(host_code=True)
    # inside the export directory, so no other rule is what refuses it
    area = _export_dir(root)
    area.mkdir(parents=True)
    if kind == "policy":
        protected = area / "policy.rvl"
        protected.write_text("// operator policy\n", encoding="utf-8")
        jail.protect_operator_files([str(protected)])
    elif kind == "profile-case-variant":
        (area / "ops.profile").write_text(PROFILE, encoding="utf-8")
        jail.protect_operator_files([str(area / "ops.profile")])
        protected = area / "OPS.PROFILE"
    elif kind == "policy-nfd":  # protected before it exists, named in NFD
        jail.protect_operator_files([str(area / unicodedata.normalize(
            "NFC", "p\u00f3licy.rvl"))])
        protected = area / unicodedata.normalize("NFD", "p\u00f3licy.rvl")
    elif kind == "wal-latch":  # `serve --wal`: the latch need not exist yet
        jail.protect_operator_files([str(area / "run.wal") + ".estop"])
        protected = area / "run.wal.estop"
    elif kind == "live-latch":
        monkeypatch.setattr(server_mod.SESSION, "_wal_path",
                            str(area / "live.wal"), raising=False)
        protected = area / "live.wal.estop"
    else:
        protected = area / "other.estop"
    before = protected.read_bytes() if protected.exists() else None
    assert _call("revl_load", {"source": PLAIN})["ok"] is True
    target = (area if trust == "untrusted" else root) / "main.rvl"
    rel = os.path.relpath(protected, target.parent)
    server_mod.SESSION.origin["modules"] = {rel: FORGED}
    out = _call("revl_export", {"path": str(target), "overwrite": True})
    assert out["ok"] is False and "emergency-stop latch" in _message(out), out
    assert not target.exists()
    after = protected.read_bytes() if protected.exists() else None
    assert after == before


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


@pytest.mark.parametrize("spelling", ["ops.profile", "OPS.PROFILE"])
@pytest.mark.parametrize("trust", ["untrusted", "trusted"])
def test_an_export_cannot_replace_the_live_operator_profile(tmp_path, trust,
                                                            spelling):
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
        key = os.path.relpath(base / spelling, target.parent)
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


def test_a_note_cannot_smuggle_host_code_through_a_reload(tmp_path):
    """Review probe r02: a note body holding carriage returns was rendered as
    a `//` comment the lexer read as one line, then written back over the
    operator's file; read back with universal newlines it became declarations,
    and a new server ran the agent's host code as the operator's. The export
    is refused, the operator's file is unchanged, and nothing runs."""
    base = tmp_path / "root"
    base.mkdir()
    ops = base / "ops.rvl"
    ops.write_text(CLOCK, encoding="utf-8")
    inject = "\r".join([
        'extern pure fn boom() -> Str = @py { return "host code ran" }',
        "service Ops { fn run() -> Str }",
        "component OpsBox provides ops: Ops { provide ops { fn run() = boom() } }"])
    server = _Stdio(base)
    try:
        assert server.tool("revl_load", {"files": [str(ops)]}).get("ok") is True
        note = server.tool("revl_knowledge", {
            "op": "add", "symbol": "FixedClock", "kind": "rationale",
            "body": "why\r" + inject})
        assert note.get("ok") is True, note
        server.tool("revl_export", {"with_knowledge": True})
    finally:
        server.close()
    assert ops.read_bytes() == CLOCK.encode("utf-8")
    for path in [ops, *_export_dir(base).glob("*.rvl")]:
        server = _Stdio(base)
        try:
            server.tool("revl_load", {"files": [str(path)]})
            ran = server.tool("revl_call", {"key": "ops", "method": "run"})
            assert ran.get("result") != "host code ran", (path, ran)
        finally:
            server.close()


@pytest.mark.parametrize("trust", ["default", "untrusted", "trusted"])
def test_no_request_reaches_an_in_place_write(root, trust):
    """`revl_export` is reached only over the transport, so the caller is an
    author at every trust: a files-loaded export always writes the copy, and
    no argument in the request body asks for the operator's file instead."""
    if trust == "untrusted":
        server_mod.set_authoring_trust(host_code=False)
    elif trust == "trusted":
        server_mod.set_authoring_trust(host_code=True)
    path = root / "clock.rvl"
    path.write_text(CLOCK, encoding="utf-8")
    assert _call("revl_load", {"files": [str(path)]})["ok"] is True
    assert _call("revl_change", {"commit": True, "replace": {
        "component": "FixedClock.clock.now", "source": "fn now() = 8"}})["ok"]
    out = _call("revl_export", {"over_the_transport": False, "in_place": True,
                                "overwrite": True, "path": str(path)})
    copy = _export_dir(root).resolve() / "clock.rvl"
    assert out.get("written") in ([str(copy)], None, []), out
    assert path.read_text(encoding="utf-8") == CLOCK


@pytest.mark.parametrize("trust", ["untrusted", "trusted"])
def test_an_export_never_writes_the_manifest(root, trust):
    """A manifest is never an export target, under any spelling, in any
    directory, so an author cannot vouch for bytes it did not export."""
    if trust == "trusted":
        server_mod.set_authoring_trust(host_code=True)
    digest = hashlib.sha256(CLOCK.encode("utf-8")).hexdigest()
    forged = json.dumps({"files": {"ops.rvl": {"sha256": digest}}})
    target = _export_dir(root) / "main.rvl"
    for key in ("manifest.json", "MANIFEST.JSON",
                os.path.join("sub", ".revl", "export", "manifest.json"),
                os.path.join("..", "..", ".REVL", "Export", "Manifest.json")):
        _reset()
        assert _call("revl_load", {"source": PLAIN, "modules": {key: forged}})["ok"]
        out = _call("revl_export", {"path": str(target), "overwrite": True})
        assert out["ok"] is False and "export manifest" in _message(out), (key, out)
    assert not (_export_dir(root) / "manifest.json").exists()


def test_a_failed_write_records_nothing(root, monkeypatch):
    target = _export_dir(root) / "main.rvl"
    assert _call("revl_load", {"source": PLAIN})["ok"] is True
    assert _call("revl_export", {"path": str(target)})["ok"] is True
    manifest = _export_dir(root) / "manifest.json"
    recorded = manifest.read_bytes()
    _reset()
    assert _call("revl_load", {"source": PLAIN + "// changed\n"})["ok"] is True
    real = server_mod._write_atomic

    def failing(path, text):
        if path == str(target):
            raise OSError(36, "File name too long")
        real(path, text)

    monkeypatch.setattr(server_mod, "_write_atomic", failing)
    with pytest.raises(OSError):
        server_mod._tool_export({"path": str(target), "overwrite": True})
    assert manifest.read_bytes() == recorded
    assert target.read_text(encoding="utf-8") == PLAIN


@pytest.mark.parametrize("mode", ["root", "sub"])
def test_a_trusted_author_cannot_forge_the_manifest(tmp_path, mode):
    """Review probe p01: a trusted author wrote a fake manifest as an inline
    module (at the root, or in a subdirectory once the root had one), then
    exported over the operator's ops.rvl, which a closed-trust server later
    loaded unprofiled and ran the author's host body. Now the forged manifest
    is never written, the operator's file is unchanged, and nothing runs."""
    base = tmp_path / "root"
    sub = base / "sub" if mode == "sub" else base
    sub.mkdir(parents=True)
    ops = sub / "ops.rvl"
    ops.write_text(CLOCK, encoding="utf-8")
    digest = hashlib.sha256(CLOCK.encode("utf-8")).hexdigest()
    forged = json.dumps({"files": {"ops.rvl": {"sha256": digest}}})
    key = os.path.relpath(sub / ".revl" / "export" / "manifest.json", base)
    server = _Stdio(base, "--author-trust", "trusted")
    try:
        if mode == "sub":
            server.tool("revl_load", {"source": PLAIN})
            server.tool("revl_export", {"path": str(_export_dir(base) / "x.rvl")})
            server.tool("revl_unload", {"force": True})
        server.tool("revl_load", {"source": PLAIN, "modules": {key: forged}})
        server.tool("revl_export", {"path": str(base / "main.rvl"), "overwrite": True})
        server.tool("revl_unload", {"force": True})
        server.tool("revl_load", {"source": EVIL})
        server.tool("revl_export", {"path": str(ops), "overwrite": True})
    finally:
        server.close()
    assert ops.read_bytes() == CLOCK.encode("utf-8")
    server = _Stdio(base)
    try:
        server.tool("revl_load", {"files": [str(ops)]})
        ran = server.tool("revl_call", {"key": "ops", "method": "run"})
        assert ran.get("result") != "host code ran", ran
    finally:
        server.close()
