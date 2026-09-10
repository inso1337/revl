"""Counterfactual incident replay over a durable WAL — roadmap item 467.

The exit criterion on the item is narrow and this file pins it exactly: a
recorded incident WAL replays under an ALTERNATE POLICY and against a CANDIDATE
component with ZERO LIVE EFFECTS, and the report names (a) the first dangerous
crossing and (b) whether the candidate is admitted.

What these tests hold, and why each one is not obvious:

* the "first dangerous crossing" is the EARLIEST REFUSED crossing in RECORDED
  ORDER (the WAL's own seq space), not the first record and not the last
  refusal — pinned with a permitted crossing first, two refusals around it, and
  a permitted crossing between them, so an implementation that reported the
  first record, the last refusal, or any crossing at all would fail;
* the answer is RECOMPUTED, not re-decided: the WAL holds no admit decision and
  no host response (`revl.recovery` states its gate never reads approval or
  grant records back), so every bound the report leans on is asserted as present
  text rather than assumed. A recording that cannot answer says so
  (`not-answerable`) and exits nonzero instead of guessing;
* a crossing's capability TOKEN is not on the emission record — the record names
  the component, label, key, method, service and arguments, and the declared
  scope lives only in the callee's declaration, which only a compiled audit
  graph enumerates. So `--under` with no `--candidate` is not answerable at all,
  which is pinned rather than left to a reader's inference;
* ZERO LIVE EFFECT AUTHORITY is a structural property, not a promise: the WAL is
  byte-identical across a run (checked by digest), the report says zero live
  effects fired, and a full run in a fresh interpreter leaves the cordis runtime
  tier out of `sys.modules` entirely. The last of those is the one that actually
  constrains the implementation: it can only hold because this surface imports
  no runtime tier.

Nothing here needs the cordis-py runtime, so no test is gated on it: the WAL
fixtures are JSON Lines built directly (the writer is not on the path), the
candidate is compiled statically, and the evaluator is the pure
`revl.policy.evaluate`.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl.counterfactual import (  # noqa: E402
    BOUNDS, CounterfactualError, render, replay_under)


# ---------------------------------------------------------------------------
# Fixtures: a WAL the incident made, a candidate composition, and three
# policies over the same recording.
# ---------------------------------------------------------------------------

#: The candidate. `Agent` requires two emitting services and reaches one host
#: extern directly. `sink.shout` is the crossing the incident made and the
#: strict policy refuses; `metrics.bump` is the crossing it made and the strict
#: policy permits; `audit_log` is host code reached as a FREE extern emission,
#: which leaves no WAL record at all (issue #841) — it is reachable by the audit
#: graph and therefore decides admission, yet no recorded crossing can back it.
CANDIDATE = """\
extern emission[db] fn audit_log(line: Str) -> Int = @py { return 0 }
service Sink { emission[sink_cap] fn shout(msg: Str) -> Int }
service Metrics { emission[metrics_cap] fn bump(n: Int) -> Int }
service Svc { emission fn run(msg: Str) -> Int }
component Agent requires sink: Sink, metrics: Metrics provides run: Svc {
  provide run {
    fn run(msg: Str) {
      emit metrics.bump(1)
      emit sink.shout(msg)
      let n = audit_log(msg)
      return n
    }
  }
}
"""

#: Refuses `sink_cap`, which the candidate reaches through `sink.shout`.
STRICT = "component Agent may not reach sink_cap\n"
#: An allow-list covering the candidate's whole reach, so nothing is refused.
LOOSE = "component Agent may reach sink_cap, metrics_cap, db\n"
#: Refuses only `db` — the token of the free host extern, which is precisely the
#: crossing the WAL cannot hold. Every recorded crossing passes and the
#: candidate is still refused, for a reason the recording has no crossing for.
NOTIFY = "component Agent may not reach db\n"


def _effect(seq, label, key, method, service, component="Agent"):
    return {"record": "effect", "kind": "emission", "seq": seq,
            "component": component, "stepIndex": seq, "label": label,
            "site": ["app.rvl", 10 + seq], "source": None, "origin": {},
            "inverse": {},
            "boundary": {"class": "emission",
                         "referent": "process-crossing",
                         "compensated": False,
                         "detail": {"key": key, "method": method,
                                    "service": service, "args": []}}}


def _wal_lines() -> list:
    """The incident: permitted, REFUSED, permitted, REFUSED. A fifth record
    names a crossing the candidate does not declare at all."""
    return [
        {"record": "header", "walVersion": 1, "generation": 1,
         "guarantee": "at-least-once"},
        _effect(1, "metrics.bump", "metrics", "bump", "Metrics"),
        _effect(2, "sink.shout", "sink", "shout", "Sink"),
        _effect(3, "metrics.bump", "metrics", "bump", "Metrics"),
        _effect(4, "sink.shout", "sink", "shout", "Sink"),
        _effect(5, "fs.write", "fs", "write", "Fs"),
        {"record": "activation-complete", "components": ["Agent"],
         "generation": 1},
    ]


def _write_wal(path: Path, lines=None, complete=True) -> str:
    lines = list(_wal_lines() if lines is None else lines)
    if not complete:
        lines = [l for l in lines if l.get("record") != "activation-complete"]
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n",
                    encoding="utf-8")
    return str(path)


def _fixtures(tmp_path) -> dict:
    wal = _write_wal(tmp_path / "incident.wal")
    candidate = tmp_path / "candidate.rvl"
    candidate.write_text(CANDIDATE, encoding="utf-8")
    paths = {}
    for name, text in (("strict", STRICT), ("loose", LOOSE),
                       ("notify", NOTIFY)):
        p = tmp_path / f"{name}.toml"
        p.write_text(text, encoding="utf-8")
        paths[name] = str(p)
    return {"wal": wal, "candidate": str(candidate), **paths}


def _run_cli(argv):
    from revl.__main__ import main  # noqa: PLC0415

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def _digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# The exit criterion.
# ---------------------------------------------------------------------------


def test_replay_names_the_first_dangerous_crossing_and_the_admission(tmp_path):
    """The item's exit criterion, in one run: a recorded WAL replays under an
    alternate policy and a candidate with no live effects, and the report names
    the first dangerous crossing and whether the candidate is admitted."""
    fx = _fixtures(tmp_path)
    before = _digest(fx["wal"])

    doc = replay_under(fx["wal"], fx["strict"], candidate=[fx["candidate"]])

    assert doc["kind"] == "revl.counterfactual-replay"
    assert doc["complete"] is True and doc["torn"] is False
    assert doc["recordedCrossings"] == 5
    assert doc["answerable"] is True

    # A candidate was compiled for the report and nothing was run for it.
    assert doc["candidate"] == {"files": [fx["candidate"]],
                                "components": ["Agent"], "liveEffects": 0}

    # The first dangerous crossing is the EARLIEST REFUSED one in recorded
    # order: seq 2, not seq 1 (permitted), not seq 4 (the later refusal) and not
    # seq 5 (not declared by the candidate at all).
    assert doc["firstDangerousCrossing"]["seq"] == 2
    assert doc["firstDangerousCrossing"]["label"] == "sink.shout"
    assert doc["firstDangerousCrossing"]["tokens"] == ["sink_cap"]
    assert doc["firstDangerousCrossing"]["verdict"] == "refused"
    assert doc["refusedCrossings"] == 2

    # The candidate is not admitted, and the refusal is the one the crossing
    # carried, quoted from the evaluator rather than restated.
    admission = doc["admission"]
    assert admission["admitted"] is False
    assert [v["token"] for v in admission["violations"]] == ["sink_cap"]
    assert admission["violations"][0]["kind"] == "deny"

    # Zero live effect authority, checked rather than asserted in prose.
    assert _digest(fx["wal"]) == before

    code, out, _ = _run_cli(["replay", fx["wal"], "--under", fx["strict"],
                             "--candidate", fx["candidate"]])
    assert code == 1
    assert "FIRST DANGEROUS CROSSING: Agent#2  sink.shout" in out
    assert "candidate admitted: no" in out
    assert "live effects: 0 (compiled statically, nothing run)" in out


def test_the_recorded_order_decides_which_refusal_is_first(tmp_path):
    """Recorded order is the incident's own seq space, so the same set of
    crossings in a different order names a different first dangerous one. An
    implementation that reported the first record, the last refusal, or the
    first crossing in any other sort would pass the exit-criterion test and fail
    this one."""
    fx = _fixtures(tmp_path)
    lines = _wal_lines()
    original = replay_under(fx["wal"], fx["strict"],
                            candidate=[fx["candidate"]])
    assert original["firstDangerousCrossing"]["seq"] == 2

    # The same five crossings, recorded in a different order: a refusal first,
    # then a permitted crossing, then the other refusal.
    rotated = [l for l in lines if l.get("record") == "effect"]
    rotated = [rotated[1], rotated[0], rotated[3], rotated[2], rotated[4]]
    for index, record in enumerate(rotated, start=1):
        record["seq"] = index
        record["stepIndex"] = index
    wal = _write_wal(tmp_path / "reordered.wal",
                     [lines[0]] + rotated + [lines[-1]])

    doc = replay_under(wal, fx["strict"], candidate=[fx["candidate"]])
    # The first refused crossing is now at seq 1, and the set of refusals is
    # unchanged: only the recorded order moved.
    assert doc["firstDangerousCrossing"]["seq"] == 1
    assert doc["firstDangerousCrossing"]["label"] == "sink.shout"
    assert doc["refusedCrossings"] == original["refusedCrossings"] == 2
    assert [c["label"] for c in doc["crossings"]] == [
        "sink.shout", "metrics.bump", "sink.shout", "metrics.bump",
        "fs.write"]


def test_a_permissive_policy_reports_no_dangerous_crossing_and_admits(tmp_path):
    """The counterfactual is a real recompute, not a fixed verdict: the same
    recording and the same candidate under a policy that allows the candidate's
    whole reach reports no dangerous crossing and admits."""
    fx = _fixtures(tmp_path)
    doc = replay_under(fx["wal"], fx["loose"], candidate=[fx["candidate"]])
    assert doc["firstDangerousCrossing"] is None
    assert doc["refusedCrossings"] == 0
    assert doc["admission"] == {"admitted": True, "violations": []}

    code, out, _ = _run_cli(["replay", fx["wal"], "--under", fx["loose"],
                             "--candidate", fx["candidate"]])
    assert code == 0
    assert "first dangerous crossing: none" in out
    assert "candidate admitted: yes" in out


def test_a_refusal_with_no_recorded_crossing_behind_it_says_so(tmp_path):
    """A policy can refuse the candidate for a reach no recorded crossing backs,
    which is exactly what a FREE host-extern emission produces: the audit graph
    sees `db`, the WAL holds no record of it (issue #841). The report must not
    present that refusal as a dangerous recorded crossing, and must not present
    the clean crossing list as an admission either."""
    fx = _fixtures(tmp_path)
    doc = replay_under(fx["wal"], fx["notify"], candidate=[fx["candidate"]])

    assert doc["firstDangerousCrossing"] is None
    assert doc["refusedCrossings"] == 0
    assert doc["admission"]["admitted"] is False
    assert [v["token"] for v in doc["admission"]["violations"]] == ["db"]

    code, out, _ = _run_cli(["replay", fx["wal"], "--under", fx["notify"],
                             "--candidate", fx["candidate"]])
    assert code == 1
    assert "candidate admitted: no" in out
    assert ("the refusal is not over a recorded crossing: the policy refuses "
            "the candidate for a reason the recording holds no crossing "
            "for") in out


def test_the_candidate_compiles_statically_and_the_audit_names_its_host_externs(
        tmp_path):
    """The candidate's reached host externs are listed because a free host-extern
    emission leaves no record, so those are exactly the crossings the recording
    cannot confirm. `audit_log` is reached by the candidate's body and appears in
    no recorded crossing."""
    fx = _fixtures(tmp_path)
    doc = replay_under(fx["wal"], fx["strict"], candidate=[fx["candidate"]])
    assert doc["hostExternsUnrecordable"] == [
        {"component": "Agent", "extern": "audit_log", "tokens": ["db"]}]
    labels = [c["label"] for c in doc["crossings"]]
    assert "audit_log" not in labels


# ---------------------------------------------------------------------------
# What the recording cannot answer.
# ---------------------------------------------------------------------------


def test_without_a_candidate_the_question_is_not_answerable(tmp_path):
    """A crossing's capability token is not on the emission record: the record
    holds the component, label, key, method, service and arguments, and the
    declared scope lives only in the callee's declaration. So with no candidate
    no crossing can be graded, and the honest verdict is `not-answerable` — for
    the crossings and for the admission — with a nonzero exit. The policy's
    deny-list here would have refused nothing if the token had been guessed as
    the label."""
    fx = _fixtures(tmp_path)
    doc = replay_under(fx["wal"], fx["strict"])

    assert doc["answerable"] is False
    assert doc["candidate"] is None
    assert doc["admission"] is None
    assert doc["firstDangerousCrossing"] is None
    assert doc["refusedCrossings"] == 0
    assert [c["verdict"] for c in doc["crossings"]] == [
        "not-answerable"] * 5
    assert all("not answerable from this recording" in c["reason"]
               for c in doc["crossings"])
    # A verdict is never invented from the label: no tokens were resolved.
    assert all("tokens" not in c for c in doc["crossings"])

    code, out, _ = _run_cli(["replay", fx["wal"], "--under", fx["strict"]])
    assert code == 1
    assert "not answerable from this recording" in out
    assert "candidate admitted: not answerable without a candidate" in out


def test_a_candidate_that_does_not_declare_a_crossing_is_reported_as_such(
        tmp_path):
    """A crossing the candidate does not declare was renamed or removed, so the
    candidate does not make it. That is a third verdict, not a refusal and not a
    silent drop: reporting it refused would invent a dangerous crossing, and
    dropping it would report a shorter incident than the log holds."""
    fx = _fixtures(tmp_path)
    doc = replay_under(fx["wal"], fx["strict"], candidate=[fx["candidate"]])
    crossing = [c for c in doc["crossings"] if c["seq"] == 5][0]
    assert crossing["verdict"] == "not-in-candidate"
    assert "declares no emission labelled `fs.write`" in crossing["reason"]
    assert crossing.get("refusals") in (None, [])

    empty = tmp_path / "empty.rvl"
    empty.write_text("component Other { }\n", encoding="utf-8")
    other = replay_under(fx["wal"], fx["strict"], candidate=[str(empty)])
    assert [c["verdict"] for c in other["crossings"]] == [
        "not-in-candidate"] * 5
    assert "defines no component named `Agent`" in other["crossings"][0]["reason"]
    assert other["refusedCrossings"] == 0


def test_a_recording_with_no_crossing_says_it_crossed_nothing(tmp_path):
    """An empty crossing set reads as an incident that crossed nothing, and the
    report says that a free host-extern emission would not be recorded even when
    it did, rather than reporting a clean bill of health for a gap in the
    record."""
    fx = _fixtures(tmp_path)
    lines = [{"record": "header", "walVersion": 1, "generation": 1,
              "guarantee": "at-least-once"},
             {"record": "activation-complete", "components": ["Agent"],
              "generation": 1}]
    wal = _write_wal(tmp_path / "quiet.wal", lines)
    doc = replay_under(wal, fx["strict"], candidate=[fx["candidate"]])
    assert doc["recordedCrossings"] == 0 and doc["crossings"] == []
    assert doc["firstDangerousCrossing"] is None
    # Admission is asked of the candidate, which still reaches `sink_cap`, so a
    # recording with no crossing does not make the candidate admissible.
    assert doc["admission"]["admitted"] is False
    assert [v["token"] for v in doc["admission"]["violations"]] == ["sink_cap"]
    text = render(doc)
    assert "no crossing is on the record" in text
    assert "issue #841" in text


def test_a_torn_wal_is_flagged_and_its_intact_prefix_is_still_read(tmp_path):
    """A crash mid-write leaves a torn tail. Only the intact prefix is read, and
    the bound that says so is added: a crossing in the unacknowledged tail is
    not in the report and no verdict covers it."""
    fx = _fixtures(tmp_path)
    lines = _wal_lines()
    path = tmp_path / "torn.wal"
    path.write_text("\n".join(json.dumps(l) for l in lines[:-1]) + "\n"
                    + '{"record": "activation-com', encoding="utf-8")
    doc = replay_under(str(path), fx["strict"], candidate=[fx["candidate"]])
    assert doc["torn"] is True and doc["complete"] is False
    assert doc["recordedCrossings"] == 5
    assert "tornWal" in doc["bounds"]
    assert "torn" in render(doc)


def test_the_report_carries_the_bounds_the_record_cannot_cross(tmp_path):
    """Every bound is part of the output, not a footnote: the recompute-not-
    re-decision bound, the free-host-extern bound (issue #841), the
    emissions-only bound and the reach-leg bound. A reader who only ever sees the
    rendered report has to be able to see what it cannot answer."""
    fx = _fixtures(tmp_path)
    doc = replay_under(fx["wal"], fx["strict"], candidate=[fx["candidate"]])
    assert set(doc["bounds"]) == set(BOUNDS)
    assert set(BOUNDS) == {"recordedAuthority", "freeHostExterns",
                           "emissionCrossingsOnly", "reachLegOnly",
                           "candidateAdmissionIsThePolicyLeg"}
    text = render(doc)
    for key in BOUNDS:
        assert key in text
    assert "RECOMPUTES the alternate policy" in text
    assert "issue #841" in text


def test_a_realm_scoped_policy_without_a_candidate_states_the_gap(tmp_path):
    """Realm-scoped rules select on a component's isolate map, which only a
    candidate supplies. Without one they select nothing, and the report says that
    is a gap in its own reach rather than a finding that they are satisfied."""
    fx = _fixtures(tmp_path)
    realm = tmp_path / "realm.toml"
    realm.write_text("realm billing may not reach sink_cap\n", encoding="utf-8")
    doc = replay_under(fx["wal"], str(realm))
    assert "realmScopedRules" in doc["bounds"]
    assert "rules select nothing here" in \
        doc["bounds"]["realmScopedRules"]
    with_candidate = replay_under(fx["wal"], str(realm),
                                  candidate=[fx["candidate"]])
    assert "realmScopedRules" not in with_candidate["bounds"]


# ---------------------------------------------------------------------------
# Input refusal.
# ---------------------------------------------------------------------------


def test_a_candidate_that_does_not_compile_names_the_compiler_refusal(tmp_path):
    """The candidate is COMPILED, so the compiler is the authority on whether it
    is a composition at all: a refusal is surfaced as the compiler's own
    diagnostic and as a usage error, not as a traceback and not as an empty
    report."""
    fx = _fixtures(tmp_path)
    bad = tmp_path / "bad.rvl"
    bad.write_text("component Agent requires nowhere: Missing { }\n",
                   encoding="utf-8")
    code, _, err = _run_cli(["replay", fx["wal"], "--under", fx["strict"],
                             "--candidate", str(bad)])
    assert code == 2
    assert "the candidate composition does not compile" in err

    with pytest.raises(CounterfactualError):
        replay_under(fx["wal"], fx["strict"], candidate=[str(bad)])


def test_open_holes_and_unreadable_inputs_are_refused(tmp_path):
    """A hole is a crossing no audit can enumerate, so a candidate carrying one
    is refused rather than graded. A missing WAL, a missing policy and a
    malformed policy are each the same kind of honest refusal."""
    fx = _fixtures(tmp_path)
    holed = tmp_path / "holed.rvl"
    holed.write_text(CANDIDATE + "fn gap() -> Int { return ? }\n",
                     encoding="utf-8")
    with pytest.raises(CounterfactualError):
        replay_under(fx["wal"], fx["strict"], candidate=[str(holed)])

    missing = str(tmp_path / "absent")
    for wal, policy in ((missing, fx["strict"]), (fx["wal"], missing)):
        with pytest.raises(CounterfactualError):
            replay_under(wal, policy)

    broken = tmp_path / "broken.toml"
    broken.write_text("component Agent may reach\n", encoding="utf-8")
    with pytest.raises(CounterfactualError):
        replay_under(fx["wal"], str(broken))

    code, _, err = _run_cli(["replay", missing, "--under", fx["strict"]])
    assert code == 2
    assert "cannot read WAL" in err

    code, _, err = _run_cli(["replay", fx["wal"], "--under", missing])
    assert code == 2
    assert "cannot read policy" in err


# ---------------------------------------------------------------------------
# Zero live effect authority, structurally.
# ---------------------------------------------------------------------------

_PROBE = """
import hashlib, json, sys
from revl.counterfactual import replay_under
wal, policy, candidate = sys.argv[1], sys.argv[2], sys.argv[3]
before = hashlib.sha256(open(wal, "rb").read()).hexdigest()
doc = replay_under(wal, policy, candidate=[candidate])
after = hashlib.sha256(open(wal, "rb").read()).hexdigest()
tiers = sorted(m for m in sys.modules
               if m.split(".")[0] in ("runtime", "cordis"))
json.dump({"first": doc["firstDangerousCrossing"]["seq"],
           "admitted": doc["admission"]["admitted"],
           "liveEffects": doc["candidate"]["liveEffects"],
           "walUnchanged": before == after, "tiers": tiers}, sys.stdout)
"""


def test_a_full_run_pulls_no_runtime_tier_and_writes_nothing(tmp_path):
    """The structural half of "zero live effect authority". A fresh interpreter
    runs a complete counterfactual: the WAL is byte-identical afterwards, the
    report says no live effect fired, and the cordis runtime tier never enters
    `sys.modules`. That last assertion is the one with teeth — it can only hold
    while this surface imports no runtime tier, so a future edit that reached for
    a live component to "replay properly" would redden here rather than quietly
    acquire effect authority."""
    fx = _fixtures(tmp_path)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get(
        "PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, fx["wal"], fx["strict"],
         fx["candidate"]],
        capture_output=True, text=True, env=env, timeout=300)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["first"] == 2
    assert result["admitted"] is False
    assert result["liveEffects"] == 0
    assert result["walUnchanged"] is True
    assert result["tiers"] == []
