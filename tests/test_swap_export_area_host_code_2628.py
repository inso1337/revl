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

A files-only candidate OUTSIDE every export area is the operator's own only
while host code is untrusted. Under trusted host code issue #2672 makes it
agent text for the disclosure gate, because the vouched-for host bodies can
rewrite any file the agent can reach; the swap ticket follows that gate, and
the tests below pin both trust modes.
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


def _clear_held_swap() -> None:
    """Clear the session the way the established repair fixture does
    (test_untrusted_repair_realm_2520.py) rather than assuming an unload alone
    is enough: a held swap installs nothing, but it may leave pending state
    behind, and the next load has to start from the same position as the first
    or the two tickets are not comparable."""
    server.SESSION.unload()
    server.SESSION.proposals = {}
    server.SESSION.pending_draft = None
    server.SESSION.draft = None


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
    inline_externs = {entry["extern"] for entry in inline["unreviewedHostCode"]}
    assert "candidate_note" in inline_externs, inline
    assert "notify" not in inline_externs, inline

    exported = _exported(tmp_path, _CANDIDATE)
    from revl.export_area import in_export_area
    assert in_export_area(str(exported))  # really in the export area

    # Between the two doors, clear the session the way the established repair
    # fixture does (test_untrusted_repair_realm_2520.py) rather than assuming an
    # unload alone is enough: a held swap installs nothing, but it may leave
    # pending state behind, and the second load has to start from the same
    # position as the first or the two tickets are not comparable.
    _clear_held_swap()
    through_file = _swapped_ticket(transport, tmp_path,
                                   {"files": [str(exported)]})
    exported_externs = {
        entry["extern"] for entry in through_file["unreviewedHostCode"]
    }
    assert "notify" not in exported_externs, through_file

    assert through_file.get("unreviewedHostCode") == \
        inline["unreviewedHostCode"], through_file
    assert through_file.get("unreviewedHostCodeWarning") == \
        inline.get("unreviewedHostCodeWarning"), through_file


@needs_cordis
def test_a_trusted_files_only_swap_outside_the_export_area_reports_the_authors_host_code(
        transport, tmp_path):
    """The same rule through the other trust mode (issue #2672).

    A files-only candidate outside every export area is the operator's own only
    while host code is untrusted. Under `--author-trust trusted` the operator
    has vouched for host bodies that can rewrite any file the agent can reach,
    so the disclosure gate treats the candidate as agent text. The approval
    ticket must not understate what a yes admits: it names the same host code
    the inline door names for the same candidate.

    Before #2672 the candidate compiled unprofiled and read as the operator's
    own, so this ticket named nothing while the gate would have skipped.
    """
    inline = _swapped_ticket(transport, tmp_path, {"source": _CANDIDATE})
    assert inline.get("unreviewedHostCode"), inline

    from revl.export_area import in_export_area

    operator_file = tmp_path / "operator.rvl"
    operator_file.write_text(_CANDIDATE, encoding="utf-8")
    assert not in_export_area(str(operator_file))

    _clear_held_swap()
    through_file = _swapped_ticket(transport, tmp_path,
                                   {"files": [str(operator_file)]})
    assert through_file.get("unreviewedHostCode") == \
        inline["unreviewedHostCode"], through_file
    assert through_file.get("unreviewedHostCodeWarning") == \
        inline.get("unreviewedHostCodeWarning"), through_file


@needs_cordis
def test_an_untrusted_files_only_swap_outside_the_export_area_reports_nothing(
        transport, tmp_path):
    """The operator's own file outside every export area while host code is
    UNTRUSTED, the shape in which that ownership is true (issue #2672).

    The compile reports no transport origin, so the file is the operator's
    jailed text and its host bodies are the operator's; reporting them as
    unreviewed agent code would raise a false alarm on their own program. The
    trusted direction is the test above, where the same file is agent text.
    """
    from revl.export_area import in_export_area

    server.set_authoring_trust(host_code=False)
    operator_file = tmp_path / "operator.rvl"
    operator_file.write_text(_CANDIDATE, encoding="utf-8")
    assert not in_export_area(str(operator_file))

    ticket = _swapped_ticket(transport, tmp_path,
                             {"files": [str(operator_file)]})
    assert not ticket.get("unreviewedHostCode"), (
        "an operator's own untrusted file is being reported as unreviewed "
        f"agent host code, which is a false alarm on their own program: {ticket}")
