"""A swap sent only as an export-area file reports the host code it carries
(issue #2628).

`_authored_host_bodies` treated a candidate as authored only when it arrived as
inline `source` or `modules`. A file inside an export directory is the author's
by its PATH and always has been: `revl_export`, the one MCP verb that writes to
disk, writes source only under `<root>/.revl/export/`, and `export_area.py`
reads that as authorship without consulting the trust mode at all. So a swap
whose candidate was only such a file came back with an empty list, and the
approval ticket then carried neither `unreviewedHostCode` nor its warning: the
operator approved a crossing without being told the composition carried
unreviewed agent-authored host code.

This is not the realm question next door. The realm-removal check needs a
profile to have been APPLIED, so it gates on `AUTHORING.profile()`. The ticket
question is whether the author WROTE this text, and for an export-area path the
answer is yes whatever the trust mode. The two must not share a predicate.

The test asserts the export-area ticket against the inline one for the same
candidate rather than against a hardcoded list: the inline door is the control
that already reported this field before the fix, so equality pins the export-area
door to it without restating what the bodies are.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl.export_area import EXPORT_DIR  # noqa: E402
from revl.mcp import server  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="loading and swapping a live composition needs cordis-py "
           "(`sh backends/python/setup.sh`, run under backends/python/.venv)",
)

_DECLS = ("service Ops { emission[notify] fn stash(p: Str) }\n"
          "service Tool { fn describe() -> Str }\n")

# the OPERATOR's host code, wired in as `AuthoringTrust.providers`
_PROVIDER = ("extern emission[notify] fn notify(p: Str) = @py { return }\n"
             "component OpsProvider provides ops: Ops {\n"
             "  provide ops { fn stash(p) { emit notify(p) } }\n"
             "}\n")

_BASE = (_DECLS + "component ToolV1 provides tool: Tool {\n"
         '  provide tool { fn describe() = "v1" }\n'
         "}\n")

# the successor: its ACTIVATION reaches the operator's class-(c) emission, and it
# carries an immediate `candidate_note` emission of its own, so what holds the
# swap on an approval ticket is over-determined rather than the operator's
# emission alone.
#
# It carries a host body of its OWN (`candidate_note`), and that is deliberate
# rather than incidental: the operator's `notify` is in the merged IR too, so a
# ticket built from every body in the IR would report `notify` here and would be
# reporting the OPERATOR's code as the author's. Giving the candidate its own
# body means the assertion below is about a body the author really wrote, and it
# keeps the test meaningful once attribution is narrowed: a correctly attributed
# ticket still reports `candidate_note`, so the equality between the two doors
# cannot be satisfied by both of them reporting nothing.
_CANDIDATE = (_DECLS
              + "extern emission[notify] fn candidate_note(msg: Str) = @py "
                "{ return }\n"
              + "component ToolV2 requires ops: Ops provides tool: Tool {\n"
              '  emit ops.stash("boot")\n'
              '  emit candidate_note("candidate")\n'
              '  provide tool { fn describe() = "v2" }\n'
              "}\n")


@pytest.fixture
def transport(tmp_path):
    """A server rooted at `tmp_path` under a trusted host-code author, driven
    over the wire. Nothing below reads internal state: every assertion is on a
    payload that crossed the transport."""
    from revl.mcp.session import Session

    before = server.AUTHORING
    before_session = server.SESSION
    server.SESSION = Session()
    server.set_authoring_trust(host_code=True, granted=None,
                               providers={"prov.rvl": _PROVIDER},
                               roots=(str(tmp_path),))

    def call(name: str, arguments: dict) -> dict:
        response = server.handle({"jsonrpc": "2.0", "id": 1,
                                  "method": "tools/call",
                                  "params": {"name": name,
                                             "arguments": arguments}})
        return json.loads(response["result"]["content"][0]["text"])

    yield call

    if server.SESSION.loaded:
        server.SESSION.unload()
    server.SESSION = before_session
    server.AUTHORING = before


def _exported(base: Path, text: str) -> Path:
    """`text` as a file inside the root's export directory."""
    path = base / EXPORT_DIR / "tool.rvl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _swapped_ticket(transport, tmp_path, candidate: dict) -> dict:
    base = tmp_path / "base.rvl"
    base.write_text(_BASE, encoding="utf-8")
    server.SESSION.approval_policy = "auto"
    assert transport("revl_load", {"files": [str(base)],
                                   "record": True})["ok"] is True
    result = transport("revl_swap", {**candidate, "replacing": ["ToolV1"]})
    assert result["ok"] is False, result
    assert result["approvalRequired"] is True, result
    return result["ticket"]


@needs_cordis
def test_an_export_area_swap_reports_the_host_code_the_inline_one_does(
        transport, tmp_path):
    """The same candidate through both doors must disclose the same thing.

    The inline swap is the control: it reported `unreviewedHostCode` before this
    fix. The export-area swap reported nothing, which is the defect. Asserting
    equality pins the fixed door to the working one and cannot pass by both
    being empty, because the emptiness is checked first.
    """
    inline = _swapped_ticket(transport, tmp_path, {"source": _CANDIDATE})
    assert inline.get("unreviewedHostCode"), (
        "the inline control reports no unreviewed host code, so this candidate "
        f"does not exercise the field at all: {inline}")
    # The field being non-empty is not enough: an attribution that keeps only the
    # OPERATOR's `notify` and drops the author's body would satisfy the equality
    # below while crediting the operator's code to the author. The body the author
    # really wrote has to be named, in both doors.
    assert "candidate_note" in {e["extern"] for e in inline["unreviewedHostCode"]}, inline

    exported = _exported(tmp_path, _CANDIDATE)
    from revl.export_area import in_export_area
    assert in_export_area(str(exported))  # really in the export area

    # Between the two doors, clear the session the way the established repair
    # fixture does (test_untrusted_repair_realm_2520.py) rather than assuming an
    # unload alone is enough: a held swap installs nothing, but it may leave
    # pending state behind, and the second load has to start from the same
    # position as the first or the two tickets are not comparable.
    server.SESSION.unload()
    server.SESSION.proposals = {}
    server.SESSION.pending_draft = None
    server.SESSION.draft = None
    through_file = _swapped_ticket(transport, tmp_path,
                                   {"files": [str(exported)]})

    assert through_file.get("unreviewedHostCode") == \
        inline["unreviewedHostCode"], through_file
    assert through_file.get("unreviewedHostCodeWarning") == \
        inline.get("unreviewedHostCodeWarning"), through_file


@needs_cordis
def test_a_swap_of_operator_files_outside_the_export_area_reports_nothing(
        transport, tmp_path):
    """The other direction, which the fix must not break.

    A candidate that is entirely jailed `files` OUTSIDE the export area is the
    operator's own, and its host bodies are the operator's. Reporting them as
    unreviewed agent code would raise a false alarm on the operator's own
    program.

    This control exists because the two ways to get it wrong fail in opposite
    directions, and only one of them is caught by the test above. Passing
    `host_bodies=None` instead of widening the condition would make the approval
    path compute the bodies itself with no transport test at all, and every
    operator load carrying host bodies would then raise a ticket. Pinning this
    case is what stops that simplification from looking correct.
    """
    from revl.export_area import in_export_area

    operator_file = tmp_path / "operator.rvl"
    operator_file.write_text(_CANDIDATE, encoding="utf-8")
    assert not in_export_area(str(operator_file))  # really outside the export area

    ticket = _swapped_ticket(transport, tmp_path,
                             {"files": [str(operator_file)]})
    assert not ticket.get("unreviewedHostCode"), (
        "an operator's own file is being reported as unreviewed agent host "
        f"code, which is a false alarm on their own program: {ticket}")
