"""The `deferred` modifier on the AUDIT surface — roadmap item 484 (GH #937).

A deferrable emission is the one crossing whose semantics differ from every
other emission: it does NOT fire the host at the call, it is held and flushed at
the commit prompt (an abort drops the queue). That is the difference between a
boundary that interrupts a human mid-session and one that does not — the whole
reason the modifier exists.

`extern emission deferred fn outbox_deliver(...)` from `revl-harness`
(`src/components/outbox.rvl`, H2) reduced to the crossing. The fact was in the
surface syntax, in the IR (`lower._lower_externs` records `deferred: True`), and
in the two consumers that branch on it (`revl.session_commit.refuse_deferred_on
_ownerless_tier`, `revl.mcp.approval`), but `revl audit` — the surface a reviewer
uses to enumerate the boundary — dropped it: prose printed
`outbox_deliver [emission] backends: py` and `--json` carried no `deferred` key.

Byte-compatibility is the constraint, exactly as for item 373's `reach`: an
IMMEDIATE emission carries no `deferred` key and prints exactly as before.
"""

import json

from revl.compiler import compile_source
from revl.audit_diff import audit_report


_DEFERRED = (
    "extern emission deferred fn outbox_deliver("
    "platform: Str, chat: Str, text: Str)\n"
    "  = @py {\n"
    "    return\n"
    "  }\n"
)

_IMMEDIATE = (
    "extern emission fn outbox_deliver("
    "platform: Str, chat: Str, text: Str)\n"
    "  = @py {\n"
    "    return\n"
    "  }\n"
)


def _entry(source: str) -> dict:
    ir = compile_source(source, "outbox.rvl")
    externs = {e["name"]: e for e in ir.get("externs") or []}
    return externs["outbox_deliver"]


# ---------------------------------------------------------------------------
# the IR carries it (this was never the bug — it is the premise)
# ---------------------------------------------------------------------------

def test_deferred_reaches_the_ir():
    assert _entry(_DEFERRED)["deferred"] is True


def test_immediate_has_no_deferred_key_byte_compat():
    # byte-identity: an immediate emission carries NO `deferred` key at all
    assert "deferred" not in _entry(_IMMEDIATE)


# ---------------------------------------------------------------------------
# the audit surface PRINTS the modifier (and stays byte-compatible without it)
# ---------------------------------------------------------------------------

def _audit_text(source: str, tmp_path, capsys) -> str:
    from revl.__main__ import main
    src = tmp_path / "m.rvl"
    src.write_text(source, encoding="utf-8")
    rc = main(["audit", str(src)])
    assert rc == 0
    return capsys.readouterr().out


def test_audit_prose_prints_the_deferred_modifier(tmp_path, capsys):
    out = _audit_text(_DEFERRED, tmp_path, capsys)
    # the modifier reads inside the bracket, in the order it is declared
    assert "outbox_deliver  [emission deferred]  backends: py" in out


def test_audit_prose_byte_compat_without_deferred(tmp_path, capsys):
    out = _audit_text(_IMMEDIATE, tmp_path, capsys)
    # an immediate emission prints exactly as before — no `deferred` token
    assert "outbox_deliver  [emission]  backends: py" in out
    assert "deferred" not in out


# ---------------------------------------------------------------------------
# both --json producers carry it: `__main__` (CLI) and `audit_report` (MCP)
# ---------------------------------------------------------------------------

def test_cli_json_carries_deferred(tmp_path, capsys):
    from revl.__main__ import main
    src = tmp_path / "m.rvl"
    src.write_text(_DEFERRED, encoding="utf-8")
    assert main(["audit", "--json", str(src)]) == 0
    body = json.loads(capsys.readouterr().out)
    entry = next(e for e in body["externs"] if e["name"] == "outbox_deliver")
    assert entry["deferred"] is True


def test_audit_report_carries_deferred():
    # the MCP `revl_audit` path builds its entry through `audit_diff.audit_report`
    report = audit_report(compile_source(_DEFERRED, "outbox.rvl"))
    entry = next(e for e in report["externs"] if e["name"] == "outbox_deliver")
    assert entry["deferred"] is True


def test_audit_report_byte_compat_without_deferred():
    report = audit_report(compile_source(_IMMEDIATE, "outbox.rvl"))
    entry = next(e for e in report["externs"] if e["name"] == "outbox_deliver")
    assert "deferred" not in entry
