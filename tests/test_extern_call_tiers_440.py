"""Extern call tiers: the READ tier and the RE-ISSUE SEAM (roadmap item 440).

Item 309 already gave `revl recover` a journal, a branch by tier, and a
fail-closed ambiguous outcome. Item 440 is the remainder:

(a) **the read tier.** 309's register was two-valued from recovery's side —
    declared-idempotent or fenced — so a call that CHANGES NOTHING was treated
    as unkeyed, fenced on its first attempt and escalated to an operator on the
    second. `undo pure <inverse>(result)` lowers to `register: "read"`, rides the
    WAL descriptor, and makes `revl recover` idempotent over that inverse.

    The tier is DECLARED, never derived from the `pure` classification alone:
    `pure` is checked for shape only ("no observable effect" is the wording of
    the declaration, never a proof) and shipped examples classify mutating host
    bodies `pure`, so deriving the tier would resolve an ambiguity optimistically.
    Lower holds `undo pure` to a `pure`-classified callee, which anchors the
    claim to the classification without trusting the classification alone.

(b) **the re-issue seam.** `recovery.py` carried `TODO(309-slice3)`: an owed
    deferred emission whose extern is `idempotent(key: p)` MAY be auto-fired,
    but firing needed a way to re-invoke the call in a fresh process. The seam is
    `World.reissue` — the same adapter that already carries `apply_inverse` and
    `apply_compensation` — gated by the item-33 policy knob `recovery may
    re-issue owed emissions [(strength: <level>)]`.

Every fail-closed property is asserted here alongside the payoff: with no policy
nothing fires; with the policy on, an owed emission carrying NO register is still
never fired; and a `declared` (trust-me) re-issue spends a durable fence, so a
crash between the fence and the fire leaves `outcome: "unknown"` rather than a
second unproven attempt.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from revl.compiler import compile_files            # noqa: E402
from revl.errors import RevlError                  # noqa: E402
from revl.policy import parse_policy               # noqa: E402
from revl.recovery import DictWorld, recover       # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _compile(tmp_path: Path, source: str) -> dict:
    path = tmp_path / "t.rvl"
    path.write_text(source, encoding="utf-8")
    return compile_files([str(path)])


def _extern(ir: dict, name: str) -> dict:
    return next(e for e in ir["externs"] if e["name"] == name)


def _wal(tmp_path: Path, records: list, name: str = "w.jsonl") -> str:
    path = tmp_path / name
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    return str(path)


def _inverse_descriptor(register: str | None = None,
                        undo_idempotent: bool = False) -> dict:
    """One witnessed (`transactional`) discharge-descriptor, undischarged."""
    return {
        "record": "discharge-descriptor", "seq": 1, "entry": "transactional",
        "call": {"receiver": "Db", "method": "probe_charge", "args": ["w1"]},
        "origin": {"phase": "activation", "key": "Db"},
        "witness": "w1", "idempotency": None,
        **({"undo_idempotent": True} if undo_idempotent else {}),
        **({"register": register} if register else {}),
    }


def _owed_emission(seq: int, receiver: str, method: str, args: list, *,
                   register: str | None = None,
                   idempotency: str | None = None) -> dict:
    """One class-(b) deferred-emission descriptor with no `flushed` record."""
    return {
        "record": "deferred-emission", "seq": seq,
        "call": {"receiver": receiver, "method": method, "args": list(args)},
        "origin": {"key": receiver, "method": method},
        "idempotency": idempotency,
        **({"register": register} if register else {}),
    }


READ_SOURCE = """
pub extern pure fn probe_charge(w: ChargeWitness) = @py { pass }
pub extern witnessed[db] fn charge(id: Str) -> Result[ChargeWitness, Str]
    undo pure probe_charge(result)
    = @py { return ("ok", None) }
"""


# ---------------------------------------------------------------------------
# (a) the read tier: surface, derivation, refusals
# ---------------------------------------------------------------------------


def test_undo_pure_lowers_to_the_read_register(tmp_path):
    """`undo pure` is the third register value and it outranks `keyed`."""
    from revl.lower import _REGISTER_RANK

    ir = _compile(tmp_path, READ_SOURCE)
    ext = _extern(ir, "charge")
    assert ext["undo_read"] is True
    assert ext["register"] == "read"
    # a read crosses nothing at all, so it is strictly stronger than a call that
    # crosses and leans on a remote's dedup contract.
    assert _REGISTER_RANK["read"] > _REGISTER_RANK["keyed"]


def test_a_pre_440_extern_is_byte_identical(tmp_path):
    """No `undo pure` — no `undo_read`, no `register`. Additivity."""
    ir = _compile(tmp_path, """
pub extern pure fn probe_charge(w: ChargeWitness) = @py { pass }
pub extern witnessed[db] fn charge(id: Str) -> Result[ChargeWitness, Str]
    undo probe_charge(result)
    = @py { return ("ok", None) }
""")
    ext = _extern(ir, "charge")
    assert "undo_read" not in ext
    assert "register" not in ext


def test_undo_pure_over_a_non_pure_inverse_is_refused(tmp_path):
    """The claim is anchored to the classification revl already has: the named
    inverse must itself be a `pure`-classified extern."""
    with pytest.raises(RevlError) as excinfo:
        _compile(tmp_path, """
pub extern emission[net] fn ping(id: Str) = @py { pass }
pub extern witnessed[db] fn charge(id: Str) -> Result[ChargeWitness, Str]
    undo pure ping(id)
    = @py { return ("ok", None) }
""")
    assert "declares `undo pure`" in str(excinfo.value)
    assert "classified `emission`" in str(excinfo.value)


def test_undo_idempotent_pure_together_is_refused(tmp_path):
    """Two different claims about one inverse. Recovery reports which claim it
    replayed under, so it must be exactly one."""
    with pytest.raises(RevlError) as excinfo:
        _compile(tmp_path, """
pub extern pure fn probe_charge(w: ChargeWitness) = @py { pass }
pub extern witnessed[db] fn charge(id: Str) -> Result[ChargeWitness, Str]
    undo idempotent pure probe_charge(result)
    = @py { return ("ok", None) }
""")
    assert "both `undo idempotent` and `undo pure`" in str(excinfo.value)


def test_read_register_is_emitted_into_the_transactional_kwargs():
    """The py emitter passes `register='read'` down to the runtime, which writes
    it onto the WAL discharge-descriptor."""
    from emit import _transactional_register_kwargs  # noqa: PLC0415

    assert _transactional_register_kwargs({"register": "read"}) \
        == ", register='read'"
    assert _transactional_register_kwargs({}) == ""


# ---------------------------------------------------------------------------
# (a) exit test: a read recovers with NO operator prompt; a non-idempotent
#     inverse still refuses.
# ---------------------------------------------------------------------------


def test_read_tier_inverse_never_reaches_an_operator(tmp_path):
    """Exit (3), first half. Two recovery runs over the same WAL: the read-tier
    inverse replays on both, spends no fence, and leaves residue CLEAN — an
    operator is never asked about a call that changes nothing."""
    path = _wal(tmp_path, [_inverse_descriptor(register="read")])

    first = recover(path, world=DictWorld())
    second = recover(path, world=DictWorld())

    for report in (first, second):
        assert report["verdict"] == "rolled-back"
        assert report["residue"]["clean"] is True
        assert report["residue"]["outstanding"] == []
        assert [e["replay"] for e in report["transactionalRolledBack"]] == ["read"]
        assert report["fencedDeferred"] == []
    # and nothing was fenced: a read spends no at-most-once attempt.
    assert "replay-fence" not in Path(path).read_text(encoding="utf-8")


def test_non_idempotent_inverse_still_refuses_on_the_second_run(tmp_path):
    """Exit (3), second half. The read tier changes NOTHING for an unregistered
    inverse: one fenced attempt, then `outcome: "unknown"` for a human."""
    path = _wal(tmp_path, [_inverse_descriptor()])

    first = recover(path, world=DictWorld())
    assert [e["replay"] for e in first["transactionalRolledBack"]] == ["fenced"]
    assert first["residue"]["clean"] is True

    second = recover(path, world=DictWorld())
    assert second["transactionalRolledBack"] == []
    assert second["residue"]["clean"] is False
    (record,) = second["residue"]["outstanding"]
    assert record["outcome"] == "unknown"
    assert "cannot be proven safe" in record["error"]["message"]


def test_declared_idempotent_inverse_keeps_its_309_class(tmp_path):
    """The read tier is additive: a 309 `undo idempotent` inverse is still
    `replay: free`, not silently relabelled."""
    path = _wal(tmp_path, [_inverse_descriptor(register="declared",
                                               undo_idempotent=True)])
    report = recover(path, world=DictWorld())
    assert [e["replay"] for e in report["transactionalRolledBack"]] == ["free"]


# ---------------------------------------------------------------------------
# (b) the re-issue seam
# ---------------------------------------------------------------------------


def test_the_seam_is_off_without_a_policy(tmp_path):
    """The default is item 245's v1 rule: recover auto-fires NOTHING, and the
    owed-emission residue is the one it always reported."""
    path = _wal(tmp_path, [
        _owed_emission(1, "Ledger", "post", ["k1"],
                       register="keyed", idempotency="k1"),
        {"record": "commit-approved", "hash": "h"},
    ])
    report = recover(path, world=DictWorld())
    assert report["reissued"] == []
    assert [e["outcome"] for e in report["owedFlushes"]] == ["not-attempted"]
    assert report["residue"]["clean"] is False
    (record,) = report["residue"]["outstanding"]
    assert "never auto-fires an owed emission" in record["hint"]
    assert "reissue-fence" not in Path(path).read_text(encoding="utf-8")


def test_a_keyed_owed_emission_is_auto_fired_under_the_knob(tmp_path):
    """Exit (2). With the operator's knob on, the keyed owed emission item 309
    classified `replay: free` is actually FIRED, through `World.reissue`."""
    path = _wal(tmp_path, [
        _owed_emission(1, "Ledger", "post", ["k1"],
                       register="keyed", idempotency="k1"),
        {"record": "commit-approved", "hash": "h"},
    ])
    world = DictWorld()
    report = recover(path, world=world, reissue="keyed")

    assert report["owedFlushes"] == []
    (fired,) = report["reissued"]
    assert fired["referent"] == "Ledger.post"
    assert fired["register"] == "keyed"
    assert fired["idempotency"] == "k1"
    assert report["residue"]["clean"] is True
    # the seam RECORDS the crossing; it never clears a referent, and a re-issued
    # emission is a crossing that happened, not residue left in the world.
    assert world.state["reissued:Ledger:k1"]["method"] == "post"
    assert world.remaining() == []
    # the proof claims the dedup CONTRACT, never a confirmed dedup.
    assert "dedup CONTRACT" in report["residue"]["proof"]


def test_an_unregistered_owed_emission_is_never_fired_under_any_knob(tmp_path):
    """The fail-closed heart of (b): no register means the pre-crash flush's
    fate cannot be decided, so no policy setting fires it."""
    records = [
        _owed_emission(1, "Mail", "send", ["m"]),
        {"record": "commit-approved", "hash": "h"},
    ]
    for index, strength in enumerate(("keyed", "declared", "strong")):
        path = _wal(tmp_path, records, name=f"unreg{index}.jsonl")
        report = recover(path, world=DictWorld(), reissue=strength)
        assert report["reissued"] == []
        assert [e["outcome"] for e in report["owedFlushes"]] == ["not-attempted"]
        assert report["residue"]["clean"] is False
        (record,) = report["residue"]["outstanding"]
        assert "carries NO idempotency register" in record["hint"]


def test_a_declared_owed_emission_needs_the_wider_knob(tmp_path):
    """A bare `idempotent` claim is the author's, unverified. The bare rule
    (`keyed`) refuses it; only an operator who names `strength: declared` accepts
    it."""
    records = [
        _owed_emission(1, "Mail", "send", ["m"], register="declared"),
        {"record": "commit-approved", "hash": "h"},
    ]
    narrow = recover(_wal(tmp_path, records, name="narrow.jsonl"),
                     world=DictWorld(), reissue="keyed")
    assert narrow["reissued"] == []
    (record,) = narrow["residue"]["outstanding"]
    assert "strength: declared" in record["hint"]

    wide = recover(_wal(tmp_path, records, name="wide.jsonl"),
                   world=DictWorld(), reissue="declared")
    assert [e["register"] for e in wide["reissued"]] == ["declared"]


def test_a_declared_reissue_is_fenced_before_the_fire(tmp_path):
    """Consume-before-fire, exactly as item 309 §3a does for an inverse: the
    fence is durable BEFORE the call, and a second run over the same WAL refuses
    with `outcome: "unknown"` rather than firing an unproven claim twice."""
    path = _wal(tmp_path, [
        _owed_emission(1, "Mail", "send", ["m"], register="declared"),
        {"record": "commit-approved", "hash": "h"},
    ])
    first = recover(path, world=DictWorld(), reissue="declared")
    assert [e["outcome"] for e in first["reissued"]] == ["reissued"]
    fences = [json.loads(line) for line in
              Path(path).read_text(encoding="utf-8").splitlines()
              if '"reissue-fence"' in line]
    assert fences == [{"record": "reissue-fence", "register": "declared", "seq": 1}]

    second = recover(path, world=DictWorld(), reissue="declared")
    assert second["reissued"] == []
    assert [e["outcome"] for e in second["owedFlushes"]] == ["unknown"]
    (record,) = second["residue"]["outstanding"]
    assert record["error"]["type"] == "fenced-before-attempt"
    assert "cannot be proven safe" in record["error"]["message"]


def test_a_keyed_reissue_is_free_across_recovery_runs(tmp_path):
    """A keyed descriptor is dedup-safe BY CONSTRUCTION, so its fence is only a
    record of what recovery did — a supervised restart loop re-fires it and the
    residue stays clean. This is the property `revl recover` needs to be safely
    re-runnable."""
    path = _wal(tmp_path, [
        _owed_emission(1, "Ledger", "post", ["k1"],
                       register="keyed", idempotency="k1"),
        {"record": "commit-approved", "hash": "h"},
    ])
    for _ in range(3):
        report = recover(path, world=DictWorld(), reissue="keyed")
        assert [e["seq"] for e in report["reissued"]] == [1]
        assert report["residue"]["clean"] is True


def test_a_redacted_owed_emission_is_refused_not_re_issued(tmp_path):
    """item 256 Slice 3, held across the new seam: a `Secret[T]` argument never
    reached the log, so re-issuing would send the placeholder to the remote. No
    fence is spent, nothing is attempted."""
    from revl.taint import REDACTED_SECRET  # noqa: PLC0415

    path = _wal(tmp_path, [
        _owed_emission(1, "Ledger", "post", [REDACTED_SECRET],
                       register="keyed", idempotency="k1"),
        {"record": "commit-approved", "hash": "h"},
    ])
    report = recover(path, world=DictWorld(), reissue="keyed")
    assert report["reissued"] == []
    (record,) = report["residue"]["outstanding"]
    assert record["kind"] == "redacted-residue"
    assert record["attemptedFlag"] is False
    assert "reissue-fence" not in Path(path).read_text(encoding="utf-8")


def test_a_raising_adapter_is_residue_never_a_silent_success(tmp_path):
    """The seam is fallible and says so."""
    class Boom(DictWorld):
        def reissue(self, op):
            raise RuntimeError("the remote refused")

    path = _wal(tmp_path, [
        _owed_emission(1, "Ledger", "post", ["k1"],
                       register="keyed", idempotency="k1"),
        {"record": "commit-approved", "hash": "h"},
    ])
    report = recover(path, world=Boom(), reissue="keyed")
    assert report["reissued"] == []
    assert [e["outcome"] for e in report["owedFlushes"]] == ["failed"]
    (record,) = report["residue"]["outstanding"]
    assert record["outcome"] == "failed"
    assert record["attemptedFlag"] is True
    assert "the remote refused" in record["error"]["message"]


# ---------------------------------------------------------------------------
# (b) the journal side: the register has to REACH the WAL descriptor
# ---------------------------------------------------------------------------


def test_a_keyed_deferred_emission_emits_its_register_and_key_value(tmp_path):
    """The seam decides from the DESCRIPTOR, so the emitter must put the register
    and the key's VALUE (not the parameter name) onto the enqueue call — the key
    a fresh-process re-issue has to repeat is the argument at this call site."""
    import emit  # noqa: PLC0415
    from revl.compiler import compile_source  # noqa: PLC0415

    ir = compile_source(
        "extern emission deferred idempotent(key: msg) fn deliver"
        "(sink: Str, msg: Str) = @py { return }\n"
        "service Ops { emission fn enqueue(sink: Str, msg: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops {\n"
        "    fn enqueue(sink, msg) { emit deliver(sink, msg) }\n"
        "  }\n"
        "}\n", "tiers440.rvl")
    assert _extern(ir, "deliver")["register"] == "keyed"

    code = emit.emit(ir)
    (line,) = [ln.strip() for ln in code.splitlines() if "enqueue_deferred" in ln]
    assert "register='keyed'" in line
    assert "idempotency=msg" in line
    compile(code, "tiers440", "exec")   # the emitted module still parses


def test_an_unregistered_deferred_emission_emits_byte_identically(tmp_path):
    """Additivity: a pre-440 deferred emission's emitted enqueue is unchanged."""
    import emit  # noqa: PLC0415
    from revl.compiler import compile_source  # noqa: PLC0415

    ir = compile_source(
        "extern emission deferred fn deliver(sink: Str, msg: Str) = @py { return }\n"
        "service Ops { emission fn enqueue(sink: Str, msg: Str) }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops {\n"
        "    fn enqueue(sink, msg) { emit deliver(sink, msg) }\n"
        "  }\n"
        "}\n", "tiers440b.rvl")
    (line,) = [ln.strip() for ln in emit.emit(ir).splitlines()
               if "enqueue_deferred" in ln]
    assert line.endswith("lambda: deliver(sink, msg))")


def test_the_wal_writer_carries_the_register(tmp_path):
    """`record_deferred_emission` journals the register and the key; absent, the
    record is byte-identical to a pre-440 one."""
    import replay  # noqa: PLC0415

    wal = replay.WriteAheadLog(str(tmp_path / "wal.jsonl"))
    wal.open()
    try:
        keyed = wal.record_deferred_emission(
            receiver="Ledger", method="post", args=["k1"],
            idempotency="k1", register="keyed")
        bare = wal.record_deferred_emission(
            receiver="Mail", method="send", args=["m"])
    finally:
        wal.close()
    assert keyed["register"] == "keyed" and keyed["idempotency"] == "k1"
    assert "register" not in bare


# ---------------------------------------------------------------------------
# (b) the item-33 policy knob
# ---------------------------------------------------------------------------


def test_the_policy_knob_parses_and_defaults_closed():
    assert parse_policy("tenants never reach each other\n").reissue_strength() is None
    assert parse_policy(
        "recovery may re-issue owed emissions\n").reissue_strength() == "keyed"
    assert parse_policy(
        "recovery may re-issue owed emissions (strength: declared)\n"
    ).reissue_strength() == "declared"


def test_the_policy_knob_refuses_a_malformed_argument():
    from revl.policy import PolicyError  # noqa: PLC0415

    with pytest.raises(PolicyError):
        parse_policy("recovery may re-issue owed emissions (declared)\n")
    with pytest.raises(PolicyError):
        parse_policy("recovery may re-issue owed emissions (strength: hopeful)\n")


def test_a_policy_free_of_the_rule_is_still_empty():
    """Additivity: the new rule tuple must not make an empty policy non-empty."""
    assert parse_policy("\n").is_empty()


# ---------------------------------------------------------------------------
# the audit view
# ---------------------------------------------------------------------------


def test_the_recovery_audit_names_the_read_tier(tmp_path):
    from revl.__main__ import _recovery_audit_view  # noqa: PLC0415

    ir = _compile(tmp_path, READ_SOURCE)
    lines = "\n".join(_recovery_audit_view(ir))
    assert "replay: read" in lines
    assert "idempotent: read" in lines
