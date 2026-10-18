"""`revl_deploy` confines its local paths and shows the approver what a yes does.

Issue #2528. Three gaps, each reproducible for a trusted operator too:

  1. `bundle`, and the `known_hosts` / `host_key` files inside each
     `[processes.<p>.deploy]` table, were not path arguments, so the root jail
     never saw them; the remote fields (`host`, `remote_bundle`, `runner`,
     `extra_args`, `port`) had no shape rule, so a value could reach ssh or scp
     as an option, or the far shell as extra words.
  2. A process name with a path separator named spec, world and WAL files
     outside the deploy state directory.
  3. The apply ticket carried only a digest, so the approver could not see the
     host, the remote path or the options a yes would let through.

Every case drives the real dispatch (`handle`). Nothing here opens a
connection: a refused map stops at the jail or at admission, and an admitted
`apply: true` stops at the approval ticket.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import deploy  # noqa: E402
from revl.mcp import server as server_mod  # noqa: E402
from revl.mcp.server import handle  # noqa: E402

OUTSIDE_ROOT = "outside the operator-sanctioned root(s)"
GOOD_HOST = "deploy@far.example"
GOOD_REMOTE = "/srv/revl/app.revlbundle"
GOOD_EXTRA = ["--key", "keys/trust.key", "--host-key", "keys/host.key",
              "--runtime-version=python=3.11", "--require-gauntlet"]


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


@pytest.fixture(params=[False, True], ids=["untrusted", "trusted"])
def jail(request, tmp_path, monkeypatch):
    """A sanctioned root holding a bundle, a pinned known_hosts and a host key,
    and an OUTSIDE directory holding the same three; the cwd is the root."""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    for base in (root, outside):
        (base / "bundle").mkdir(parents=True)
        (base / "bundle" / "x").write_text("x", encoding="utf-8")
        (base / "known_hosts").write_text(
            "far.example ssh-ed25519 AAAAC3NzaC1lZDI1NTE5\n", encoding="utf-8")
        (base / "host.key").write_text("k", encoding="utf-8")
    monkeypatch.chdir(root)
    old = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(root),),
                                   host_code=request.param)
    try:
        yield {"root": root, "outside": outside}
    finally:
        server_mod.AUTHORING = old


def _ssh_map(base: Path, **over) -> dict:
    table = {"via": "ssh", "host": GOOD_HOST,
             "known_hosts": str(base / "known_hosts"),
             "host_key": str(base / "host.key"),
             "remote_bundle": GOOD_REMOTE, "extra_args": list(GOOD_EXTRA)}
    table.update(over)
    return {"processes": {"edge": {"components": ["Edge"], "deploy": table}}}


def _jail_refused(result: dict) -> None:
    assert result.get("ok") is False, result
    assert result.get("approvalRequired") is not True, result
    message = " ".join(d.get("message", "")
                       for d in result.get("diagnostics") or [])
    assert OUTSIDE_ROOT in message, result


def _admit_refused(result: dict, rule: str) -> dict:
    assert result.get("ok") is False, result
    assert result.get("admitted") is False, result
    assert result.get("stoppedAt") == "admit", result
    assert result.get("approvalRequired") is not True, result
    rules = [r["rule"] for r in result.get("refusals") or []]
    assert rule in rules, result
    return result


# ------------------------------------------------- local paths: the jail

def test_a_bundle_outside_the_root_is_refused(jail):
    result = _call("revl_deploy", {
        "placement": _ssh_map(jail["root"]),
        "bundle": str(jail["outside"] / "bundle"), "apply": True})
    _jail_refused(result)


def test_a_relative_bundle_that_climbs_out_of_the_root_is_refused(jail):
    result = _call("revl_deploy", {
        "placement": _ssh_map(jail["root"]),
        "bundle": "../outside/bundle", "apply": True})
    _jail_refused(result)


@pytest.mark.parametrize("field", ["known_hosts", "host_key"])
def test_a_placement_file_outside_the_root_is_refused(jail, field):
    placement = _ssh_map(jail["root"],
                         **{field: str(jail["outside"] / Path(field).name)})
    for apply in (False, True):
        result = _call("revl_deploy", {
            "placement": placement, "bundle": str(jail["root"] / "bundle"),
            "apply": apply})
        _jail_refused(result)


PATH_KEY_NAMES = ["policy", "registry", "bundle", "files", "traceFile"]


def _named_map(name: str, base: Path, **over) -> dict:
    placement = _ssh_map(base, **over)
    placement["processes"] = {name: placement["processes"]["edge"]}
    return placement


@pytest.mark.parametrize("name", PATH_KEY_NAMES)
def test_a_process_named_like_a_path_argument_is_still_jailed(jail, name):
    """A process whose NAME is also a path-argument key still has its deploy
    table's local files confined: the table is found by position."""
    outside = jail["outside"]
    placement = _named_map(name, jail["root"],
                           known_hosts=str(outside / "known_hosts"),
                           host_key=str(outside / "host.key"))
    for apply in (False, True):
        result = _call("revl_deploy", {
            "placement": placement, "bundle": str(jail["root"] / "bundle"),
            "apply": apply})
        assert result.get("ok") is False, result
        assert result.get("approvalRequired") is not True, result
        assert result.get("admitted") is not True, result


@pytest.mark.parametrize("name", PATH_KEY_NAMES)
def test_the_deploy_admission_jails_by_position_on_its_own(jail, name):
    """The deploy's own check finds the files by position, without relying on
    the generic jail's key matching."""
    outside = jail["outside"]
    placement = _named_map(name, jail["root"],
                           host_key=str(outside / "host.key"))
    admitted = server_mod._tool_deploy_admit({
        "placement": placement, "bundle": str(jail["root"] / "bundle")})
    assert admitted["ok"] is False
    assert [r["rule"] for r in admitted["refusals"]] == ["outside-root"]
    admitted = server_mod._tool_deploy_admit({
        "placement": _named_map(name, jail["root"]),
        "bundle": str(outside / "bundle")})
    assert admitted["ok"] is False


def test_an_in_root_plan_for_a_path_key_named_process_is_admitted(jail):
    root = jail["root"]
    placement = _named_map("policy", root)
    rehearsal = _call("revl_deploy", {"placement": placement,
                                      "bundle": str(root / "bundle")})
    assert rehearsal["ok"] is True and rehearsal["admitted"] is True, rehearsal
    applied = _call("revl_deploy", {"placement": _named_map("policy", root),
                                    "bundle": "bundle", "apply": True})
    assert applied.get("approvalRequired") is True, applied


# ------------------------------------------ remote fields: the shape rule

@pytest.mark.parametrize("over", [
    {"host": "-oProxyCommand=touch${IFS}marker"},
    {"host": "far.example -oProxyCommand=x"},
    {"host": "far.example;id"},
    {"remote_bundle": "-oProxyCommand=x"},
    {"remote_bundle": "/srv/app;touch marker"},
    {"remote_bundle": "/srv/app $(id)"},
    {"runner": "revl;id"},
    {"extra_args": ["-oProxyCommand=touch marker"]},
    {"extra_args": ["--key", "k;id"]},
    {"extra_args": ["--key", "-oProxyCommand=x"]},
    {"extra_args": ["--key"]},
    {"extra_args": "--key k"},
    {"port": "22 -oProxyCommand=x"},
    {"port": 0},
    {"known_hosts": "known hosts"},
    {"known_hosts": "%d/known_hosts"},
    {"known_hosts": "${HOME}/known_hosts"},
], ids=lambda o: next(iter(o)))
def test_a_remote_field_with_an_unsafe_shape_is_refused(jail, over):
    placement = _ssh_map(jail["root"], **over)
    for apply in (False, True):
        result = _call("revl_deploy", {
            "placement": placement, "bundle": str(jail["root"] / "bundle"),
            "apply": apply})
        _admit_refused(result, "remote-shape")


# -------------------------------------------------- process names

@pytest.mark.parametrize("name", ["../escaped", "a/b", "..", "a\\b", "a\0b",
                                  "D:escaped"])
def test_a_process_name_that_leaves_the_state_dir_is_refused(jail, name):
    placement = {"processes": {name: {"components": ["C"],
                                      "deploy": {"via": "local"}}}}
    result = _call("revl_deploy", {"placement": placement, "bundle": "bundle",
                                   "apply": True})
    _admit_refused(result, "process-name")
    # and a process with no deploy table at all is held to the same rule
    bare = {"processes": {name: {"components": ["C"]}}}
    _admit_refused(_call("revl_deploy", {"placement": bare}), "process-name")
    assert not list(jail["root"].parent.glob("escaped*"))


# ------------------------------------------------- the ticket text

def test_the_ticket_names_the_host_remote_path_and_options(jail):
    root = jail["root"]
    result = _call("revl_deploy", {"placement": _ssh_map(root),
                                   "bundle": "bundle", "apply": True})
    assert result.get("approvalRequired") is True, result
    ticket = result["ticket"]
    summary = ticket["summary"]
    for needle in (GOOD_HOST, GOOD_REMOTE, "--key keys/trust.key",
                   "--require-gauntlet", str(root / "known_hosts"),
                   str(root / "bundle")):
        assert needle in summary, (needle, summary)
    (target,) = ticket["deploy"]["targets"]
    assert target["host"] == GOOD_HOST
    assert target["remote_bundle"] == GOOD_REMOTE
    assert target["extra_args"] == GOOD_EXTRA
    assert ticket["deploy"]["bundle"] == str(root / "bundle")

    # the digest still binds them: another host is another ticket
    other = _call("revl_deploy", {
        "placement": _ssh_map(root, host="deploy@other.example"),
        "bundle": "bundle", "apply": True})
    assert other.get("approvalRequired") is True, other
    assert other["ticket"]["hash"] != ticket["hash"]
    assert other["ticket"]["candidateHash"] != ticket["candidateHash"]


# ------------------------------------------------- the in-root control

def test_an_in_root_deploy_plan_is_still_admitted(jail):
    root = jail["root"]
    rehearsal = _call("revl_deploy", {"placement": _ssh_map(root),
                                      "bundle": str(root / "bundle")})
    assert rehearsal["ok"] is True and rehearsal["admitted"] is True, rehearsal
    assert rehearsal["boundaries"] == {"edge": "machine"}
    # relative local paths resolve inside the root, an IPv4 host and an
    # explicit port are fine, and a local process keeps a plain name
    relative = _ssh_map(root, known_hosts="known_hosts", host_key="host.key",
                        host="10.0.0.5", port=2222)
    relative["processes"]["svc-1.a_b"] = {"components": ["C"],
                                          "deploy": {"via": "local"}}
    applied = _call("revl_deploy", {"placement": relative, "bundle": "bundle",
                                    "apply": True})
    assert applied.get("approvalRequired") is True, applied


def test_the_cli_shape_of_runner_flags_is_admitted():
    """The runner-flag spelling the CLI deploy tests use admits unchanged."""
    verdict = deploy.admit_deploy_map({"processes": {"svc": {"deploy": {
        "via": "ssh", "host": "deploy@testhost", "known_hosts": "/k",
        "host_key": "/h", "remote_bundle": "/far/app.revlbundle",
        "extra_args": ["--key", "/p/key", "--host-key", "/p/key",
                       "--runtime-version", "python=3.11"]}}}})
    assert verdict.ok, verdict.refusals
