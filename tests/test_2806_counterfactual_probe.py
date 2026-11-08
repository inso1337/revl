"""The counterfactual probe for a candidate edit, and probe ordering in evolve
(roadmap item 2806, Slices 1 and 2).

Slice 1 is the probe summary over the existing counterfactual run: one row per
candidate with the candidate hash, the number of recorded crossings regraded,
how many are newly refused or newly allowed, the first dangerous crossing and
the bounds. It runs no new execution, so the WAL must be byte-identical before
and after, exactly as `tests/test_467_counterfactual_replay.py` pins for the
counterfactual it summarizes.

The tests that matter here:

* a candidate that WIDENS a recorded crossing shows a newly refused count above
  zero. The recording crossed `sink.shout` while its callee declared
  `sink_cap`; the widened candidate declares `db` on the same label, the
  alternate policy's allow-list omits `db`, so the crossing is refused. A
  candidate that does not widen reads zero, so the count is a real contrast
  between two candidates over one recording, not a fixed verdict;
* the probe grants nothing. The row carries the admission, but the probe exits
  zero whatever that admission says, and the evolve hook only ORDERS: a
  candidate with a clean probe row and a refusing gate is still refused;
* one row per candidate, keyed by the candidate's content hash, so competing
  edits are comparable without trusting the path they were checked out at.
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl.counterfactual import (  # noqa: E402
    PROBE_ROW_FIELDS, candidate_hash, probe, probe_row)
from revl.evolve_loop import (  # noqa: E402
    Candidate, candidate_digest, evolve_ordered, probe_order,
    probe_row_is_adverse)

# ---------------------------------------------------------------------------
# Fixtures: one recording, two candidates, one alternate policy.
# ---------------------------------------------------------------------------

#: The narrow candidate: the crossing `sink.shout` declares only `sink_cap`.
NARROW = """\
service Sink { emission[sink_cap] fn shout(msg: Str) -> Int }
service Metrics { emission[metrics_cap] fn bump(n: Int) -> Int }
service Svc { emission fn run(msg: Str) -> Int }
component Agent requires sink: Sink, metrics: Metrics provides run: Svc {
  provide run {
    fn run(msg: Str) {
      let n = emit metrics.bump(1)
      emit sink.shout(msg)
      return n
    }
  }
}
"""

#: The SAME candidate with the recorded crossing widened: `shout` now declares
#: `sink_cap` and `db`, so the crossing reaches a token the allow-list omits.
WIDE = NARROW.replace(
    "service Sink { emission[sink_cap] fn shout(msg: Str) -> Int }",
    "service Sink { emission[sink_cap, db] fn shout(msg: Str) -> Int }")

#: Allows the narrow candidate's whole reach and omits `db`, which only the
#: widened candidate reaches. The narrow candidate is admitted and no recorded
#: crossing is refused.
ALLOW_NARROW = "component Agent may reach sink_cap, metrics_cap\n"


def _effect(seq, label, key, method, service):
    return {"record": "effect", "kind": "emission", "seq": seq,
            "component": "Agent", "stepIndex": seq, "label": label,
            "site": ["app.rvl", 10 + seq], "source": None, "origin": {},
            "inverse": {},
            "boundary": {"class": "emission",
                         "referent": "process-crossing",
                         "compensated": False,
                         "detail": {"key": key, "method": method,
                                    "service": service, "args": []}}}


def _write_wal(path: Path) -> str:
    lines = [
        {"record": "header", "walVersion": 1, "generation": 1,
         "guarantee": "at-least-once"},
        _effect(1, "metrics.bump", "metrics", "bump", "Metrics"),
        _effect(2, "sink.shout", "sink", "shout", "Sink"),
        {"record": "activation-complete", "components": ["Agent"],
         "generation": 1},
    ]
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n",
                    encoding="utf-8")
    return str(path)


def _fixtures(tmp_path) -> dict:
    wal = _write_wal(tmp_path / "incident.wal")
    paths = {}
    for name, text in (("narrow", NARROW), ("wide", WIDE)):
        p = tmp_path / f"{name}.rvl"
        p.write_text(text, encoding="utf-8")
        paths[name] = str(p)
    policy = tmp_path / "allow-narrow.toml"
    policy.write_text(ALLOW_NARROW, encoding="utf-8")
    paths["policy"] = str(policy)
    paths["wal"] = wal
    return paths


def _run_cli(argv):
    from revl.__main__ import main  # noqa: PLC0415

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


def _digest(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Slice 1: the probe row and the report.
# ---------------------------------------------------------------------------


def test_the_probe_row_has_every_field_the_loop_reads(tmp_path):
    fx = _fixtures(tmp_path)
    row = probe_row(fx["wal"], fx["policy"], [fx["narrow"]])

    assert tuple(row) == PROBE_ROW_FIELDS
    assert row["candidate"] == candidate_hash([fx["narrow"]])
    assert row["files"] == [fx["narrow"]]
    assert row["recordedCrossings"] == 2
    assert row["regraded"] == 2
    assert row["answerable"] is True
    # The narrow candidate widens nothing: both recorded crossings are
    # permitted and nothing is refused.
    assert row["newlyRefused"] == 0
    assert row["newlyAllowed"] == 2
    assert row["firstDangerousCrossing"] is None
    assert row["admission"] == {"admitted": True, "violations": []}
    assert set(row["bounds"]) >= {"recordedAuthority", "freeHostExterns"}


def test_a_candidate_that_widens_a_crossing_is_newly_refused(tmp_path):
    """The widened crossing (same recorded label, a token the allow-list omits)
    must read newly refused, while the unwidened candidate over the SAME
    recording reads zero. That contrast is the probe's whole point."""
    fx = _fixtures(tmp_path)
    narrow = probe_row(fx["wal"], fx["policy"], [fx["narrow"]])
    wide = probe_row(fx["wal"], fx["policy"], [fx["wide"]])

    assert narrow["newlyRefused"] == 0
    assert wide["newlyRefused"] == 1
    assert wide["firstDangerousCrossing"]["seq"] == 2
    assert wide["firstDangerousCrossing"]["label"] == "sink.shout"
    assert set(wide["firstDangerousCrossing"]["tokens"]) == {"sink_cap", "db"}
    assert wide["admission"]["admitted"] is False
    assert [v["token"] for v in wide["admission"]["violations"]] == ["db"]
    # The two compositions are named by what they are, so the rows differ.
    assert narrow["candidate"] != wide["candidate"]


def test_the_probe_report_is_one_row_per_candidate(tmp_path):
    fx = _fixtures(tmp_path)
    doc = probe(fx["wal"], fx["policy"], [[fx["narrow"]], [fx["wide"]]])

    assert doc["kind"] == "revl.counterfactual-probe"
    assert doc["schemaVersion"] == "1.0"
    assert doc["recordedCrossings"] == 2
    assert [row["files"] for row in doc["rows"]] == [
        [fx["narrow"]], [fx["wide"]]]
    assert [row["newlyRefused"] for row in doc["rows"]] == [0, 1]
    assert {row["candidate"] for row in doc["rows"]} == {
        candidate_hash([fx["narrow"]]), candidate_hash([fx["wide"]])}


def test_the_probe_runs_with_the_wal_byte_identical(tmp_path):
    """Slice 1 runs no new execution: the only side effect is the report."""
    fx = _fixtures(tmp_path)
    before = _digest(fx["wal"])
    probe(fx["wal"], fx["policy"], [[fx["narrow"]], [fx["wide"]]])
    assert _digest(fx["wal"]) == before


def test_the_probe_cli_prints_the_rows_and_never_gates_on_them(tmp_path):
    fx = _fixtures(tmp_path)
    code, out, _ = _run_cli([
        "replay", fx["wal"], "--under", fx["policy"],
        "--candidate", fx["narrow"], "--candidate", fx["wide"],
        "--probe", "--json"])
    # The widened candidate's admission is refused, and the probe still exits
    # zero: it is ordering evidence, not an admission.
    assert code == 0
    doc = json.loads(out)
    assert doc["kind"] == "revl.counterfactual-probe"
    assert [row["newlyRefused"] for row in doc["rows"]] == [0, 1]

    code, out, _ = _run_cli([
        "replay", fx["wal"], "--under", fx["policy"],
        "--candidate", fx["narrow"], "--probe"])
    assert code == 0
    assert "counterfactual probe" in out
    assert "newly refused: 0" in out
    assert "bounds:" in out


def test_the_probe_refuses_a_malformed_question(tmp_path):
    fx = _fixtures(tmp_path)
    code, _, err = _run_cli(["replay", fx["wal"], "--under", fx["policy"],
                             "--probe"])
    assert code == 2
    assert "at least one --candidate" in err

    # The counterfactual itself still takes exactly one composition.
    code, _, err = _run_cli([
        "replay", fx["wal"], "--under", fx["policy"],
        "--candidate", fx["narrow"], "--candidate", fx["wide"]])
    assert code == 2
    assert "one --candidate composition" in err

    # --probe is the summary of the --under counterfactual, not a mode of the
    # readiness planner; asking for it without --under is a usage error, not a
    # silently ignored flag.
    code, _, err = _run_cli(["replay", fx["wal"], "--probe"])
    assert code == 2
    assert "--probe is the summary of the --under counterfactual" in err


def test_one_candidate_can_still_be_several_files(tmp_path):
    """`--candidate a.rvl b.rvl` was one composition with several files before
    `--candidate` became repeatable; it must stay one composition. The CLI
    collects a list of compositions, so one occurrence with two paths is a
    single row."""
    fx = _fixtures(tmp_path)
    head, body = NARROW.split("component Agent", 1)
    services = tmp_path / "services.rvl"
    services.write_text(head, encoding="utf-8")
    component = tmp_path / "component.rvl"
    component.write_text("component Agent" + body, encoding="utf-8")

    doc = probe(fx["wal"], fx["policy"], [[str(services), str(component)]])
    assert len(doc["rows"]) == 1
    assert doc["rows"][0]["files"] == [str(services), str(component)]
    assert doc["rows"][0]["newlyRefused"] == 0

    code, out, _ = _run_cli([
        "replay", fx["wal"], "--under", fx["policy"],
        "--candidate", str(services), str(component), "--probe", "--json"])
    assert code == 0
    rows = json.loads(out)["rows"]
    assert len(rows) == 1
    assert rows[0]["files"] == [str(services), str(component)]


# ---------------------------------------------------------------------------
# Slice 2: the evolve hook orders candidates and cannot admit one.
# ---------------------------------------------------------------------------


def _candidate(name: str) -> Candidate:
    return Candidate(sources={f"/evolve/{name}.rvl":
                              f"component {name.capitalize()} {{ }}\n"})


def _row(*, refused: int = 0, dangerous: bool = False) -> dict:
    return {"newlyRefused": refused,
            "firstDangerousCrossing": ({"seq": 2} if dangerous else None)}


def test_probe_order_defers_an_adverse_row_and_keeps_the_rest():
    clean, wide = _candidate("clean"), _candidate("wide")
    rows = {candidate_digest(clean): _row(),
            candidate_digest(wide): _row(refused=1, dangerous=True)}
    calls = []

    def probe(candidate):
        calls.append(candidate)
        return rows[candidate_digest(candidate)]

    order = probe_order([wide, clean], probe)

    assert order["ordered"] == [clean]
    assert order["deferred"] == [wide]
    assert order["rows"] == rows
    assert calls == [wide, clean]                    # one call per candidate
    assert probe_row_is_adverse(None) is False       # silence is not adverse
    assert probe_row_is_adverse({"newlyRefused": 0,
                                 "firstDangerousCrossing": None}) is False


def test_the_loop_hook_cannot_admit_a_clean_probe_through_a_refusing_gate():
    """A clean probe row only ORDERS the candidate. The Gate is still the only
    admission, so a refusing Gate refuses it."""
    clean = _candidate("clean")
    admits = []

    def refusing_gate(sources_json, manifest):
        admits.append(sources_json)
        return json.dumps({"ok": False, "diagnostic": "refusing gate",
                           "rejection": {"code": "G4",
                                         "guarantee": "capability containment",
                                         "why": {}}})

    result = evolve_ordered(
        [clean], lambda candidate: _row(), budget=1, admit=refusing_gate,
        proposer=lambda packet: packet.candidate)

    assert result.admitted is False
    assert len(admits) == 1
    assert result.attempts[0].admitted is False


def test_a_clean_probe_and_an_accepting_gate_are_admitted():
    """The positive control: a clean row does not block a candidate the Gate
    admits, so an evolve_ordered that always gives up cannot pass."""
    clean = _candidate("clean")

    def accepting_gate(sources_json, manifest):
        return json.dumps({"ok": True})

    result = evolve_ordered(
        [clean], lambda candidate: _row(), budget=1, admit=accepting_gate,
        proposer=lambda packet: packet.candidate)

    assert result.admitted is True
    assert result.final_candidate == clean


def test_a_deferred_candidate_is_never_offered_to_the_gate():
    wide = _candidate("wide")
    admits = []

    def gate(sources_json, manifest):
        admits.append(sources_json)
        return json.dumps({"ok": True})

    result = evolve_ordered(
        [wide], lambda candidate: _row(refused=1, dangerous=True), budget=1,
        admit=gate, proposer=lambda packet: packet.candidate)

    assert result.admitted is False
    assert admits == []                              # deferred, not admitted
