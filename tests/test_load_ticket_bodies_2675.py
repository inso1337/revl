"""A load ticket names the host bodies a trusted files-only load carries
(issue #2675).

The swap door follows the compile's authored report (issue #2672): a candidate
sent only as files outside every export area reads as agent text under
`--author-trust trusted`, because the vouched-for host bodies can rewrite any
file the agent can reach, so the ticket names the host code the disclosure gate
also treats as the agent's. The load door now follows the same report (issue
#2675, wired in #2677), so the two tickets must name the same bodies for the
same file. This is the end-to-end control for that equality; the mocked-load
unit test beside it pins the boot call without a runtime.

This test pins the load ticket to the swap ticket for the same file, rather than
to a hardcoded body list: the swap is the door #2672 already fixed, so equality
says the two doors agree without restating the bodies. The emptiness is checked
first, so the equality cannot pass by both doors reporting nothing.

The other trust mode is checked too: while host code is UNTRUSTED a files-only
candidate outside every export area is the operator's own jailed text, compiled
unprofiled, and reporting its host bodies as unreviewed agent code would raise a
false alarm on the operator's own program.
"""

import importlib.util
from pathlib import Path

import pytest

from revl.export_area import in_export_area
from revl.mcp import server

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="loading and swapping a live composition needs cordis-py "
           "(`sh backends/python/setup.sh`, run under backends/python/.venv)")

# A running composition the successor can replace: one component providing the
# only service, so a swap that replaces it composes cleanly.
_RUNNING = (
    "service Ops { fn ping() -> Int }\n"
    "component OpsV1 provides ops: Ops {\n"
    "  provide ops { fn ping() = 1 }\n"
    "}\n"
)

# The successor is both a complete composition (it can be loaded alone) and an
# admissible replacement for `OpsV1` (it can be swapped in). Its ACTIVATION
# emits, so a load or a swap of it raises the class-(c) ticket, and the host
# body it carries is `candidate_note`'s.
_CANDIDATE = (
    "service Ops { fn ping() -> Int }\n"
    "extern emission[notify] fn candidate_note(msg: Str) = @py { return }\n"
    "component OpsV2 provides ops: Ops {\n"
    '  emit candidate_note("candidate")\n'
    "  provide ops { fn ping() = 2 }\n"
    "}\n"
)


@pytest.fixture
def transport(tmp_path):
    """A server rooted at `tmp_path` under a trusted host-code author, driven
    over the wire. Every assertion is on a payload that crossed the
    transport."""
    from revl.mcp.session import Session

    before = server.AUTHORING
    before_session = server.SESSION
    before_authored = server._AUTHORED_HOST_BODIES
    before_live = server._LIVE_HOST_BODIES
    server.SESSION = Session()
    server.set_authoring_trust(host_code=True, granted=None,
                               roots=(str(tmp_path),))

    def call(name: str, arguments: dict) -> dict:
        response = server.handle({"jsonrpc": "2.0", "id": 1,
                                  "method": "tools/call",
                                  "params": {"name": name,
                                             "arguments": arguments}})
        return response["result"]["structuredContent"]

    yield call

    if server.SESSION.loaded:
        server.SESSION.unload()
    server.SESSION = before_session
    server.AUTHORING = before
    server._AUTHORED_HOST_BODIES = before_authored
    server._LIVE_HOST_BODIES = before_live


def _reset() -> None:
    """Clear the session between the tickets being compared the way the
    established repair fixture does (test_untrusted_repair_realm_2520.py): a
    held ticket installs nothing, but pending state must not colour the next
    ticket."""
    if server.SESSION.loaded:
        server.SESSION.unload()
    server.SESSION.proposals = {}
    server.SESSION.pending_draft = None
    server.SESSION.draft = None


def _candidate_file(tmp_path: Path) -> Path:
    """`_CANDIDATE` as the operator would name it: a file in the root, outside
    every export area."""
    path = tmp_path / "candidate.rvl"
    path.write_text(_CANDIDATE, encoding="utf-8")
    assert not in_export_area(str(path))
    return path


def _load_ticket(transport, candidate: dict) -> dict:
    server.SESSION.approval_policy = "auto"
    result = transport("revl_load", {**candidate, "record": True})
    assert result["ok"] is False, result
    assert result["approvalRequired"] is True, result
    return result["ticket"]


@needs_cordis
def test_a_trusted_files_only_load_reports_the_inline_host_code(
        transport, tmp_path):
    """The same candidate through both load doors must disclose the same thing.

    The inline load is the control: it reported `unreviewedHostCode` before
    this fix because its text arrived over the transport. The file load
    reported nothing, which is the defect. Naming the author's own
    `candidate_note` stops an attribution that reports only some other body
    from satisfying the equality.
    """
    inline = _load_ticket(transport, {"source": _CANDIDATE})
    assert inline.get("unreviewedHostCode"), (
        "the inline control reports no unreviewed host code, so this candidate "
        f"does not exercise the field at all: {inline}")
    assert "candidate_note" in {e["extern"]
                                for e in inline["unreviewedHostCode"]}, inline

    _reset()
    candidate = {"files": [str(_candidate_file(tmp_path))]}
    through_file = _load_ticket(transport, candidate)
    assert through_file.get("unreviewedHostCode") == \
        inline["unreviewedHostCode"], through_file
    assert through_file.get("unreviewedHostCodeWarning") == \
        inline.get("unreviewedHostCodeWarning"), through_file


@needs_cordis
def test_an_untrusted_files_only_load_outside_the_export_area_reports_nothing(
        transport, tmp_path):
    """The other trust mode, in which that ownership is true.

    While host code is untrusted a files-only candidate outside every export
    area is the operator's own jailed text and its host bodies are the
    operator's. Reporting them as unreviewed agent code would raise a false
    alarm on the operator's own program.
    """
    server.set_authoring_trust(host_code=False)
    ticket = _load_ticket(transport, {"files": [str(_candidate_file(tmp_path))]})
    assert not ticket.get("unreviewedHostCode"), (
        "an operator's own untrusted file is being reported as unreviewed "
        f"agent host code, which is a false alarm on their own program: {ticket}")
    assert "unreviewedHostCodeWarning" not in ticket, ticket


@needs_cordis
def test_the_trusted_files_only_load_and_swap_tickets_agree(transport, tmp_path):
    """The load ticket states the host bodies the swap ticket states, for the
    same file (issue #2675).

    The swap is the door #2672 fixed; the load is the door this issue fixes.
    A file outside every export area under trusted host code is agent text for
    both, so a yes to either admits the same host code and each ticket must
    name it. Only `unreviewedHostCode` is compared: a swap ticket also carries
    `newHostCode` and `runningHostCode`, which are about the generation being
    replaced and have no counterpart on a cold load.
    """
    candidate = _candidate_file(tmp_path)
    base = tmp_path / "base.rvl"
    base.write_text(_RUNNING, encoding="utf-8")

    server.SESSION.approval_policy = "auto"
    assert transport("revl_load", {"files": [str(base)],
                                   "record": True})["ok"] is True
    swapped = transport("revl_swap", {"files": [str(candidate)],
                                      "replacing": ["OpsV1"]})
    assert swapped["ok"] is False, swapped
    assert swapped["approvalRequired"] is True, swapped
    swap_ticket = swapped["ticket"]
    assert swap_ticket.get("unreviewedHostCode"), (
        f"the swap control reports no unreviewed host code: {swap_ticket}")

    _reset()
    load_ticket = _load_ticket(transport, {"files": [str(candidate)]})
    assert load_ticket.get("unreviewedHostCode") == \
        swap_ticket["unreviewedHostCode"], load_ticket


# A second trusted file that declares its own host body and nothing else, so a
# mixed candidate has two files' bodies to name and a module's beside them.
_SECOND_BODY = (
    "extern pure fn second_note(x: Str) -> Str = @py { return x }\n"
)


@needs_cordis
def test_a_trusted_load_with_files_and_a_module_names_both_bodies(
        transport, tmp_path):
    """An edited buffer riding in `modules` does not replace the files' bodies.

    A draft boot or a load carries an edited file as a `modules` entry beside
    the files it did not edit. Before #2675 the trusted-files attribution was
    skipped whenever `modules` was non-empty, so the ticket named only the
    module's body and dropped the outside file's body from what a yes admits.
    Both files are the trusted author's text, so the ticket names both.
    """
    first = _candidate_file(tmp_path)
    second = tmp_path / "second-2675.rvl"
    second.write_text(_SECOND_BODY, encoding="utf-8")
    assert not in_export_area(str(second))

    ticket = _load_ticket(transport, {
        "files": [str(first), str(second)],
        "modules": {str(first): _CANDIDATE + "// edited\n"},
    })
    assert {entry["extern"] for entry in ticket["unreviewedHostCode"]} == {
        "candidate_note", "second_note",
    }, ticket
