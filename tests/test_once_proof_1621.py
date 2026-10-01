"""Issue #1621: the `--once` residue proof cannot be forged by program output.

A `--once` runner boots the composition as a child process and reads the
child's stdout for `UP`, `NO-RESIDUE`, `RESIDUE-LEFT` and `DOWN`. Extern bodies
run in that child and share its stdout, so on the base a `@go` body that
printed `[run] UP`, `[run] NO-RESIDUE` and `[run] DOWN` and then exited 0
before teardown was reported clean, and the fault sweep recorded its fault
points clean.

Now the runner hands the child a per-run token on stdin (`revl._once_proof`),
the child tags its proof lines with it, and only tagged lines count. People see
the same `[run] ...` lines as before.

Three layers: the token bookkeeping (pure), the forged program on the go tier
(gated on go), and an honest program on every tier whose toolchain is present,
proving each child runner tags its proof (gated per tier).
"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl import fault as fault_mod  # noqa: E402
from revl._once_proof import SPEC_FLAG, OnceProof  # noqa: E402
from revl.run_go import go_runtime_reason, run_go  # noqa: E402
from revl.run_java import java_runtime_reason, run_java  # noqa: E402
from revl.run_rust import run_rust, rust_runtime_reason  # noqa: E402
from revl.run_ts import run_ts, ts_runtime_reason  # noqa: E402
from revl.run_wasm import run_wasm, wasm_runtime_reason  # noqa: E402

PAIR = ROOT / "examples" / "counter_pair.rvl"


# ------------------------------------------------------------ the token, pure

def test_a_tagged_line_is_the_proof_and_reads_as_it_always_did():
    proof = OnceProof("run", token="t0k3n")
    assert proof.line("[run#t0k3n] UP\n") == "[run] UP\n"
    assert proof.line("[run#t0k3n] NO-RESIDUE — the composition left nothing "
                      "behind\n") == ("[run] NO-RESIDUE — the composition left "
                                      "nothing behind\n")
    assert proof.line("[run#t0k3n] DOWN\n") == "[run] DOWN\n"
    assert proof.verdict() == {"up": True, "down": True, "noResidue": True,
                               "residueLeft": False}


def test_a_printed_proof_line_is_output_and_counts_for_nothing():
    proof = OnceProof("run", token="t0k3n")
    for line in ("[run] UP\n", "[run] NO-RESIDUE — forged\n", "[run] DOWN\n",
                 "[run#guess] NO-RESIDUE\n", "  [run#t0k3n] NO-RESIDUE\n"):
        assert proof.line(line) == line  # shown as it came
    assert proof.verdict() == {"up": False, "down": False, "noResidue": False,
                               "residueLeft": False}


def test_a_tagged_residue_line_is_recorded():
    proof = OnceProof("run", token="t0k3n")
    proof.line("[run#t0k3n] RESIDUE-LEFT — see the residue lines above\n")
    assert proof.residue_left and not proof.no_residue


def test_every_run_draws_its_own_unguessable_token():
    tokens = {OnceProof().token for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(t) == 32 and int(t, 16) >= 0 for t in tokens)


def test_the_token_goes_to_stdin_as_one_line_and_stdin_closes():
    class Pipe(io.StringIO):
        closed_by_send = False

        def close(self):
            self.closed_by_send = True
            self.sent = self.getvalue()
            super().close()

    pipe = Pipe()
    proof = OnceProof("run", token="t0k3n")
    proof.send(pipe)
    assert pipe.closed_by_send and pipe.sent == "t0k3n\n"
    assert SPEC_FLAG == "proofOnStdin"


def test_record_copies_the_verdict_for_a_caller():
    proof = OnceProof("run", token="t")
    proof.line("[run#t] UP\n")
    out: dict = {}
    proof.record(out)
    assert out["up"] is True and out["noResidue"] is False
    proof.record(None)  # no caller dict: nothing to do


# ------------------------------------------------ the forged program, on go

# The extern prints the three proof lines and exits 0 before teardown. `os` is
# in scope on the go tier because the `emit ... compensate` pulls in the
# teardown imports. Forger is listed first so the sweep faults it first.
FORGE = '''
extern pure fn shutIt(h: Int) -> Unit
  = @py {
      return None
    }
  = @go {
\tprintln("UNDO RAN go", h)
\treturn
    }

extern pure fn openIt(n: Int) -> Int
  = @py {
      return n
    }
  = @go {
\tprintln("[run] UP")
\tprintln("[run] NO-RESIDUE — the composition left nothing behind")
\tprintln("[run] DOWN")
\tos.Exit(0)
\treturn n
    }

service Database {
  fn query(sql: Str) -> List[Row]
  emission fn execute(sql: Str) -> Int
}
component Forger requires db: Database {
  let a = effect openIt(1) undo shutIt(a)
  emit db.execute("INSERT INTO log VALUES (1)")
       compensate db.execute("DELETE FROM log WHERE id = 1")
}
component PgDatabase provides db: Database {
  config { url: Str = "postgres://localhost/app" }
  let pool = effect Pool.open(config.url, 4) undo pool.close()
  provide db {
    fn query(sql)   = pool.query(sql)
    fn execute(sql) = pool.execute(sql)
  }
}
'''

_GO_REASON = go_runtime_reason()
needs_go = pytest.mark.skipif(_GO_REASON is not None,
                              reason=f"no go toolchain: {_GO_REASON}")


def _captured(fn, *args, **kwargs):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = fn(*args, **kwargs)
    return code, buffer.getvalue()


@needs_go
def test_go_once_refuses_a_proof_the_program_printed():
    """On the base this exited 0: the program's own three lines were the
    proof, and no teardown or residue check ever ran."""
    proof: dict = {}
    code, out = _captured(run_go, compile_source(FORGE, "forge.rvl"), {}, [],
                          once=True, proof_out=proof)
    assert code == 1, out
    assert proof == {"up": False, "down": False, "noResidue": False,
                     "residueLeft": False}, out
    # the program's lines are still shown, as program output
    assert "[run] NO-RESIDUE" in out
    assert "UNDO RAN" not in out  # no teardown happened, so nothing is clean


@needs_go
def test_the_go_sweep_records_no_forged_point_clean():
    """On the base the sweep recorded both of Forger's fault points clean."""
    ir = compile_source(FORGE, "forge.rvl")
    with contextlib.redirect_stdout(io.StringIO()):
        record = fault_mod._compiled_tier_sweep("go", ir, {}, [], None)
    assert not any(p["status"] == "clean" for p in record["points"]), record
    assert record["status"] != "executed", record


# ------------------------------------- an honest program, on every tier present

_TIERS = [
    ("go", run_go, go_runtime_reason),
    ("rust", run_rust, rust_runtime_reason),
    ("java", run_java, java_runtime_reason),
    ("ts", run_ts, ts_runtime_reason),
    ("wasm", run_wasm, wasm_runtime_reason),
]


@pytest.mark.parametrize(
    "tier,runner,reason_of", _TIERS, ids=[t for t, _, _ in _TIERS])
def test_each_child_runner_tags_its_proof_and_people_see_no_token(
        tier, runner, reason_of):
    reason = reason_of()
    if reason is not None:
        pytest.skip(f"{tier} runtime not available: {reason}")
    ir = compile_source(PAIR.read_text(encoding="utf-8"), str(PAIR))
    proof: dict = {}
    code, out = _captured(runner, ir, {}, [str(PAIR)], once=True,
                          proof_out=proof)
    assert code == 0, out
    assert proof == {"up": True, "down": True, "noResidue": True,
                     "residueLeft": False}, out
    assert "[run] UP" in out and "[run] NO-RESIDUE" in out and "[run] DOWN" in out
    assert "[run#" not in out, "the token must never reach the human output"


# ------------------------------------------- the wasm record channel's frames
#
# In record mode (`REVL_WAL` set) the wasm harness relays one `[wal] {...}`
# frame per witnessed registration on the same stdout, and the runner drains
# each into the durable WAL. An undischarged descriptor there is what
# `revl recover` replays. No direct route from a wasm guest to stdout was found
# (the runtime defines no WASI imports), so tagging the frames with the run's
# token is defence in depth: only the harness's own channel reaches the WAL.

import json  # noqa: E402

from revl import run_wasm as run_wasm_module  # noqa: E402

CRASHPROOF = ROOT / "backends" / "wasm" / "scenarios" / "crashproof" / "crashproof.rvl"
_FRAME = {"seq": 9, "receiver": "deleteRow", "method": "deleteRow",
          "witness": "row#9"}


def test_the_proof_names_a_tagged_prefix_for_another_channel():
    assert OnceProof("run", token="t0k3n").tag("wal") == "[wal#t0k3n] "


def test_an_untagged_wal_frame_is_not_drained(tmp_path):
    wal = tmp_path / "w.wal"
    with open(wal, "w", encoding="utf-8") as handle:
        seq = run_wasm_module.drain_wal_frame(
            handle, "[wal] " + json.dumps(_FRAME) + "\n", "[wal#t0k3n] ")
    assert seq is None
    assert wal.read_text(encoding="utf-8") == ""


def test_a_tagged_wal_frame_is_drained(tmp_path):
    wal = tmp_path / "w.wal"
    with open(wal, "w", encoding="utf-8") as handle:
        seq = run_wasm_module.drain_wal_frame(
            handle, "[wal#t0k3n] " + json.dumps(_FRAME) + "\n", "[wal#t0k3n] ")
    assert seq == 9
    record = json.loads(wal.read_text(encoding="utf-8"))
    assert record["record"] == "discharge-descriptor" and record["seq"] == 9


_WASM_REASON = wasm_runtime_reason()
needs_wasm = pytest.mark.skipif(_WASM_REASON is not None,
                                reason=f"no cordis-wasm runtime: {_WASM_REASON}")


@needs_wasm
def test_the_harness_tags_its_wal_frames_with_the_runs_token(tmp_path):
    """The real cordis-wasm harness, in record mode, handed a token on stdin
    as `run_wasm` does: every frame it relays carries the token. On the base
    it printed bare `[wal] ` frames."""
    import os
    import subprocess

    ir = compile_source(CRASHPROOF.read_text(encoding="utf-8"), str(CRASHPROOF))
    order = run_wasm_module._load_order(ir)
    modules = run_wasm_module._emit_modules(ir, record=True)
    spec = tmp_path / "run.spec.json"
    spec.write_text(json.dumps({
        "name": "run", "once": True, "record": True, "order": order,
        "modules": {name: modules[name] for name in order},
        SPEC_FLAG: True}), encoding="utf-8")
    env = dict(os.environ)
    env["CORDIS_WASM"] = str(run_wasm_module._cordis_wasm_dir())
    proc = subprocess.run(
        [run_wasm_module._cordis_wasm_python(), str(run_wasm_module._HARNESS),
         str(spec)], input="t0k3n\n", capture_output=True, text=True, env=env,
        check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    frames = [ln for ln in proc.stdout.splitlines() if ln.startswith("[wal")]
    assert frames, proc.stdout
    assert all(ln.startswith("[wal#t0k3n] {") for ln in frames), frames


@needs_wasm
def test_a_wasm_record_run_still_lands_its_descriptor_in_the_wal(
        tmp_path, monkeypatch):
    """End to end through `run_wasm` with `REVL_WAL` set: the tagged frame is
    drained, the run commits, and no token reaches the human output."""
    wal = tmp_path / "run.wal"
    monkeypatch.setenv("REVL_WAL", str(wal))
    ir = compile_source(CRASHPROOF.read_text(encoding="utf-8"), str(CRASHPROOF))
    proof: dict = {}
    code, out = _captured(run_wasm, ir, {}, [str(CRASHPROOF)], once=True,
                          proof_out=proof)
    assert code == 0, out
    assert proof["noResidue"] is True, out
    records = [json.loads(ln) for ln in wal.read_text(encoding="utf-8").splitlines()]
    kinds = [r["record"] for r in records]
    assert kinds.count("discharge-descriptor") == 1, kinds
    assert "discharge" in kinds and "activation-complete" in kinds, kinds
    assert "[run#" not in out and "[wal#" not in out, out
