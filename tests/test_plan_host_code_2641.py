"""`revl_plan` raises no ticket, so it attributes no host code (#2641, #2668).

#2668 closed #2641 for the doors that ask for a yes: `revl_load`, `revl_check`
and `revl_admit` now name the host code an export-area file carries. It
recorded `revl_plan` as not covered, for a follow-up: `_tool_plan` calls
`revl.plan.plan`, whose own `_compile` lives in `plan.py`, writes no
`_AUTHORED_HOST_BODIES` and raises no approval ticket.

The follow-up is closed by stating that boundary, not by giving a dry run a
ticket. A plan admits nothing, so there is no ticket to understate and nothing
for the operator to approve; the disclosure belongs to the approval ticket a
load or swap raises, when the approval gate raises one. The plan's `note` now
says so, and the tests pin both halves:

- a plan of an export-area host-code candidate carries no ticket and no
  `unreviewedHostCode`, writes no `_AUTHORED_HOST_BODIES`, and names the
  approval ticket a load or swap raises as the place the attribution is made;
- the control: the same candidate loaded raises that ticket and names
  `announce`, so the disclosure is deferred, not lost;
- the opposite direction: a plan of files outside the export area states the
  same boundary, because the boundary is the plan's and makes no claim about
  who wrote the candidate.

The plan is not blind to host code: when it computes an emission surface, that
surface still names the externs the composition would reach (G8). What it does
not do is attribute them, which is the approval ticket's job.
"""

import importlib.util
from pathlib import Path

import pytest

from revl.export_area import EXPORT_DIR, in_export_area
from revl.mcp import server

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="loading a live composition needs cordis-py "
           "(`sh backends/python/setup.sh`, run under backends/python/.venv)")

# The attribution sentence the plan's note must carry. Kept in one place so the
# assertions below read as one claim, not three substrings by coincidence.
_ATTRIBUTION = "a load or swap raises"


def _emitter(sink: str) -> str:
    """A class-(c) emission in an ACTIVATION body, so loading it under an
    approval policy raises the ticket two-step. Its host body belongs to
    whoever wrote the text: inline source, or a file the export area says the
    author wrote."""
    return (
        "extern emission fn announce(sink: Str, msg: Str) = @py {\n"
        "    with open(sink, 'a') as _f:\n"
        "        _f.write('announce:' + msg + '\\n')\n"
        "    return\n"
        "}\n"
        "service Ops { fn ping() -> Int }\n"
        "component Quiet provides ops: Ops {\n"
        f"  emit announce(\"{sink}\", \"boot\")\n"
        "  provide ops { fn ping() = 1 }\n"
        "}\n"
    )


@pytest.fixture
def transport(tmp_path):
    """A server rooted at `tmp_path` under a trusted host-code author, driven
    over the wire. Every assertion is on a payload that crossed the transport,
    except the write-nothing check, which reads the same module state the
    existing load-ticket test reads."""
    from revl.mcp.session import Session

    before = server.AUTHORING
    before_session = server.SESSION
    before_authored = server._AUTHORED_HOST_BODIES
    before_live = server._LIVE_HOST_BODIES
    server.SESSION = Session()
    server.set_authoring_trust(host_code=True, roots=(str(tmp_path),))

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


def _exported(base: Path, text: str) -> Path:
    """`text` as a file inside the root's export directory."""
    path = base / EXPORT_DIR / "quiet.rvl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _load_ticket(transport, candidate: dict) -> dict:
    server.SESSION.approval_policy = "auto"
    result = transport("revl_load", {**candidate, "record": True})
    assert result["ok"] is False, result
    assert result["approvalRequired"] is True, result
    return result["ticket"]


def _assert_states_the_boundary(payload: dict) -> None:
    """The plan's answer says what it does not report and which ticket would
    attribute it."""
    assert payload["ok"] is True, payload
    assert payload["admissible"] is True, payload
    assert "approvalRequired" not in payload, payload
    assert "unreviewedHostCode" not in payload, payload
    assert "unreviewedHostCodeWarning" not in payload, payload
    note = payload["note"]
    assert "raises no approval ticket" in note, note
    assert "does not report `unreviewedHostCode`" in note, note
    assert _ATTRIBUTION in note, note


def test_a_plan_of_an_export_area_file_states_the_ticket_boundary(
        transport, tmp_path):
    """The direction this follow-up records: the plan admits nothing, so it
    raises no ticket and reports no `unreviewedHostCode`. It must say where
    that attribution is made instead, or its silence reads as "no host code".

    A sentinel in `_AUTHORED_HOST_BODIES` proves the plan is a dry run in the
    literal sense: it runs the gate without writing the bodies a later ticket
    would read. The emission-surface check proves the candidate really does
    carry host code, so the absent `unreviewedHostCode` is the boundary and
    not an empty candidate.
    """
    sink = str(tmp_path / "sink.log")
    exported = _exported(tmp_path, _emitter(sink))
    assert in_export_area(str(exported))  # really in the export area

    sentinel = [{"extern": "sentinel", "classification": "emission",
                 "backends": []}]
    server._AUTHORED_HOST_BODIES = list(sentinel)

    payload = transport("revl_plan", {"files": [str(exported)]})
    _assert_states_the_boundary(payload)
    assert server._AUTHORED_HOST_BODIES == sentinel, (
        "the plan wrote the bodies a later ticket reads, so a dry run is "
        f"colourising an approval: {server._AUTHORED_HOST_BODIES}")
    gained = (payload.get("emissionSurface") or {}).get("gained") or {}
    assert "Quiet -> announce" in (gained.get("hostCode") or []), payload


@needs_cordis
def test_the_load_ticket_for_the_same_candidate_names_the_host_code(
        transport, tmp_path):
    """The control: the disclosure the plan defers is really made by the verb
    that asks for a yes. If this ever stopped naming `announce`, the plan's
    note would be pointing at a ticket that says nothing."""
    sink = str(tmp_path / "sink.log")
    exported = _exported(tmp_path, _emitter(sink))

    ticket = _load_ticket(transport, {"files": [str(exported)]})
    assert "announce" in {e["extern"] for e in ticket.get("unreviewedHostCode") or []}, \
        ticket
    assert ticket.get("unreviewedHostCodeWarning"), ticket


def test_a_plan_of_files_outside_the_export_area_says_the_same(
        transport, tmp_path):
    """The opposite direction. The boundary is the plan's, not a statement
    about who wrote the candidate, so it reads the same for a file outside the
    export area. Under a trusted host-code author that file is agent text to
    the disclosure gate (issue #2675), which is why the plan's silence must
    not be read as an attribution: the plan makes none for any candidate.
    """
    sink = str(tmp_path / "sink.log")
    outside_file = tmp_path / "outside.rvl"
    outside_file.write_text(_emitter(sink), encoding="utf-8")
    assert not in_export_area(str(outside_file))  # really outside the export area

    payload = transport("revl_plan", {"files": [str(outside_file)]})
    _assert_states_the_boundary(payload)
    assert "unreviewedHostCode" not in payload, payload
