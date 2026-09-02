"""Roadmap item 416c: two suspected confidentiality leaks of the SAME shape as
the two CONFIRMED item-256 Slice 3 leaks (`fix/secret-externalization`,
`backends/python/confidential.py`) — a value externalized past its declared
boundary into a durable record or a model's context.

1. `src/revl/mcp/approval.py::bind_resource_scope` binds the RUNTIME string
   argument at a `host`/`path`/`table`-named parameter straight into the
   capability spelling (`gateway.send(host="...")`) with no confidentiality check at
   all — not even the declared-`Secret[T]` positional marking `redact_args`
   already reads for an ordinary crossing argument. That spelling is the
   ticket body `record_approval_granted` writes to the durable WAL
   (`replay.py`) and what `replay_forward` re-derives into a later response —
   the exact two sinks item 256 Slice 3 fences for an ordinary argument,
   reached through a second, unguarded path.

2. `backends/python/runtime.py::ResponseValidationError` embeds the raw,
   untrusted model response in its message (`{value!r}` on a `const`/`enum`
   mismatch) and retains it verbatim as `.value`. A secret the program's own
   prompt fed the model can come back out in a malformed completion, so a
   host that logs the exception logs it too.

Both are fixed at the SAME choke point the confirmed fix used:
`confidential.redact_value`, an exact-value scrub against every value a
declared `Secret[T]` marking has already registered — never a heuristic, so an
ordinary (non-secret) value is unaffected. This file proves each leak
reproduces before the fix, is closed after it, and that an ordinary value
keeps working (false positives)."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
_SRC = ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from revl.compiler import compile_source  # noqa: E402
from revl.mcp.approval import ApprovalRequired  # noqa: E402
from revl.mcp.session import Session, SessionError  # noqa: E402
from revl.taint import REDACTED_SECRET  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the approval/WAL surfaces are proven against a live cordis-py "
           "composition — install it with `sh backends/python/setup.sh` and "
           "run under `backends/python/.venv/bin/pytest`",
)

CANARY_HOST = "SEKRIT-CANARY-416C-internal-db.corp"
PUBLIC_HOST = "api.public-service.example"

# `send`'s `host` is declared `Secret[Str]` on both the extern and the service
# operation; `ping`'s `host` is a plain `Str` at the same resource dimension —
# the false-positive control (an ordinary `host=` scope, like the landed N1
# `api.stripe.com` case, must keep rendering verbatim).
_SOURCE = (
    'extern emission[gateway.send] fn gwsend(host: Secret[Str], body: Str)'
    ' = @py { return }\n'
    'extern emission[gateway.ping] fn gwping(host: Str, body: Str)'
    ' = @py { return }\n'
    "service Gw {\n"
    "  emission fn send(host: Secret[Str], body: Str)\n"
    "  emission fn ping(host: Str, body: Str)\n"
    "}\n"
    "component Agent provides gw: Gw {\n"
    "  let seen = effect Map.new() undo seen.drop()\n"
    "  provide gw {\n"
    # a witnessed Map step alongside the emission (`send`'s only), so the call
    # lands on `forward_plan`'s replay list — an emission with no witnessed
    # effect at all is reported not-replayable rather than re-fired.
    '    fn send(host, body) { effect seen.insert("k", body) undo'
    ' seen.remove("k")\n'
    "                          emit gwsend(host, body) }\n"
    "    fn ping(host, body) { emit gwping(host, body) }\n"
    "  }\n"
    "}\n"
)

_BASE = compile_source(_SOURCE, "leak416c.rvl")


def _ir() -> dict:
    return copy.deepcopy(_BASE)


def _session() -> Session:
    s = Session()
    s.approval_policy = "auto"   # class-(c) still prompts under "auto" (246)
    return s


def _open_wal(session: Session, tmp_path) -> None:
    session.recorder.open_wal(str(tmp_path / "approval.wal"), session._generation)


def _wal_records(session: Session) -> list:
    wal_path = session.recorder.wal.path
    session.unload()
    return [json.loads(line) for line in Path(wal_path).read_text().splitlines()
            if line.strip()]


# ===========================================================================
# LEAK 1 — the resource-scoped capability spelling
# ===========================================================================

@needs_cordis
def test_the_ticket_never_shows_the_secret_host(tmp_path):
    """The FIRST, synchronous response to the call that supplied the value:
    `exc.ticket["classCCapabilities"]`/`resourceScopes` must not carry the raw
    `Secret[Str]` host — this is the JSON `_approval_required` (server.py)
    hands back over MCP, so an unredacted spelling here is a direct
    externalization into a model's context, not just a durable-storage risk."""
    session = _session()
    session.load(_ir(), record=True)
    _open_wal(session, tmp_path)
    with pytest.raises(ApprovalRequired) as exc:
        session.call("gw", "send", [CANARY_HOST, "hi"])
    blob = json.dumps(exc.value.ticket)
    assert CANARY_HOST not in blob
    assert REDACTED_SECRET in blob
    assert exc.value.ticket["classCCapabilities"] == \
        [f'gateway.send(host="{REDACTED_SECRET}")']
    assert exc.value.ticket["resourceScopes"] == \
        {"gateway.send": f'gateway.send(host="{REDACTED_SECRET}")'}


@needs_cordis
def test_the_wal_grant_record_never_holds_the_secret_host(tmp_path):
    """LEAK 1, durable half. A human yes writes `approval-granted` to the WAL
    (`record_approval_granted`, `replay.py`) with the ticket's
    `classCCapabilities`/`resourceScopes` spliced in verbatim
    (`_distillation_ledger_fields`, session.py) — created 0644, plaintext at
    rest, exactly the WAL sink item 256 Slice 3 already fences for an
    ordinary argument."""
    session = _session()
    session.load(_ir(), record=True)
    _open_wal(session, tmp_path)
    with pytest.raises(ApprovalRequired) as exc:
        session.call("gw", "send", [CANARY_HOST, "hi"])
    approval = session.approve_ticket(exc.value.ticket["hash"])
    assert approval["approved"]
    records = _wal_records(session)
    blob = json.dumps(records)
    assert CANARY_HOST not in blob
    granted = [r for r in records if r.get("record") == "approval-granted"]
    assert granted, records
    assert granted[-1]["resourceScopes"] == \
        {"gateway.send": f'gateway.send(host="{REDACTED_SECRET}")'}


@needs_cordis
def test_replay_forward_never_re_derives_the_secret_host(tmp_path):
    """LEAK 1, the model-context half the audit sharpened. `replay_forward`
    re-invokes the original call, and a still-class-(c) crossing raises
    `ApprovalRequired` again — its FRESH ticket is copied straight into
    `replay_forward`'s own result (session.py, `outcome["ticket"] = exc.ticket`),
    a SEPARATE, later tool response than the one that first asked. Since the
    fix is at `bind_resource_scope` (the ticket's only source), the re-derived
    ticket is safe for the same reason the first one is — proven here rather
    than assumed."""
    session = _session()
    session.load(_ir(), record=True)
    _open_wal(session, tmp_path)
    with pytest.raises(ApprovalRequired) as exc:
        session.call("gw", "send", [CANARY_HOST, "hi"])
    session.approve_ticket(exc.value.ticket["hash"])
    session.call("gw", "send", [CANARY_HOST, "hi"])   # fires once, consumes it

    session.step_back("Agent", 1, force=True)
    report = session.replay_forward("Agent", 1)
    outcome = next(r for r in report["replayed"] if r["method"] == "send")
    assert outcome.get("approvalRequired") is True
    blob = json.dumps(outcome["ticket"])
    assert CANARY_HOST not in blob
    assert REDACTED_SECRET in blob
    # the ALREADY-landed item-256 Slice 3 fix covers the replayed args
    # themselves (declared `Secret[Str]` on the service op too) — confirmed
    # here as the other half of what a replayed response exposes.
    assert CANARY_HOST not in json.dumps(outcome["args"])


@needs_cordis
def test_a_standing_grant_cannot_be_scoped_to_the_redacted_placeholder(tmp_path):
    """A resource-scoped standing grant must be refused when its capability
    names the placeholder rather than a real value: every secret-valued
    crossing of `send` binds to the SAME placeholder, so a grant minted
    against it would silently cover every future crossing regardless of the
    (different) real value each one actually carries — the over-authorization
    a resource-scoped grant exists to prevent."""
    session = _session()
    session.load(_ir(), record=True)
    _open_wal(session, tmp_path)
    with pytest.raises(ApprovalRequired):
        session.call("gw", "send", [CANARY_HOST, "hi"])
    with pytest.raises(SessionError, match="Secret"):
        session.mint_standing_grant(
            capability=f'gateway.send(host="{REDACTED_SECRET}")', uses=5)


# ---------------------------------------------------------------------------
# FALSE POSITIVES — an ordinary resource-scope value is unaffected
# ---------------------------------------------------------------------------

@needs_cordis
def test_an_ordinary_host_still_renders_verbatim_in_the_ticket(tmp_path):
    """`ping`'s `host` is a plain `Str` — redaction must not blanket-erase the
    resource-scope surface (the landed N1 case: `api.stripe.com` in a ledger
    read is a legitimate audit target, not a leak)."""
    session = _session()
    session.load(_ir(), record=True)
    _open_wal(session, tmp_path)
    with pytest.raises(ApprovalRequired) as exc:
        session.call("gw", "ping", [PUBLIC_HOST, "hi"])
    assert exc.value.ticket["classCCapabilities"] == \
        [f'gateway.ping(host="{PUBLIC_HOST}")']


@needs_cordis
def test_an_ordinary_host_still_renders_verbatim_in_the_wal(tmp_path):
    """Under the OPT-IN `bound` posture. The host here is a caller argument, so
    since roadmap 427 F8's default flip it is withheld unless the operator asks
    for the item-251 fold — see the two `..._is_the_default_...` tests below."""
    session = _session()
    session.approval_record_values = "bound"
    session.load(_ir(), record=True)
    _open_wal(session, tmp_path)
    with pytest.raises(ApprovalRequired) as exc:
        session.call("gw", "ping", [PUBLIC_HOST, "hi"])
    session.approve_ticket(exc.value.ticket["hash"])
    records = _wal_records(session)
    granted = [r for r in records if r.get("record") == "approval-granted"]
    assert PUBLIC_HOST in json.dumps(granted[-1])


@needs_cordis
def test_a_standing_grant_still_mints_for_an_ordinary_host(tmp_path):
    session = _session()
    session.load(_ir(), record=True)
    _open_wal(session, tmp_path)
    with pytest.raises(ApprovalRequired):
        session.call("gw", "ping", [PUBLIC_HOST, "hi"])
    grant = session.mint_standing_grant(
        capability=f'gateway.ping(host="{PUBLIC_HOST}")', uses=5)
    assert grant["granted"]


# ===========================================================================
# LEAK 2 — the raw model response on a validation fault
# ===========================================================================

CANARY_TOKEN = "SEKRIT-CANARY-416C-model-response"


def _runtime():
    import runtime  # noqa: PLC0415 — backend module, path set above

    return runtime


@pytest.fixture(autouse=True)
def _fresh_secret_registry():
    import confidential  # noqa: PLC0415

    confidential.forget_secret_values()
    yield
    confidential.forget_secret_values()


def test_a_registered_secret_echoed_by_the_model_is_redacted_in_the_message():
    """LEAK 2. `CANARY_TOKEN` is registered exactly as a declared `Secret[T]`
    marking would register it (`confidential.register_secret_value`, the same
    call `taint`-marked crossings make). A model response that echoes it back
    verbatim on a `const` mismatch must not embed it in the fault message."""
    runtime = _runtime()
    import confidential  # noqa: PLC0415

    confidential.register_secret_value(CANARY_TOKEN)
    schema = {"type": "object", "properties": {"tag": {"const": "Final"}},
             "required": ["tag"]}
    with pytest.raises(runtime.ResponseValidationError) as exc:
        runtime.validate_response({"tag": CANARY_TOKEN}, schema, "Agent.run")
    assert CANARY_TOKEN not in str(exc.value)
    assert runtime.confidential.REDACTED in str(exc.value)


def test_a_registered_secret_echoed_by_the_model_is_redacted_on_the_exception():
    """The retained `.value` attribute is the other sink: a host that logs
    `exc.value` rather than `str(exc)` (a crash reporter, a bare `repr`) must
    not recover the secret either."""
    runtime = _runtime()
    import confidential  # noqa: PLC0415

    confidential.register_secret_value(CANARY_TOKEN)
    schema = {"type": "object", "properties": {"tag": {"const": "Final"}},
             "required": ["tag"]}
    with pytest.raises(runtime.ResponseValidationError) as exc:
        runtime.validate_response({"tag": CANARY_TOKEN}, schema, "Agent.run")
    blob = json.dumps(exc.value.value)
    assert CANARY_TOKEN not in blob
    assert exc.value.value == {"tag": confidential.REDACTED}


def test_an_enum_mismatch_also_redacts_the_response():
    runtime = _runtime()
    import confidential  # noqa: PLC0415

    confidential.register_secret_value(CANARY_TOKEN)
    schema = {"enum": ["Final", "Pending"]}
    with pytest.raises(runtime.ResponseValidationError) as exc:
        runtime.validate_response(CANARY_TOKEN, schema, "Agent.run")
    assert CANARY_TOKEN not in str(exc.value)


# ---------------------------------------------------------------------------
# FALSE POSITIVES — an ordinary (never-registered) response is untouched
# ---------------------------------------------------------------------------

def test_an_unregistered_malformed_response_is_still_rendered_verbatim():
    """FALSE POSITIVE, and the whole point: the retry loop and a developer
    debugging a schema mismatch need to see the actual malformed value in the
    overwhelmingly common case — an ordinary bad completion, not a secret."""
    runtime = _runtime()
    schema = {"type": "object", "properties": {"tag": {"const": "Final"}},
             "required": ["tag"]}
    with pytest.raises(runtime.ResponseValidationError) as exc:
        runtime.validate_response({"tag": "Pending"}, schema, "Agent.run")
    assert "Pending" in str(exc.value)
    assert exc.value.value == {"tag": "Pending"}


def test_a_short_or_default_registered_value_never_over_redacts():
    """`confidential._MIN_MARKABLE` refuses to register a short value (a coin
    flip against ordinary response content) — so this stays a no-op, matching
    the existing timeline/WAL behaviour for the same guard."""
    runtime = _runtime()
    import confidential  # noqa: PLC0415

    confidential.register_secret_value("no")   # too short: never registered
    schema = {"enum": ["Final", "Pending"]}
    with pytest.raises(runtime.ResponseValidationError) as exc:
        runtime.validate_response("no", schema, "Agent.run")
    assert "no" in str(exc.value)


# ===========================================================================
# ROADMAP 425 F3 / 427 F5 — the UNDECLARED half of LEAK 1
#
# 416c fixed the DECLARED half: a `Secret[T]` resource dimension binds to the
# placeholder before any sink. An UNDECLARED one binds verbatim, by design, so a
# sensitive value passed through a plain `Str` parameter named `path`/`host`/
# `table` still reached the ticket, `_approval_records` and the durable WAL.
# Reproduced above by `test_an_ordinary_host_still_renders_verbatim_in_the_wal`,
# which is that behaviour asserted deliberately: it IS item 251's N1, the reason
# a distilled rule can name a target instead of comparing an opaque args hash.
#
# So the residual is not closed by redacting harder. Blanket redaction would make
# every ticket unanswerable (an operator who cannot see the target cannot decide),
# and a placeholder in the ledger would be WORSE than the leak, because every
# distinct target would fold to one shape and a rule minted over it would cover
# all of them — the over-authorization `mint_standing_grant` already refuses a
# placeholder-scoped grant for.
#
# What is resolved here instead:
#   * the promotion stops being INVISIBLE. A call's arguments are only ever
#     hashed into the ticket (`argsDigest`) EXCEPT at a parameter whose name sits
#     in `cap_order._REGISTRY`, whose runtime value is lifted out and, on a yes,
#     written verbatim to a durable cross-session log. The ticket now says so, and
#     names the `Secret[Str]` declaration that changes it;
#   * and the durability becomes an OPERATOR decision
#     (`--approval-record-values`).
#
# ROADMAP 427 F8 then settled the DEFAULT, which the first pass deliberately left
# at `bound` (what shipped). It is now `withheld`, because the two mistakes are
# not symmetric: recording is irreversible and silent (a plaintext cross-session
# file on disk, holding somebody else's value, selected by whether a parameter
# happens to be NAMED `path`/`host`/`table`), while withholding costs only the
# distiller's fold over caller-argument targets — visible, recoverable, and now
# SELF-EXPLAINING, since `distill.Reason.RESOURCE_SCOPE_UNRECORDED` names the
# flag that gets the fold back. Author-written literal targets are recorded under
# both modes, so a composition that names its destinations in source pays nothing.
# Both behaviours are pinned below so the choice cannot drift back by accident.
#
# NEGATIVE RESULT on the third option, recording a SCOPED DERIVATIVE instead of
# the raw value (a per-segment keyed hash, say, which would preserve the equality
# and longest-common-prefix joins `_resource_join` needs): it cannot work here.
# The distilled artifact is a rule an operator READS and applies
# (`AutoApproveRule.to_dsl()`), and a rule reading `fs.write(path="/9f3a/ab12")`
# cannot be reviewed; the auto-approve matcher would have to re-derive the same
# tokens at every later crossing, so the key must outlive the session and sit
# beside the log it protects; and path/host segments are low-entropy enough that
# a keyed hash over them is a dictionary attack, not a redaction. The fold and
# the plaintext are the same fact, which is why this is a posture and not a
# transform.
# ===========================================================================

_CALLER_TARGET = "/var/secrets/tenant-9f2c/prod-db-dump.sql"
_LITERAL_TARGET = "/var/spool/outbox"

_DURABILITY_SOURCE = (
    'extern emission[fs.write] fn arch(path: Str, body: Str) = @py { return }\n'
    'extern emission[fs.spool] fn spool(path: Str, body: Str) = @py { return }\n'
    "service Ops {\n"
    "  emission fn store(path: Str, body: Str)\n"
    "  emission fn park(body: Str)\n"
    "}\n"
    "component Filer provides ops: Ops {\n"
    "  provide ops {\n"
    "    fn store(path, body) { emit arch(path, body) }\n"
    f'    fn park(body) {{ emit spool("{_LITERAL_TARGET}", body) }}\n'
    "  }\n"
    "}\n"
)


def _durability_session(mode: str = "withheld") -> Session:
    session = _session()
    session.approval_record_values = mode
    session.load(compile_source(_DURABILITY_SOURCE, "filer.rvl"), record=True)
    return session


@needs_cordis
def test_the_ticket_discloses_that_the_target_is_a_caller_value(tmp_path):
    """The operator's yes is what persists the value, so the prompt says so —
    the same move item 425 F1 made with `unreviewedHostCode`. The target itself
    is NOT hidden: the caller just sent it, and an operator who cannot see it
    cannot answer."""
    session = _durability_session(mode="bound")
    _open_wal(session, tmp_path)
    with pytest.raises(ApprovalRequired) as exc:
        session.call("ops", "store", [_CALLER_TARGET, "hi"])
    ticket = exc.value.ticket
    assert ticket["resourceScopesFromCallerArgs"] == ["fs.write"]
    disclosure = ticket["resourceScopeDurability"]
    assert "durable approval log" in disclosure
    assert "`Secret[Str]`" in disclosure          # item 274: the fix is named
    assert "--approval-record-values withheld" in disclosure
    assert _CALLER_TARGET in json.dumps(ticket)   # the decision is still legible
    session.unload()


@needs_cordis
def test_the_disclosure_states_the_posture_this_session_actually_runs(tmp_path):
    """Roadmap 427 F8. A disclosure describing the OTHER session's behaviour is
    worse than none — it would teach an operator that the field is boilerplate.
    Under the default the sentence says the value stays out of the log, and names
    what that costs; under `bound` it says the opposite. The provenance half (the
    target is a caller value, not an author literal) is stated under both."""
    withheld = _durability_session()
    _open_wal(withheld, tmp_path)
    with pytest.raises(ApprovalRequired) as exc:
        withheld.call("ops", "store", [_CALLER_TARGET, "hi"])
    said = exc.value.ticket["resourceScopeDurability"]
    assert "the CALLER passed in" in said              # provenance, both modes
    assert "does NOT reach the durable" in said
    assert "--approval-record-values bound" in said    # and how to get the fold
    withheld.unload()

    bound = _durability_session(mode="bound")
    with pytest.raises(ApprovalRequired) as exc:
        bound.call("ops", "store", [_CALLER_TARGET, "hi"])
    said = exc.value.ticket["resourceScopeDurability"]
    assert "the CALLER passed in" in said
    assert "writes that value verbatim into the durable approval log" in said
    bound.unload()


@needs_cordis
def test_an_author_written_literal_target_is_not_a_caller_value(tmp_path):
    """The distinction the whole rule rests on: a literal is in the source
    already and discloses nothing about the caller, so it is neither marked nor
    withheld."""
    session = _durability_session(mode="withheld")
    _open_wal(session, tmp_path)
    with pytest.raises(ApprovalRequired) as exc:
        session.call("ops", "park", ["hi"])
    ticket = exc.value.ticket
    assert "resourceScopesFromCallerArgs" not in ticket
    assert "resourceScopeDurability" not in ticket
    session.approve_ticket(ticket["hash"])
    granted = [r for r in _wal_records(session)
               if r.get("record") == "approval-granted"]
    assert granted[-1]["resourceScopes"] == \
        {"fs.spool": f'fs.spool(path="{_LITERAL_TARGET}")'}


@needs_cordis
def test_withheld_is_the_default_so_a_caller_target_never_reaches_the_log(
        tmp_path):
    """Roadmap 427 F8, the decision itself. An operator who never reads the flag
    does not silently persist somebody else's values: with NOTHING configured, the
    caller's target is absent from the durable log and recorded as the fail-closed
    UNRECORDED shape. Its twin below pins the opt-in, so the default is a choice
    rather than an accident."""
    assert Session().approval_record_values == "withheld"
    session = _durability_session()          # i.e. nothing configured
    _open_wal(session, tmp_path)
    with pytest.raises(ApprovalRequired) as exc:
        session.call("ops", "store", [_CALLER_TARGET, "hi"])
    session.approve_ticket(exc.value.ticket["hash"])
    records = _wal_records(session)
    assert _CALLER_TARGET not in json.dumps(records)
    granted = [r for r in records if r.get("record") == "approval-granted"]
    assert granted[-1]["resourceScopes"] == {"fs.write": None}


@needs_cordis
def test_bound_is_still_available_and_still_records_the_caller_target(tmp_path):
    """The OTHER behaviour, pinned so the flip is a posture and not a removal.
    `--approval-record-values bound` records the destination that actually
    crossed, which is what item 251's N1 needs to distil a rule that NAMES it."""
    session = _durability_session(mode="bound")
    _open_wal(session, tmp_path)
    with pytest.raises(ApprovalRequired) as exc:
        session.call("ops", "store", [_CALLER_TARGET, "hi"])
    session.approve_ticket(exc.value.ticket["hash"])
    granted = [r for r in _wal_records(session)
               if r.get("record") == "approval-granted"]
    assert granted[-1]["resourceScopes"] == \
        {"fs.write": f'fs.write(path="{_CALLER_TARGET}")'}


def test_the_withheld_fold_loss_explains_itself_and_names_the_flag():
    """The only substantive argument for keeping `bound` was that flipping would
    SILENTLY disable the item-251 fold. It is not silent: the distiller's
    fail-closed refusal for exactly this shape names the flag that restores it, so
    the operator who loses a rule is told why and what to change."""
    from revl import distill  # noqa: PLC0415

    rec = {"component": "Filer", "session": "s", "operator": "op",
           "resourceScopes": {"fs.write": None}}
    refusal = distill._project(rec, "fs.write")
    assert refusal.reason is distill.Reason.RESOURCE_SCOPE_UNRECORDED
    assert "--approval-record-values bound" in refusal.detail


@needs_cordis
def test_withheld_keeps_the_caller_target_out_of_the_durable_log(tmp_path):
    """The operator's other option. Both channels have to drop it: the
    distiller reads an inline-parameterised `classCCapabilities` entry in
    PREFERENCE to the `resourceScopes` map, so withholding one and not the other
    would leave the value in the log anyway."""
    session = _durability_session(mode="withheld")
    _open_wal(session, tmp_path)
    with pytest.raises(ApprovalRequired) as exc:
        session.call("ops", "store", [_CALLER_TARGET, "hi"])
    session.approve_ticket(exc.value.ticket["hash"])
    records = _wal_records(session)
    assert _CALLER_TARGET not in json.dumps(records)
    granted = [r for r in records if r.get("record") == "approval-granted"]
    # the fail-closed "resource-bearing but UNRECORDED" shape `distill` already
    # understands, not a placeholder that would fold every target into one
    assert granted[-1]["resourceScopes"] == {"fs.write": None}
    assert granted[-1]["classCCapabilities"] == ["fs.write"]


@needs_cordis
def test_a_restored_session_replays_no_earlier_principals_arguments(tmp_path):
    """NEGATIVE RESULT, pinned so it cannot rot. The finding also named
    `replay_forward` echoing raw `item["args"]` into a later response as a route
    into the model's context. It is not a cross-principal one: a snapshot carries
    SOURCES, not recorded calls, and `restore` boots a fresh runtime with a fresh
    recorder, so every step a session can replay was recorded by a call that
    session's own driver made. The args it echoes are the driver's own."""
    session = _session()
    session.approval_policy = None
    session.load(compile_source(_DURABILITY_SOURCE, "filer.rvl"), record=True,
                 origin={"source": _DURABILITY_SOURCE})
    session.call("ops", "park", ["hi"])
    snap = session.snapshot()
    assert "hi" not in json.dumps(snap.get("meta") or {})
    session.unload()

    restored = Session()
    restored.restore(snap)
    try:
        plan = restored.replay_forward("Filer", 0)
        assert plan["replay"] == []          # nothing earlier to replay at all
    finally:
        restored.unload()
