"""The cross-tier fault sweep (roadmap item 125): the same fault, on every
runtime, residue-free, and the runtimes agree.

`revl test --sweep` proves A8/R4 exhaustively on the py reference tier;
`revl test --backend all --sweep` upgrades the claim to a portability one — the
same injected fault at the same step leaves no residue on *every* runtime whose
toolchain is present, and the runtimes AGREE.

Two layers, mirroring tests/test_fault_tests.py:

* **pure** — the corpus enumeration, the dependent-pruning, the per-tier
  verdict classification, and the agreement aggregation are all pure over
  fabricated inputs, so a disagreement, a leak, and a loud skip are tested
  without a runtime.
* **executed** — the real cross-tier sweep over a witnessed/compensating
  composition, gated behind the toolchains it needs (cordis-py for the py
  reference, go for the second executing tier). The heavy tiers loud-skip when
  their runtime is absent — asserted to be a skip, never a pass.
"""

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import fault as fault_mod  # noqa: E402
from revl.run_rust import rust_runtime_reason  # noqa: E402

EXAMPLES = ROOT / "examples"

# A provider + a consumer with a compensating (two-phase) teardown: faulting
# after the emission runs `compensate`, then the accumulated inverses (LIFO).
TWO_PHASE = '''
service Database {
  fn query(sql: Str) -> List[Row]
  emission fn execute(sql: Str) -> Int
}
component PgDatabase provides db: Database {
  config { url: Str = "postgres://localhost/app" }
  let pool = effect Pool.open(config.url, 4) undo pool.close()
  provide db {
    fn query(sql)   = pool.query(sql)
    fn execute(sql) = pool.execute(sql)
  }
}
component Migrator requires db: Database {
  let lock = effect db.query("SELECT pg_advisory_lock(42)")
             undo   db.query("SELECT pg_advisory_unlock(42)")
  emit db.execute("INSERT INTO migration_log VALUES (42)")
       compensate db.execute("DELETE FROM migration_log WHERE id = 42")
}
'''


def _two_phase() -> dict:
    return compile_source(TWO_PHASE, "two_phase.rvl")


# --------------------------------------------------------------- corpus + prune


def test_corpus_units_enumerates_every_top_level_step():
    ir = _two_phase()
    corpus = fault_mod._corpus_units(ir)
    # PgDatabase: pool, provision; Migrator: lock, emit  -> four points
    assert [(u["component"], u["step"]) for u in corpus] == [
        ("PgDatabase", 1), ("PgDatabase", 2),
        ("Migrator", 1), ("Migrator", 2)]


def test_corpus_cap_takes_a_representative_first_middle_last_subset():
    ir = _two_phase()
    # a component with five steps, capped to 3, keeps first/middle/last
    ir["components"][0]["body"] = ir["components"][0]["body"] + [
        {"step": "let-effect", "bind": f"x{i}",
         "acquire": {"kind": "host", "fn": "Map.new", "args": []},
         "undo": {"kind": "lit", "value": None}} for i in range(3)]
    capped = fault_mod._corpus_units(ir, cap=3)
    pg = [u["step"] for u in capped if u["component"] == "PgDatabase"]
    assert len(pg) == 3 and pg[0] == 1 and pg[-1] == len(
        ir["components"][0]["body"])


def test_prune_dependents_holds_out_the_targets_consumers():
    ir = _two_phase()
    # Migrator requires what PgDatabase provides, so faulting PgDatabase must
    # drop Migrator from the boot (else it strands waiting on a provider that
    # never comes — a deadlock on the go runner, a false "residue").
    pruned = fault_mod._prune_dependents(ir, "PgDatabase")
    assert [c["name"] for c in pruned["components"]] == ["PgDatabase"]
    # faulting the leaf consumer prunes nothing
    kept = fault_mod._prune_dependents(ir, "Migrator")
    assert {c["name"] for c in kept["components"]} == {"PgDatabase", "Migrator"}


# --------------------------------------------------------------- verdict split


class _FakeRunner:
    """A stand-in `--once` runner: returns a fixed (code, printed output) and
    reports the proof lines it VERIFIED (token-tagged, issue #1621), which is
    all the verdict may read. Printed text is only output."""

    def __init__(self, code: int, output: str, verified=()):
        self.code, self.output = code, output
        self.verified = {"up": "UP" in verified, "down": "DOWN" in verified,
                         "noResidue": "NO-RESIDUE" in verified,
                         "residueLeft": "RESIDUE-LEFT" in verified}

    def __call__(self, ir, config, files, once=False, interactive=False,
                 proof_out=None):
        print(self.output)
        if proof_out is not None:
            proof_out.update(self.verified)
        return self.code


def test_once_verdict_reads_a_no_residue_proof_as_clean():
    kind, _ = fault_mod._once_verdict(
        _FakeRunner(0, "[run] UP\n[run] NO-RESIDUE\n[run] DOWN",
                    verified=("UP", "NO-RESIDUE", "DOWN")), {}, {}, [])
    assert kind == "clean"


def test_once_verdict_reads_residue_left_as_a_real_leak():
    kind, detail = fault_mod._once_verdict(
        _FakeRunner(1, "[run] RESIDUE-LEFT — a live pool",
                    verified=("UP", "RESIDUE-LEFT", "DOWN")), {}, {}, [])
    assert kind == "residue" and "RESIDUE-LEFT" in detail


def test_once_verdict_does_not_read_a_proof_the_program_printed():
    """Issue #1621: a program can print `[run] NO-RESIDUE` itself and exit 0
    before teardown. Only a proof the runner verified against its token
    counts, so printed text alone is a gap, never clean."""
    kind, _ = fault_mod._once_verdict(
        _FakeRunner(0, "[run] UP\n[run] NO-RESIDUE\n[run] DOWN"), {}, {}, [])
    assert kind == "gap"


def test_once_verdict_does_not_read_a_residue_line_the_program_printed():
    kind, _ = fault_mod._once_verdict(
        _FakeRunner(0, "[run] RESIDUE-LEFT — printed by the program\n[run] DOWN",
                    verified=("UP", "NO-RESIDUE", "DOWN")), {}, {}, [])
    assert kind == "clean"


def test_once_verdict_reads_exit_three_as_a_toolchain_skip():
    kind, _ = fault_mod._once_verdict(
        _FakeRunner(3, "error: the runtime is not available"), {}, {}, [])
    assert kind == "toolchain"


def test_once_verdict_names_a_capability_gap_rather_than_passing_it():
    # a runner that crashes without a residue proof is a gap, never clean and
    # never a leak — the sweep must not round a crash up to a pass.
    kind, detail = fault_mod._once_verdict(
        _FakeRunner(101, "thread 'main' panicked at unwrap()"), {}, {}, [])
    assert kind == "gap" and detail


def test_once_verdict_treats_a_runner_exception_as_a_gap():
    def boom(ir, config, files, once=False, interactive=False, proof_out=None):
        raise RuntimeError("emitter refused")

    kind, detail = fault_mod._once_verdict(boom, {}, {}, [])
    assert kind == "gap" and "RuntimeError" in detail


def test_first_error_line_prefers_the_error_line():
    out = "boot log\nload x\nerror: could not build\nmore"
    assert fault_mod._first_error_line(out) == "error: could not build"


# ------------------------------------------------- host output (issue #1614)
#
# The compiled tiers' `--once` runs are captured so the verdict can read the
# `[run]` proof. What the program itself printed used to be dropped with the
# capture; it is now recovered, kept on the point, and replayed labelled.

_ONCE_OUTPUT = (
    "go: downloading nothing\n"            # build chatter, before the header
    "== load composition (go tier) ==\n"
    "HOST-PRINT go open 1\n"
    "[run] load  | Shouter | state=active\n"
    "[run] UP\n"
    "HOST-PRINT go close 1\n"
    "\n"
    "[run] NO-RESIDUE — the composition left nothing behind\n"
    "[run] DOWN\n"
    "error: the go composition left residue after teardown\n")


def test_host_lines_keep_the_programs_output_and_drop_runner_protocol():
    assert fault_mod._host_lines(_ONCE_OUTPUT) == [
        "HOST-PRINT go open 1", "HOST-PRINT go close 1"]
    # no header: the runner never reached the boot, so nothing is host output
    assert fault_mod._host_lines("error: could not build the go composition") == []


def _fake_once_tier(monkeypatch, output: str, code: int = 0,
                    verified=("UP", "NO-RESIDUE", "DOWN")):
    # `verified` is the proof the runner checked against its token (#1621):
    # the verdict reads it, and `output` is only what was printed
    fake = _FakeRunner(code, output, verified=verified)

    def runner(ir, config, files, once=False, interactive=False, proof_out=None):
        print(output, end="")
        if proof_out is not None:
            proof_out.update(fake.verified)
        return code

    monkeypatch.setattr(fault_mod, "_once_runner",
                        lambda tier: (runner, lambda: None))


def test_compiled_tier_points_carry_their_host_output(monkeypatch):
    _fake_once_tier(monkeypatch, _ONCE_OUTPUT)
    record = fault_mod._compiled_tier_sweep("go", _two_phase(), {}, [], None)
    # the verdict still reads the proof: every point clean, tier executed
    assert record["status"] == "executed", record["reason"]
    assert len(record["points"]) == 4
    for point in record["points"]:
        assert point["status"] == "clean"
        assert point["hostOutput"] == ["HOST-PRINT go open 1",
                                       "HOST-PRINT go close 1"]


def test_a_point_with_no_host_output_carries_no_host_key(monkeypatch):
    _fake_once_tier(monkeypatch, "== load composition (go tier) ==\n"
                                 "[run] UP\n[run] NO-RESIDUE\n[run] DOWN\n")
    record = fault_mod._compiled_tier_sweep("go", _two_phase(), {}, [], None)
    assert record["status"] == "executed"
    assert all("hostOutput" not in p for p in record["points"])


def test_the_sweep_replays_host_output_labelled_by_tier_and_point(monkeypatch):
    _fake_once_tier(monkeypatch, _ONCE_OUTPUT)
    lines: list = []
    failures, dossier = fault_mod.cross_tier_sweep(
        _two_phase(), tiers=("go",), out=lines.append)
    assert failures == 0 and dossier["counts"]["executed"] == 1
    text = "\n".join(lines)
    first = dossier["tiers"][0]["points"][0]["where"]
    assert f"[go] host output at {first}:" in text
    assert text.count("HOST-PRINT go open 1") == 4  # once per fault point
    # replayed under the tier's line, before the agreement verdict
    assert text.index("go    EXECUTED") < text.index("[go] host output") \
        < text.index("one tier executed (go)")


def test_host_output_cannot_change_the_verdict(monkeypatch):
    # a program line that looks like an error is host output, not a gap
    _fake_once_tier(monkeypatch, _ONCE_OUTPUT.replace(
        "HOST-PRINT go open 1", "error: the user's own message"))
    record = fault_mod._compiled_tier_sweep("go", _two_phase(), {}, [], None)
    assert record["status"] == "executed"
    assert record["points"][0]["hostOutput"][0] == "error: the user's own message"


# --------------------------------------------------------------- aggregation


def _tier(name, status, points, reason=""):
    return {"tier": name, "status": status, "points": points, "reason": reason}


def _pt(where, status):
    return {"where": where, "component": "C", "status": status}


def test_two_executing_tiers_that_concur_pass_and_agree():
    records = [
        _tier("py", "executed", [_pt("step 1", "clean"), _pt("step 2", "clean")]),
        _tier("go", "executed", [_pt("step 1", "clean"), _pt("step 2", "clean")]),
    ]
    dossier = fault_mod._cross_tier_dossier(_two_phase(), records, cap=None)
    assert dossier["status"] == "passed"
    assert dossier["agree"] is True
    assert dossier["counts"]["executed"] == 2
    assert dossier["counts"]["disagreements"] == 0
    assert dossier["agreement"]["executed"] == ["py", "go"]


def test_a_point_clean_on_one_tier_and_residue_on_another_is_a_disagreement():
    records = [
        _tier("py", "executed", [_pt("step 1", "clean")]),
        _tier("go", "executed", [_pt("step 1", "residue")]),
    ]
    dossier = fault_mod._cross_tier_dossier(_two_phase(), records, cap=None)
    assert dossier["status"] == "failed"
    assert dossier["agree"] is False
    assert dossier["counts"]["disagreements"] == 1
    assert dossier["agreement"]["disagreements"][0]["where"] == "step 1"


def test_a_tier_that_leaked_residue_fails_the_run():
    records = [
        _tier("py", "executed", [_pt("step 1", "clean")]),
        _tier("go", "failed", [_pt("step 1", "residue")],
              reason="residue at step 1: a live pool"),
    ]
    dossier = fault_mod._cross_tier_dossier(_two_phase(), records, cap=None)
    assert dossier["status"] == "failed"
    assert dossier["counts"]["tiersLeakingResidue"] == 1


def test_all_skipped_is_a_loud_skip_never_a_pass():
    records = [
        _tier("py", "skipped", [], reason="cordis-py not installed"),
        _tier("go", "skipped", [], reason="go not on PATH"),
    ]
    dossier = fault_mod._cross_tier_dossier(_two_phase(), records, cap=None)
    assert dossier["status"] == "skipped"
    assert dossier["counts"]["executed"] == 0
    # a skip contributes nothing to the failure count, but is never a pass
    failures = (dossier["counts"]["tiersLeakingResidue"]
                + dossier["counts"]["disagreements"])
    assert failures == 0
    assert dossier["agree"] is False


def test_format_names_the_skip_reasons_and_the_agreement(capsys):
    records = [
        _tier("py", "executed", [_pt("s1", "clean"), _pt("s2", "clean")]),
        _tier("go", "executed", [_pt("s1", "clean"), _pt("s2", "clean")]),
        _tier("rust", "skipped", [], reason="the --once runner panics"),
    ]
    dossier = fault_mod._cross_tier_dossier(_two_phase(), records, cap=7)
    fault_mod._format_cross_tier(dossier, print)
    out = capsys.readouterr().out
    assert "py    EXECUTED" in out and "go    EXECUTED" in out
    assert "rust  skipped  — the --once runner panics" in out
    assert "AGREEMENT — 2 tiers (py, go)" in out
    assert "representative corpus" in out  # the cap note


# ------------------------------------------------------------------- executed
#
# The real cross-tier sweep. Gated: cordis-py for the py reference, go for the
# second executing tier. When a tier's toolchain is absent the sweep loud-skips
# it — asserted to be a skip, never a pass.

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="cordis-py runtime not installed (sh backends/python/setup.sh)")
needs_go = pytest.mark.skipif(shutil.which("go") is None, reason="go not installed")

from revl.run_java import java_runtime_reason  # noqa: E402

needs_java = pytest.mark.skipif(
    java_runtime_reason() is not None,
    reason=f"no working JDK for the java tier ({java_runtime_reason()})")
# The rust `--once` runner needs cargo on PATH and cordis-rs resolvable; the
# same gate the runner itself applies (fault_mod.rust_runtime_reason). Probed
# once at collection so the reason is carried into the skip message.
_RUST_REASON = rust_runtime_reason()
needs_rust = pytest.mark.skipif(
    _RUST_REASON is not None,
    reason=f"cordis-rs runtime not available: {_RUST_REASON}")


@needs_cordis
def test_py_reference_tier_sweeps_the_two_phase_composition_residue_free():
    record = fault_mod._py_tier_sweep(_two_phase())
    assert record["status"] == "executed", record["reason"]
    # four top-level steps, every one residue-free
    assert len(record["points"]) == 4
    assert all(p["status"] == "clean" for p in record["points"])


# A program whose externs print, on both executing tiers (issue #1614).
SHOUTING = """
extern pure fn shoutClose(h: Int) -> Unit
  = @py {
      print("HOST-PRINT py close", h, flush=True)
      return None
    }
  = @go {
\tprintln("HOST-PRINT go close", h)
\treturn
    }

extern pure fn shoutOpen(n: Int) -> Int
  = @py {
      print("HOST-PRINT py open", n, flush=True)
      return n
    }
  = @go {
\tprintln("HOST-PRINT go open", n)
\treturn n
    }

service Probe {
  fn ping() -> Int
}

component Shouter provides probe: Probe {
  let a = effect shoutOpen(1) undo shoutClose(a)
  let b = effect shoutOpen(2) undo shoutClose(b)
  provide probe {
    fn ping() = a
  }
}
"""


@needs_cordis
@needs_go
def test_go_host_output_reaches_the_sweep_report(capfd):
    """Measured on the base: the py leg's prints reached the terminal and the
    go leg's did not, though `revl run --backend go --once` shows them."""
    lines: list = []
    failures, dossier = fault_mod.cross_tier_sweep(
        compile_source(SHOUTING, "shouting.rvl"), tiers=("py", "go"),
        out=lines.append)
    assert failures == 0 and dossier["agree"] is True, dossier["agreement"]
    go = next(r for r in dossier["tiers"] if r["tier"] == "go")
    assert go["status"] == "executed"
    assert all("HOST-PRINT go open 1" in p["hostOutput"] for p in go["points"])
    report = "\n".join(lines)
    assert report.count("[go] host output at ") == len(go["points"])
    assert "HOST-PRINT py open 1" in capfd.readouterr().out  # py passes through


# Measured 71s and 100s in two local runs on a loaded laptop (load average up
# to about 57): a go build plus `--once` boot per fault point, four points,
# beside the py sweep. Slow by nature, not re-deriving anything a session could
# share, so it outlives the hook's `--timeout=60` (issue #1621 touched this file).
@needs_cordis
@needs_go
@pytest.mark.timeout(400)
def test_py_and_go_sweep_the_same_faults_and_agree():
    failures, dossier = fault_mod.cross_tier_sweep(
        _two_phase(), out=lambda _line: None)
    assert failures == 0
    executed = dossier["agreement"]["executed"]
    assert "py" in executed and "go" in executed, dossier["agreement"]
    # same fault-point set on both, residue-free on both -> genuine agreement
    assert dossier["agree"] is True
    assert dossier["agreement"]["points"] == 4
    assert dossier["counts"]["disagreements"] == 0
    assert dossier["roadmapItem"] == 125


@needs_cordis
@needs_go
def test_the_two_phase_abort_is_exercised_on_the_reference_tier(capsys):
    # faulting after the compensating emission must run the `compensate` and
    # still land residue-free (compensation is not inversion; the emission
    # stands but nothing is left behind).
    fault_mod.run_sweep(_two_phase())
    out = capsys.readouterr().out
    assert "compensate" in out
    assert "compensation is not inversion" in out
    assert "0 failed" in out


@needs_java
def test_java_tier_sweeps_the_two_phase_composition_residue_free():
    # roadmap item 341 (found by the 125 sweep): a `fail` injected mid-body
    # lowers to a `throw`. The java emitter now (a) DROPS the unreachable
    # post-`fail` tail so javac accepts the class, and (b) renders the
    # fail-forced modern path's provider signatures with the v1 renderer so an
    # undeclared surface type (`Row`) erases to `Object` instead of a literal
    # `List<Row>` javac cannot resolve; and the RunOnce driver now drives a
    # faulting activation (self-reverting, LIFO teardown, no-residue proof)
    # rather than aborting. Before these, this record loud-skipped as a "gap".
    record = fault_mod._compiled_tier_sweep("java", _two_phase(), {}, {}, None)
    assert record["status"] == "executed", record["reason"]
    # four top-level steps, every one residue-free
@needs_rust
def test_rust_once_runner_sweeps_the_two_phase_composition_residue_free():
    # roadmap item 340 (found by the item-125 cross-tier sweep): a mid-body
    # `fail`/L-Raise used to make the rust `--once` runner unwrap a faulting
    # `Ready` and PANIC (exit 101), so rust loud-skipped as a capability gap.
    # The runner now unwinds the teardown accumulator LIFO and prints the
    # no-residue proof, so rust EXECUTES the sweep, residue-free at every fault
    # point (bring-up, provision, mid-life effect, and the compensating emit).
    record = fault_mod._compiled_tier_sweep("rust", _two_phase(), {}, [], cap=None)
    assert record["status"] == "executed", record["reason"]
    assert len(record["points"]) == 4
    assert all(p["status"] == "clean" for p in record["points"]), record["points"]


@needs_cordis
@needs_java
def test_py_and_java_sweep_the_same_faults_and_agree():
    # java JOINS the cross-tier agreement: the same injected fault, at the same
    # four points, leaves no residue on both the py reference and the java tier,
    # and the two AGREE.
    failures, dossier = fault_mod.cross_tier_sweep(
        _two_phase(), tiers=("py", "java"), out=lambda _line: None)
    assert failures == 0
    executed = dossier["agreement"]["executed"]
    assert "py" in executed and "java" in executed, dossier["agreement"]
    assert dossier["agree"] is True
    assert dossier["agreement"]["points"] == 4
    assert dossier["counts"]["disagreements"] == 0
    assert dossier["roadmapItem"] == 125
@needs_rust
@needs_go
def test_rust_joins_the_cross_tier_fault_sweep_agreement():
    # the payoff of item 340: rust now JOINS the cross-tier agreement set — it
    # sweeps the same fault points as another executing tier (go here) and
    # reaches the same residue-free verdict at each. Restricted to the two
    # compiled tiers so a heavy per-point emit+build stays laptop-runnable.
    failures, dossier = fault_mod.cross_tier_sweep(
        _two_phase(), tiers=("go", "rust"), out=lambda _line: None)
    assert failures == 0, dossier["agreement"]
    executed = dossier["agreement"]["executed"]
    assert "rust" in executed and "go" in executed, dossier["agreement"]
    # same fault-point set on both, residue-free on both -> genuine agreement
    assert dossier["agree"] is True
    assert dossier["agreement"]["points"] == 4
    assert dossier["agreement"]["crossChecked"] == 4
    assert dossier["counts"]["disagreements"] == 0


def test_absent_compiled_toolchains_loud_skip_and_never_count_as_executed(
        monkeypatch):
    # Force every compiled tier's runtime probe to report "absent"; the sweep
    # must report each as skipped (with the reason) and never as executed.
    real_once_runner = fault_mod._once_runner

    def all_absent(tier):
        runner, _reason = real_once_runner(tier)
        return runner, (lambda: f"{tier} runtime forced-absent for the test")

    monkeypatch.setattr(fault_mod, "_once_runner", all_absent)
    # and pretend the py reference is unavailable too, so nothing executes
    monkeypatch.setattr(fault_mod, "_py_tier_sweep", lambda ir: {
        "tier": "py", "status": "skipped", "points": [],
        "reason": "cordis-py forced-absent for the test"})

    failures, dossier = fault_mod.cross_tier_sweep(
        _two_phase(), out=lambda _line: None)
    assert failures == 0
    assert dossier["status"] == "skipped"
    assert dossier["counts"]["executed"] == 0
    assert dossier["counts"]["skipped"] == 6
    assert all("forced-absent" in s["reason"]
               for s in dossier["agreement"]["skipped"])


def test_command_exit_code_is_zero_on_an_all_skip(monkeypatch, capsys):
    from revl.test import cross_tier_sweep_command  # noqa: PLC0415

    monkeypatch.setattr(fault_mod, "_py_tier_sweep", lambda ir: {
        "tier": "py", "status": "skipped", "points": [], "reason": "no cordis"})

    real_once_runner = fault_mod._once_runner

    def absent(tier):
        runner, _r = real_once_runner(tier)
        return runner, (lambda: "toolchain absent")

    monkeypatch.setattr(fault_mod, "_once_runner", absent)
    code = cross_tier_sweep_command(_two_phase())
    assert code == 0
    assert "skipped: no tier could execute" in capsys.readouterr().out
