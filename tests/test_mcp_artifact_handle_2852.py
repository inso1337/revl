"""Content handles: an id for formatted text the session already holds (#2852).

The producer is `revl_fmt` with `handle: true`, which answers with an
`artifactId` in place of the formatted text. The consumer is `revl_check`,
which accepts that id in place of `source` and checks the text it names. The
text form of both verbs is unchanged, so a caller that wants the bytes keeps
asking for them and getting them.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.mcp import server  # noqa: E402
from revl.mcp.canonical import digest  # noqa: E402
from revl.mcp.server import Session, handle  # noqa: E402

PULSE = (ROOT / "examples" / "pulse.rvl").read_text(encoding="utf-8")


@pytest.fixture
def session(monkeypatch):
    """A fresh session per test: a handle is session scoped, so a handle from
    one test must never resolve in another."""
    fresh = Session()
    monkeypatch.setattr(server, "SESSION", fresh)
    return fresh


def _round(tool: str, arguments: dict) -> tuple[dict, str]:
    """A tool call, as its structured answer and the exact text a client gets."""
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return (response["result"]["structuredContent"],
            response["result"]["content"][0]["text"])


def _call(tool: str, arguments: dict) -> dict:
    return _round(tool, arguments)[0]


def test_fmt_handle_then_check_round_trips_without_the_text(session):
    formatted = _call("revl_fmt", {"source": PULSE})["formatted"]
    held = _call("revl_fmt", {"source": PULSE, "handle": True})
    assert "formatted" not in held
    assert held["artifactId"] == digest(formatted)

    checked = _call("revl_check", {"artifactId": held["artifactId"]})
    assert checked["ok"] is True
    # the checked text is the formatted text: the canonical digest the check
    # answers with is the id the producer minted
    assert checked["canonicalSource"]["digest"] == held["artifactId"]

    direct = _call("revl_check", {"source": formatted})
    assert direct["ok"] is True
    assert checked["selfCheck"] == direct["selfCheck"]


def test_a_handle_answer_replaces_the_formatted_body(session):
    plain, plain_text = _round("revl_fmt", {"source": PULSE})
    held, held_text = _round("revl_fmt", {"source": PULSE, "handle": True})
    assert plain["formatted"]
    assert "formatted" not in held
    assert held["artifactId"] == digest(plain["formatted"])
    # the two calls differ by exactly one key: the formatted body is gone and
    # the id is in its place, everything else is byte-identical
    assert plain["sessionState"] == held["sessionState"]
    replaced = {}
    for key, value in plain.items():
        if key == "formatted":
            replaced["artifactId"] = held["artifactId"]
        else:
            replaced[key] = value
    assert held_text == json.dumps(replaced, indent=2)
    # so the answer is smaller by nearly the body: the id line that replaces
    # the body's JSON literal is under 32 bytes
    delta = len(plain_text) - len(held_text)
    assert delta >= len(plain["formatted"]) - 32


def test_fmt_text_form_is_unchanged(session):
    plain = _call("revl_fmt", {"source": PULSE})
    assert plain["ok"] is True
    assert plain["admitted"] is True
    assert plain["formatted"]
    assert plain["proof"] == "unchanged"
    # the text form mints nothing, so an answer that wants the bytes is the
    # same answer it was before the handle existed (the answer ledger reads
    # this form)
    assert "artifactId" not in plain
    assert not session.artifacts


def test_check_text_form_is_unchanged(session):
    checked = _call("revl_check", {"source": PULSE})
    assert checked["ok"] is True
    assert checked["canonicalSource"]["digest"] == digest(
        _call("revl_fmt", {"source": PULSE})["formatted"])


def test_unknown_handle_is_refused_by_name(session):
    unknown = "sha256:" + "0" * 16
    refused = _call("revl_check", {"artifactId": unknown})
    assert refused["ok"] is False
    assert refused["code"] == "UNKNOWN_ARTIFACT"
    assert refused["diagnostics"][0]["category"] == "usage"
    assert unknown in refused["diagnostics"][0]["message"]


def test_stale_handle_is_refused_by_name(session):
    held = _call("revl_fmt", {"source": PULSE, "handle": True})
    # a swap moves the generation; the handle is bound to the unit it was
    # minted in, so it no longer resolves against the session that moved on
    session._generation += 1
    refused = _call("revl_check", {"artifactId": held["artifactId"]})
    assert refused["ok"] is False
    assert refused["code"] == "STALE_ARTIFACT"
    assert refused["diagnostics"][0]["category"] == "usage"
    assert held["artifactId"] in refused["diagnostics"][0]["message"]


def test_a_handle_takes_no_text_of_its_own(session):
    held = _call("revl_fmt", {"source": PULSE, "handle": True})
    refused = _call("revl_check", {"artifactId": held["artifactId"],
                                   "source": PULSE})
    assert refused["ok"] is False
    assert refused["diagnostics"][0]["category"] == "usage"
    assert held["artifactId"] in refused["diagnostics"][0]["message"]


def test_a_handle_takes_no_held_session_form_of_its_own(session):
    held = _call("revl_fmt", {"source": PULSE, "handle": True})
    refused = _call("revl_check", {"artifactId": held["artifactId"],
                                   "session": True})
    assert refused["ok"] is False
    assert refused["diagnostics"][0]["category"] == "usage"
    # the fix names the two arguments the caller actually sent
    message = refused["diagnostics"][0]["message"]
    assert "`session: true`" in message
    assert "`artifactId`" in message
