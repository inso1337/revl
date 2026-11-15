"""The evolve feedback loop over the Gate service (roadmap item 148, Fuller
Path A).

Item 144 shipped one-shot admission: a candidate is admitted into a running
world by passing revl's guarantees, and a refusal carries a why-trace naming
the violated G-rule. This suite pins **evolve** — the loop that turns a refusal
into a next attempt: admit, on refusal hand the structured why-trace to a
`propose` extern seam, retry to a budget, terminating on an admit or on
budget exhaustion with the full attempt history.

The `propose` seam is an extern the harness fills with its real generator; a
trivial deterministic in-repo proposer (`evolve_loop.scripted_proposer`) stands
in so the loop is proven end to end without an LLM.

Levels:
  1. the Gate's structured refusal (`admit_structured`) carries the machine-
     readable payload — the G-rule key, the offending subject, the call path,
     the mapped fix;
  2. the payload contract (`rejection_payload`) normalizes it for a generator;
  3. the loop repairs a G2-refused candidate on attempt 2 (the proof exit);
  4. the loop gives up at budget with the full history + final why-trace;
  5. the seam plumbing (register_proposer, budget guards);
  6. the bounded `ProposerPacket` and its digest on every proposal call;
  7. `rounds`: fresh-context restarts from the retained component, with the
     original brief and no refusal text carried across the round boundary.
All pure frontend — the in-memory compile is the only runtime touched.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.compiler import compile_files  # noqa: E402
from revl.gate_service import admit_structured  # noqa: E402
from revl import evolve_loop  # noqa: E402
from revl.evolve_loop import (  # noqa: E402
    MAX_NOTE_CHARS,
    MAX_PACKET_NOTES,
    Candidate,
    ProposerPacket,
    candidate_digest,
    evolve,
    g2_key_bump_proposer,
    rejection_payload,
    register_proposer,
    rounds,
    scripted_proposer,
)


# --------------------------------------------------------------------------
# fixtures: a running world (one provider of key `thing`) and the two
# candidates from item 144 — `dup` collides on `thing` (G2), `extra` is clean.
# --------------------------------------------------------------------------


def _running_manifest():
    sources = {
        "/evolve/base.rvl":
            "service Thing { fn ping() -> Int }\n"
            "component Base provides thing: Thing { provide thing { fn ping() = 1 } }\n",
    }
    ir = compile_files(list(sources), sources=sources)
    return json.dumps({"manifest": ir.get("manifest") or {},
                       "services": ir.get("services") or {}})


def _collide_candidate():
    return Candidate(
        sources={"/evolve/dup.rvl":
                 "component Dup provides thing: Thing { provide thing { fn ping() = 2 } }\n"},
        manifest=_running_manifest())


# --------------------------------------------------------------------------
# 1. the Gate's structured refusal carries the machine-readable payload
# --------------------------------------------------------------------------


def test_admit_structured_admits_a_clean_candidate_with_null_rejection():
    running = _running_manifest()
    clean = json.dumps({"/evolve/extra.rvl":
                        "component Extra provides other: Thing { provide other { fn ping() = 3 } }\n"})
    verdict = json.loads(admit_structured(clean, running))
    assert verdict["ok"] is True
    assert verdict["admitted"] == ["Extra"]
    assert verdict["rejection"] is None


def test_admit_structured_refusal_carries_g_rule_subject_callpath_and_fix():
    """The proving property of the payload: a G2 collision comes back with the
    machine key `G2`, the colliding key `thing`, the two-provider call path,
    and the mapped one-line fix — all without parsing the human diagnostic."""
    cand = _collide_candidate()
    verdict = json.loads(admit_structured(cand.sources_json(), cand.manifest))
    assert verdict["ok"] is False
    rej = verdict["rejection"]
    assert rej["code"] == "G2"
    assert rej["why"]["subject"] == "thing"
    names = [s["name"] for s in rej["why"]["steps"]]
    assert names == ["Base", "Dup"]                 # the two providers
    assert "one provider per key" in rej["fix"]     # the mapped rewrite
    # the human diagnostic is still present and unchanged in spirit
    assert "(G2)" in verdict["diagnostic"]


# --------------------------------------------------------------------------
# 2. the payload contract: the generator-facing normalized view
# --------------------------------------------------------------------------


def test_rejection_payload_normalizes_the_gate_record():
    cand = _collide_candidate()
    verdict = json.loads(admit_structured(cand.sources_json(), cand.manifest))
    payload = rejection_payload(verdict["rejection"])
    assert payload["g_rule"] == "G2"
    assert payload["subject"] == "thing"
    assert payload["component"] == "Dup"            # last named step
    assert [s["name"] for s in payload["call_path"]] == ["Base", "Dup"]
    assert payload["fix"] and "provider per key" in payload["fix"]
    assert payload["guarantee"]


def test_rejection_payload_is_total_on_an_empty_record():
    payload = rejection_payload({})
    assert payload["g_rule"] == "REVL"
    assert payload["call_path"] == []
    assert payload["subject"] is None


# --------------------------------------------------------------------------
# 3. the proof exit: refused on attempt 1, repaired by the scripted proposer,
#    admitted on attempt 2.
# --------------------------------------------------------------------------


def test_evolve_repairs_a_g2_refusal_on_attempt_two():
    result = evolve(_collide_candidate(), proposer=g2_key_bump_proposer(), budget=3)

    assert result.admitted is True
    assert len(result.attempts) == 2               # stopped as soon as admitted
    assert result.final_rejection is None

    first, second = result.attempts
    # attempt 1 was refused, and the why-trace it fed to propose was G2/thing
    assert first.admitted is False
    assert first.rejection["g_rule"] == "G2"
    assert first.rejection["subject"] == "thing"
    assert [s["name"] for s in first.rejection["call_path"]] == ["Base", "Dup"]
    assert "provides thing:" in next(iter(first.candidate.sources.values()))

    # attempt 2 is the *revised* candidate (the proposer bumped the key) and it
    # admitted
    assert second.admitted is True
    revised = next(iter(second.candidate.sources.values()))
    assert "provides thing_v2:" in revised
    assert result.final_candidate is second.candidate


def test_evolve_history_is_ordered_and_records_every_candidate_offered():
    result = evolve(_collide_candidate(), proposer=g2_key_bump_proposer(), budget=3)
    assert [a.n for a in result.attempts] == [1, 2]
    # the serialized trace a harness would log: each attempt with its verdict
    doc = result.to_json()
    assert doc["admitted"] is True
    assert len(doc["attempts"]) == 2
    assert doc["attempts"][0]["rejection"]["g_rule"] == "G2"
    assert doc["attempts"][1]["admitted"] is True


# --------------------------------------------------------------------------
# 4. budget exhaustion: a proposer that never fixes → give up with the full
#    history and the final why-trace.
# --------------------------------------------------------------------------


def test_evolve_exhausts_the_budget_when_the_proposer_never_repairs():
    noop = scripted_proposer({})                    # matches no g_rule → no-op
    result = evolve(_collide_candidate(), proposer=noop, budget=3)

    assert result.admitted is False
    assert len(result.attempts) == 3               # spent the whole budget
    assert all(a.admitted is False for a in result.attempts)
    # the give-up carries the final why-trace, still G2 with the call path
    assert result.final_rejection["g_rule"] == "G2"
    assert result.final_rejection["subject"] == "thing"
    assert [s["name"] for s in result.final_rejection["call_path"]] == ["Base", "Dup"]


def test_evolve_budget_one_is_a_single_admit_no_propose():
    calls = []

    def spy(packet):
        calls.append(packet)
        return packet.candidate

    result = evolve(_collide_candidate(), proposer=spy, budget=1)
    assert result.admitted is False
    assert len(result.attempts) == 1
    assert calls == []                              # propose never spent


def test_evolve_clean_candidate_admits_on_attempt_one():
    clean = Candidate(
        sources={"/evolve/extra.rvl":
                 "component Extra provides other: Thing { provide other { fn ping() = 3 } }\n"},
        manifest=_running_manifest())
    calls = []
    result = evolve(clean, proposer=lambda p: calls.append(p) or p.candidate,
                    budget=3)
    assert result.admitted is True
    assert len(result.attempts) == 1
    assert calls == []                              # no refusal, no propose


# --------------------------------------------------------------------------
# 5. the seam: register_proposer default, and the budget guard.
# --------------------------------------------------------------------------


def test_evolve_uses_the_harness_registered_proposer_by_default(monkeypatch):
    monkeypatch.setattr(evolve_loop, "_HARNESS_PROPOSER", None)
    register_proposer(g2_key_bump_proposer())
    try:
        result = evolve(_collide_candidate(), budget=3)   # no explicit proposer
        assert result.admitted is True
    finally:
        evolve_loop._HARNESS_PROPOSER = None


def test_evolve_without_a_proposer_seam_is_a_clear_error(monkeypatch):
    monkeypatch.setattr(evolve_loop, "_HARNESS_PROPOSER", None)
    with pytest.raises(ValueError, match="no propose seam"):
        evolve(_collide_candidate(), budget=3)


def test_evolve_rejects_a_zero_budget():
    with pytest.raises(ValueError, match="budget must be >= 1"):
        evolve(_collide_candidate(), proposer=g2_key_bump_proposer(), budget=0)


# --------------------------------------------------------------------------
# 6. the proposer packet and its digest: the second call sees the first
#    candidate and refusal exactly, and the attempt records what it saw.
# --------------------------------------------------------------------------


def test_packet_digest_matches_the_packet_the_proposer_saw():
    seen = []
    inner = g2_key_bump_proposer("_v2")

    def recorder(packet):
        seen.append(packet)
        return inner(packet)

    result = evolve(_collide_candidate(), proposer=recorder, budget=3)

    assert result.admitted is True
    assert len(seen) == 1                      # only the second attempt asks
    packet = seen[0]
    first = result.attempts[0]

    # the one proposal call received exactly the parent candidate and the
    # refusal payload, with no brief and no earlier notes
    assert packet.candidate == first.candidate
    assert packet.last_refusal == first.rejection
    assert packet.brief == ""
    assert packet.notes == ()

    # the refused attempt records the digest of that packet, byte for byte
    assert first.packet_digest == packet.digest()
    rebuilt = ProposerPacket(brief="", candidate=first.candidate,
                             last_refusal=first.rejection)
    assert first.packet_digest == rebuilt.digest()

    # nothing was handed over for the admitted attempt
    assert result.attempts[1].packet_digest is None


def test_packet_digest_is_recorded_for_every_propose_and_absent_when_unspent():
    result = evolve(_collide_candidate(), proposer=scripted_proposer({}),
                    budget=3)
    assert result.admitted is False
    # two proposes for three attempts: the final refusal is not handed over
    assert [a.packet_digest is None for a in result.attempts] == [False, False, True]
    assert len({a.packet_digest for a in result.attempts if a.packet_digest}) == 2


def test_packet_notes_carry_earlier_refusals_only_and_are_capped():
    seen = []

    def recorder(packet):
        seen.append(packet)
        return packet.candidate

    evolve(_collide_candidate(), proposer=recorder, budget=MAX_PACKET_NOTES + 3)

    # attempt n's packet sees notes for attempts 1..n-1, capped at the tail
    assert [len(p.notes) for p in seen[:3]] == [0, 1, 2]
    assert len(seen[-1].notes) == MAX_PACKET_NOTES
    assert all(len(note) <= MAX_NOTE_CHARS for note in seen[-1].notes)
    # the notes name attempts 2..9 only: attempt 1 fell off the cap and the
    # current refusal (attempt 10) is in last_refusal, not in the notes
    assert [note.split(":")[0] for note in seen[-1].notes] == [
        f"attempt {i}" for i in range(2, MAX_PACKET_NOTES + 2)]


# --------------------------------------------------------------------------
# 7. rounds: a bounded run ends at a retained component, and the next round
#    starts from a fresh packet built from it and the original brief.
# --------------------------------------------------------------------------


def test_two_rounds_have_different_packets_and_round_two_is_fresh():
    brief = "keep the greeter working"
    result = rounds(_collide_candidate(), brief,
                    proposer=g2_key_bump_proposer(), budget=3, max_rounds=2)

    assert result.admitted is True
    assert len(result.rounds) == 2
    first, second = result.rounds
    assert first.packet_digest != second.packet_digest

    # the log names each round's seed packet and digest
    doc = result.to_json()
    assert doc["admitted"] is True
    assert doc["rounds"][1]["packet_digest"] == second.packet_digest
    assert doc["rounds"][1]["packet"]["brief"] == brief

    # the round-two packet holds no refusal text from round one
    assert second.packet.last_refusal is None
    assert second.packet.notes == ()
    round_one_refusal = first.result.attempts[0].rejection["message"]
    assert round_one_refusal not in second.packet.canonical_json()


def test_round_two_packet_holds_the_kept_component_and_the_original_brief():
    brief = "extend the greeter safely"
    result = rounds(_collide_candidate(), brief,
                    proposer=g2_key_bump_proposer(), budget=3, max_rounds=2)

    kept = result.rounds[0].result.final_candidate
    packet = result.rounds[1].packet

    assert packet.candidate == kept
    assert packet.candidate_digest == candidate_digest(kept)
    assert packet.brief == brief
    # the kept component is the repaired one, not the round-one input
    assert "provides thing_v2:" in next(iter(kept.sources.values()))
    assert result.final_candidate == kept


def test_rounds_stop_at_a_failed_round_and_keep_the_last_retained_component():
    noop = scripted_proposer({})
    result = rounds(_collide_candidate(), "keep trying", proposer=noop,
                    budget=2, max_rounds=2)

    assert result.admitted is False
    assert len(result.rounds) == 1             # the first failed round ends it
    assert result.final_rejection["g_rule"] == "G2"
    # no round retained anything, so there is no final candidate
    assert result.final_candidate is None


def test_a_failed_second_round_ends_the_run_without_losing_the_retained_component():
    inner = g2_key_bump_proposer("_v2")
    calls = []
    bad = Candidate(
        sources={"/evolve/dup.rvl":
                 "component Dup provides thing: Thing { provide thing { fn ping() = 2 } }\n"},
        manifest=_collide_candidate().manifest)

    def once(packet):
        calls.append(packet)
        if len(calls) == 1:                    # round-one seed: keep the candidate
            return packet.candidate
        if len(calls) == 2:                    # round-one repair
            return inner(packet)
        return bad                             # round two never converges

    result = rounds(_collide_candidate(), "keep going", proposer=once,
                    budget=2, max_rounds=2)

    assert len(result.rounds) == 2
    assert result.rounds[0].result.admitted is True
    assert result.rounds[1].result.admitted is False
    assert result.admitted is False
    assert result.final_rejection["g_rule"] == "G2"
    # the last retained component is still named, and round two started from it
    retained = result.rounds[0].result.final_candidate
    assert result.final_candidate == retained
    assert result.rounds[1].packet.candidate == retained


def test_rounds_are_bounded_and_validate_their_arguments():
    with pytest.raises(ValueError, match="budget must be >= 1"):
        rounds(_collide_candidate(), "", proposer=g2_key_bump_proposer(),
               budget=0)
    with pytest.raises(ValueError, match="rounds must be >= 1"):
        rounds(_collide_candidate(), "", proposer=g2_key_bump_proposer(),
               max_rounds=0)
    result = rounds(_collide_candidate(), "", proposer=g2_key_bump_proposer(),
                    budget=3, max_rounds=1)
    assert len(result.rounds) == 1             # bounded even when admitted
