"""The path jail must not read a snapshot's approval-policy NAME as a path (#2033).

`_collect_path_arguments` applies its key allowlist at EVERY depth, so a
`revl_restore` document — a nested dict — inherited the top-level meaning of
every bare key inside it. A snapshot's `meta.approval.policy` holds the
approval-policy MODE name (`"auto"`, `persist._approval_posture`), and the
collector classified the word `auto` as a path argument. Under the default
`revl mcp serve --approval-policy auto` the jail therefore refused **the exact
document `revl_snapshot` had just handed back**, and the `undo` a `revl_unload`
tells the session to keep — `{"tool": "revl_restore", "arguments": {"snapshot":
…}}` — was a document the session could not use. Same family as #1709: the jail
applied to a value that is not a path.

The fix is CLASSIFICATION, not loosening. `policy` and `registry` are paths only
where a tool DECLARES them as one — the top level of its own arguments, the only
two sites that open them (`_tool_resolve`, `_tool_repair`). The `files` family
stays path-bearing at every depth, because its nested occurrences really are read
from disk: a restore document's `sources.files` (`_restore_authoring_refusal`)
and a repair candidate's `candidate.files` (`repair._compile_candidate`).

Both directions are pinned here: the round trip succeeds, and every genuine
escape — a top-level `files`, a nested `sources.files`, a nested
`candidate.files`, a top-level `policy` — is still refused, stat-free, with
nothing read.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server  # noqa: E402
from revl.mcp.session import Session  # noqa: E402

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the session tools need the cordis-py runtime — install it with "
           "`sh backends/python/setup.sh`")

SERVICE = "service Store { fn get(k: Str) -> Str }\n"
COMPONENT = ("component M provides store: Store {\n"
             '  provide store { fn get(k) = "v" }\n'
             "}\n")
OUTSIDE = "/etc/passwd"
NEED = "service Database { fn ping() -> Bool }\n"

#: `--approval-policy` mode -> the `(session.approval_policy,
#: session.approval_separation)` pair `_resolve_serve_approval_mode` binds, and
#: the `meta.approval.policy` that posture stamps into a snapshot (None = no
#: posture stamped at all, the pre-#1706 `off` shape).
MODES = {
    "auto": ("auto", True, "auto"),
    "advisory": ("auto", False, "auto"),
    "off": (None, False, None),
}


@pytest.fixture(autouse=True)
def _fresh_server(tmp_path, monkeypatch):
    """A closed-by-default server rooted at `tmp_path`, restored afterwards."""
    before = server.AUTHORING
    monkeypatch.setattr(server, "SESSION", Session())
    server.set_authoring_trust(host_code=False, granted=None, providers=None,
                               roots=(str(tmp_path),))
    yield
    server.AUTHORING = before


@pytest.fixture
def tree(tmp_path):
    """One component on disk inside the sanctioned root."""
    path = tmp_path / "store.rvl"
    path.write_text(SERVICE + COMPONENT, encoding="utf-8")
    return [str(path)]


def _call(name: str, arguments: dict) -> dict:
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": name, "arguments": arguments}})
    return json.loads(response["result"]["content"][0]["text"])


def _message(payload: dict) -> str:
    return " ".join(d.get("message", "") for d in payload.get("diagnostics") or [])


def _apply_mode(mode: str) -> None:
    policy, separation, _ = MODES[mode]
    server.SESSION.approval_policy = policy
    server.SESSION.approval_separation = separation


def _refused(payload: dict) -> None:
    """A jail refusal: the message names the root, and nothing ran."""
    assert payload["ok"] is False, payload
    assert "outside the operator-sanctioned root" in _message(payload), payload
    assert payload["note"] == "nothing was read, compiled or loaded", payload


# ------------------------------------------------- (a) the classification rule

def test_a_snapshots_policy_name_contributes_no_path():
    """Acceptance 5, first half: `meta.approval.policy` is a MODE name.

    A nested `policy` — anywhere below the top level — is bookkeeping, never a
    caller-supplied path, because no handler opens one there."""
    out: list = []
    server._collect_path_arguments(
        {"snapshot": {"sources": {"source": "component A {}"},
                      "meta": {"approval": {"policy": "auto"},
                               "config": {}, "record": False}}}, out)
    assert out == []


def test_a_snapshots_nested_sources_files_contributes_every_path():
    """Acceptance 5, second half: the nested key that IS read from disk.

    `revl_restore` compiles `sources.files` (`_restore_authoring_refusal`), so
    the same document's nested `files` must still be collected — the fix narrows
    the allowlist, it does not stop descending."""
    out: list = []
    server._collect_path_arguments(
        {"snapshot": {"sources": {"files": ["/in/a.rvl", "/in/b.rvl"]},
                      "meta": {"approval": {"policy": "auto"}}}}, out)
    assert out == ["/in/a.rvl", "/in/b.rvl"]


def test_the_top_level_meaning_of_policy_and_registry_is_unchanged():
    """The other half of the rule: at the top level they ARE paths.

    `revl_resolve` opens `policy` and `revl_repair` opens `registry`; both are
    declared there, so both are collected there."""
    out: list = []
    server._collect_path_arguments({"files": ["/in/a.rvl"],
                                    "policy": "/etc/passwd",
                                    "registry": "/etc",
                                    "traceFile": "/etc/hosts"}, out)
    assert out == ["/in/a.rvl", "/etc/passwd", "/etc", "/etc/hosts"]


def test_a_nested_candidate_files_contributes_every_path():
    """`revl_repair`'s candidate is compiled from disk
    (`repair._compile_candidate`), so its nested `files` stays a path."""
    out: list = []
    server._collect_path_arguments(
        {"component": "C", "candidate": {"files": ["/in/a.rvl"],
                                         "modules": {"m.rvl": "// x"}}}, out)
    assert out == ["/in/a.rvl"]


# ------------------------------------------- (b) the round trip now completes

@needs_runtime
@pytest.mark.parametrize("mode", sorted(MODES))
def test_a_snapshot_round_trips_under_the_default_approval_policy(mode, tree):
    """Acceptance 1 and 2: load -> snapshot -> unload -> restore the document.

    The snapshot's own `meta.approval.policy` is the mode name — under `auto`
    and `advisory` it is literally `"auto"`, which is exactly the value the
    unmodified collector read as an escaping path."""
    _apply_mode(mode)
    assert _call("revl_load", {"files": tree})["ok"] is True

    snap = _call("revl_snapshot", {})["snapshot"]
    expected = MODES[mode][2]
    if expected is None:
        assert "approval" not in snap["meta"], snap["meta"]
    else:
        assert snap["meta"]["approval"]["policy"] == expected, snap["meta"]

    assert _call("revl_unload", {})["ok"] is True
    restored = _call("revl_restore", {"snapshot": snap})
    assert restored["ok"] is True, _message(restored)
    assert restored["restored"] is True and restored["reAdmitted"] is True
    assert [c["name"] for c in restored["components"]] == ["M"], restored
    assert _call("revl_state", {})["ok"] is True


@needs_runtime
@pytest.mark.parametrize("mode", sorted(MODES))
def test_the_unload_undo_round_trips_under_the_default_approval_policy(mode,
                                                                      tree):
    """Acceptance 3: `revl_unload`'s `undo` is `revl_restore`'s document.

    The session is told to keep this document; before the fix it was a document
    the session could not use, which is what made the recovery path dead."""
    _apply_mode(mode)
    assert _call("revl_load", {"files": tree})["ok"] is True
    unloaded = _call("revl_unload", {})
    assert unloaded["ok"] is True, unloaded
    undo = unloaded["undo"]
    assert undo["tool"] == "revl_restore", undo

    restored = _call(undo["tool"], undo["arguments"])
    assert restored["ok"] is True, _message(restored)
    assert restored["reAdmitted"] is True


# ------------------------------------- (c) every genuine escape is still refused

def test_an_escaping_top_level_files_path_is_still_refused(tree):
    """Acceptance 4: `revl_load {files: [<outside>]}`."""
    _refused(_call("revl_load", {"files": [OUTSIDE]}))


def test_an_escaping_top_level_policy_path_is_still_refused():
    """Acceptance 4: `revl_resolve {policy: <outside>}` — the key that IS a path
    where it is declared."""
    _refused(_call("revl_resolve", {"need": NEED, "policy": OUTSIDE}))


def test_an_escaping_nested_sources_files_path_is_still_refused():
    """The nested `files` the fix deliberately keeps collecting: a restore
    document's `sources.files`, which the restore compile reads."""
    snap = {"sources": {"files": [OUTSIDE],
                        "files_content": {OUTSIDE: SERVICE + COMPONENT}},
            "manifest": {"components": [{"name": "M", "provides": ["store"]}],
                         "loadOrder": ["M"]},
            "meta": {"snapshotVersion": 1, "components": ["M"],
                     "loadOrder": ["M"], "record": False, "config": {}}}
    _refused(_call("revl_restore", {"snapshot": snap}))


def test_an_escaping_nested_candidate_files_path_is_still_refused():
    """`revl_repair` compiles `candidate.files` from disk, so the nested key
    must not have been narrowed away with `policy`."""
    _refused(_call("revl_repair", {"component": "M",
                                   "candidate": {"files": [OUTSIDE]}}))


def test_the_policy_name_is_not_a_second_way_to_smuggle_a_path():
    """The narrowing is provably not a widening: a snapshot document that puts
    a real path under a nested `policy` does not thereby escape the jail — the
    nested key is simply not a path, and the jail refuses nothing for it. What
    must still be refused is the same document's nested `files`."""
    snap = {"sources": {"files": [OUTSIDE]},
            "meta": {"approval": {"policy": "auto"},
                     "config": {"M": {"policy": OUTSIDE}}}}
    _refused(_call("revl_restore", {"snapshot": snap}))
