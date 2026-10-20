"""A load sent as export-area files reports the host code it carries
(issue #2641).

#2630 widened `_authored_host_bodies` and taught `_swap_inline` to say when its
candidate carries a file inside an export directory, so a swap sent only as
such a file reports `unreviewedHostCode` on its ticket. The same defect stayed
open at the other two doors named in #2628:

- `_boot`, the shared boot the load door uses, computed the bodies from
  `source`/`modules` alone, so a `revl_load` whose candidate was export-area
  files returned a ticket naming the class-(c) crossing and nothing about the
  host code riding with it;
- the shared `_compile` guard the check and admit verbs dispatch through did
  the same, so the bodies it remembered for a later ticket were empty. `plan`
  is not one of them: `_tool_plan` calls `revl.plan.plan`, whose own `_compile`
  lives in `plan.py`, writes no bodies and raises no approval ticket, so it
  cannot report and is out of scope here.

An export-area file is the author's by its PATH (`revl_export` writes source
only under `<root>/.revl/export/`, and `export_area.py` reads that as
authorship without consulting the trust mode). So the load door must report it
whatever the trust mode, exactly as the swap door does.

The two-sided shape of the swap test is kept here:

- an export-area load reports the host code the inline load reports;
- a load of operator files OUTSIDE the export area reports nothing. That
  direction is the operator's own program, and reporting it would be a false
  alarm. It is also the direction the tempting simplification (stop passing
  `host_bodies=authored`) gets wrong.

Two more tests drive the shared `_compile` guard over the transport, through
the two verbs that dispatch through it: `revl_check` and `revl_admit`. `plan`
is not covered for the reason given above. The draft boot is not tested here:
a draft filled over the transport always carries its edited buffer as a
`modules` entry, so `_authored_host_bodies`' existing condition already calls
it authored; a files working set with no edited buffers, which is the only
shape `_boot`'s export-area check would decide, is refused by the compiler's
own export-area rule under an open trust. `_boot` keeps its old behaviour for
that caller, and the fix is exactly the two doors the issue proves open.
"""

import importlib.util
from pathlib import Path

import pytest

from revl import compile_source
from revl.export_area import EXPORT_DIR, in_export_area
from revl.mcp import server

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="loading and booting a live composition needs cordis-py "
           "(`sh backends/python/setup.sh`, run under backends/python/.venv)")

# A running composition the emitter candidate can be admitted against: it
# provides a key the candidate does not, so admission turns on the compile
# rather than on a G2 collision.
_RUNNING = (
    "service Clock { fn tick() -> Int }\n"
    "component Timer provides clock: Clock {\n"
    "  provide clock { fn tick() = 1 }\n"
    "}\n"
)


def _emitter(sink: str) -> str:
    """A class-(c) emission in an ACTIVATION body, so under an approval policy
    the load itself raises the ticket two-step. Its host body belongs to
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
    except the guard test's, which reads the same module state the existing
    load-ticket test reads."""
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


def _reset() -> None:
    """Clear the session between the two loads the way the established repair
    fixture does (test_untrusted_repair_realm_2520.py): a held ticket installs
    nothing, but pending state must not colour the second ticket."""
    if server.SESSION.loaded:
        server.SESSION.unload()
    server.SESSION.proposals = {}
    server.SESSION.pending_draft = None
    server.SESSION.draft = None


def _load_ticket(transport, candidate: dict) -> dict:
    server.SESSION.approval_policy = "auto"
    result = transport("revl_load", {**candidate, "record": True})
    assert result["ok"] is False, result
    assert result["approvalRequired"] is True, result
    return result["ticket"]


@needs_cordis
def test_an_export_area_load_reports_the_host_code_the_inline_one_does(
        transport, tmp_path):
    """The same candidate through both load doors must disclose the same thing.

    The inline load is the control: it reported `unreviewedHostCode` before
    this fix. The export-area load reported nothing, which is the defect.
    Asserting equality pins the fixed door to the working one and cannot pass
    by both being empty, because the emptiness is checked first. Naming the
    author's own `announce` stops an attribution that reports only some other
    body from satisfying the equality.
    """
    sink = str(tmp_path / "sink.log")
    text = _emitter(sink)

    inline = _load_ticket(transport, {"source": text})
    assert inline.get("unreviewedHostCode"), (
        "the inline control reports no unreviewed host code, so this candidate "
        f"does not exercise the field at all: {inline}")
    assert "announce" in {e["extern"] for e in inline["unreviewedHostCode"]}, inline

    _reset()
    exported = _exported(tmp_path, text)
    assert in_export_area(str(exported))  # really in the export area

    through_file = _load_ticket(transport, {"files": [str(exported)]})
    assert through_file.get("unreviewedHostCode") == \
        inline["unreviewedHostCode"], through_file
    assert through_file.get("unreviewedHostCodeWarning") == \
        inline.get("unreviewedHostCodeWarning"), through_file


@needs_cordis
def test_a_load_of_operator_files_outside_the_export_area_reports_nothing(
        transport, tmp_path):
    """The other direction, which the fix must not break.

    A candidate that is entirely jailed `files` OUTSIDE the export area is the
    operator's own, and its host bodies are the operator's. Reporting them as
    unreviewed agent code would raise a false alarm on the operator's own
    program. This is what stops the tempting simplification from looking
    correct: dropping `host_bodies=authored` would make the approval path
    compute the bodies with no transport test at all, and this load would
    report the operator's own `announce`.
    """
    sink = str(tmp_path / "sink.log")
    operator_file = tmp_path / "operator.rvl"
    operator_file.write_text(_emitter(sink), encoding="utf-8")
    assert not in_export_area(str(operator_file))  # really outside the export area

    ticket = _load_ticket(transport, {"files": [str(operator_file)]})
    assert not ticket.get("unreviewedHostCode"), (
        "an operator's own file is being reported as unreviewed agent host "
        f"code, which is a false alarm on their own program: {ticket}")
    assert "unreviewedHostCodeWarning" not in ticket, ticket


def test_the_shared_compile_guard_reports_export_area_files_and_only_those(
        transport, tmp_path):
    """`revl_check` and `revl_admit` dispatch through `_compile`, whose
    remembered bodies a later class-(c) ticket names. It must carry the
    export-area part of a `files` candidate too, and nothing for the
    operator's own files.

    `revl_check` is the thinnest of those verbs, so a failure here is the
    guard's and not another verb's. Before this fix it recorded `[]` for the
    export-area candidate.
    """
    sink = str(tmp_path / "sink.log")
    text = _emitter(sink)
    exported = _exported(tmp_path, text)

    checked = transport("revl_check", {"files": [str(exported)]})
    assert checked["ok"] is True, checked
    assert [e["extern"] for e in server._AUTHORED_HOST_BODIES] == ["announce"], \
        server._AUTHORED_HOST_BODIES

    _reset()
    operator_file = tmp_path / "operator.rvl"
    operator_file.write_text(text, encoding="utf-8")
    # A sentinel proves the operator check OVERWRITES what the export check
    # recorded, rather than passing because nothing wrote the global at all.
    server._AUTHORED_HOST_BODIES = [
        {"extern": "sentinel", "classification": "emission", "backends": []}]

    checked = transport("revl_check", {"files": [str(operator_file)]})
    assert checked["ok"] is True, checked
    assert server._AUTHORED_HOST_BODIES == [], (
        "a check of the operator's own file recorded bodies as unreviewed "
        f"agent host code: {server._AUTHORED_HOST_BODIES}")


def test_the_shared_compile_guard_reports_export_area_files_through_admit(
        transport, tmp_path):
    """The same guard, driven through the other verb that dispatches through
    `_compile`: `revl_admit`.

    A fix that reached only `revl_check` would leave an export-area candidate
    admitted against a running composition recorded as carrying no host code,
    so the next class-(c) ticket would understate it again. The shape is the
    check test's: the export-area file records `announce`, the operator's own
    file overwrites that with `[]` and is not reported.
    """
    sink = str(tmp_path / "sink.log")
    text = _emitter(sink)
    exported = _exported(tmp_path, text)
    running = compile_source(_RUNNING, "running.rvl")

    admitted = transport("revl_admit", {"files": [str(exported)],
                                        "manifest": running})
    assert admitted["ok"] is True, admitted
    assert admitted["admitted"] is True, admitted
    assert [e["extern"] for e in server._AUTHORED_HOST_BODIES] == ["announce"], \
        server._AUTHORED_HOST_BODIES

    _reset()
    operator_file = tmp_path / "operator.rvl"
    operator_file.write_text(text, encoding="utf-8")
    # The sentinel proves the operator admit OVERWRITES what the export admit
    # recorded, rather than passing because nothing wrote the global at all.
    server._AUTHORED_HOST_BODIES = [
        {"extern": "sentinel", "classification": "emission", "backends": []}]

    admitted = transport("revl_admit", {"files": [str(operator_file)],
                                        "manifest": running})
    assert admitted["ok"] is True, admitted
    assert server._AUTHORED_HOST_BODIES == [], (
        "an admit of the operator's own file recorded bodies as unreviewed "
        f"agent host code: {server._AUTHORED_HOST_BODIES}")

