"""Roadmap item 441 / issue #120, S2 — `revl goal audit`, the blind-spot report
(docs/design/458-termination-language-surface.md §7 "S2", 441-goal-contracts.md
§5.2).

441 §5.2's answer to "the criteria are drafted by the agent, what does the
operator review?" is "not the criteria, the blind spot": the class-(c)
capabilities in the run's reach that no criterion in the contract observes. This
is the §C7 set difference

    unobserved  =  classC(whole-run reach)  minus  classC(contract's cone)

computed as a PURE function of a compiled IR (`revl.goal_audit.blind_spot`) — no
session, no policy, no runtime — reading the L5 `termination` header fact S1
landed (issue #520). The observation cone is the transitive reach from every
contract component through its `requires` wiring, so a criterion that reads a
verifier is credited with whatever that verifier's provider reaches.

Covered here: the IR-computable oracle bullets of §7 S2. NOT covered: the §11.3
measurement over a real item-248 agent run ("observes N of M for one real run"),
which needs a harness workload and is tracked as remaining work on the item.
"""

import importlib
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.goal_audit import (  # noqa: E402
    blind_spot,
    contract_components,
    goal_operations,
    render,
)


# A run reaching THREE class-(c) capabilities. The contract's criterion reads a
# `Runner` verifier whose provider reaches `ci`; the `Worker` reaches `gwsend`
# and `shell`, which the contract's cone never touches — two blind spots.
SIX = """
service Done   { fn tests_pass() -> Criterion  fn no_secret() -> Guard }
service Runner { fn last_exit() -> Int  emission fn kick() -> Int }
extern emission fn ci(h: Str)     -> Int = @py { return 0 }
extern emission fn gwsend(h: Str) -> Int = @py { return 0 }
extern emission fn shell(c: Str)  -> Int = @py { return 0 }
component TheRunner provides runner: Runner {
  provide runner {
    fn last_exit() = 0
    fn kick() { let x = emit ci("run") return x }
  }
}
component Contract requires runner: Runner provides done: Done {
  provide done {
    fn tests_pass() = Some(runner.last_exit() == 0)
    fn no_secret()  = Some(false)
  }
}
component Worker {
  emit gwsend("api.stripe.com")
  emit shell("rm -rf /")
}
"""


# ------------------------------------------------------------- the header fact

def test_goal_operations_reads_the_l5_termination_key():
    ops = goal_operations(compile_source(SIX))
    assert ops == {"Done": {"tests_pass": "criterion", "no_secret": "guard"}}


def test_contract_components_are_the_goal_service_providers():
    contract = contract_components(compile_source(SIX))
    assert [c["name"] for c in contract] == ["Contract"]
    prov = contract[0]["provisions"]
    assert prov == [{"key": "done", "service": "Done",
                     "criteria": ["tests_pass"], "guards": ["no_secret"]}]


# ------------------------------------------------------------- the set difference

def test_blind_spot_names_the_unobserved_capabilities():
    """The core §5.2 report: the run reaches three class-(c) capabilities, the
    contract observes one (`ci`, through its verifier's provider), and the two
    the contract's cone never touches are named as UNOBSERVED — each in the
    capability spelling `revl audit` uses (the token, not the call site)."""
    report = blind_spot(compile_source(SIX))
    assert report["reachClassC"] == ["ci", "gwsend", "shell"]
    assert [e["token"] for e in report["observed"]] == ["ci"]
    assert [e["token"] for e in report["unobserved"]] == ["gwsend", "shell"]
    assert (report["observes"], report["of"]) == (1, 3)
    # the crossing provenance names the reaching component and the via-label
    gwsend = next(e for e in report["unobserved"] if e["token"] == "gwsend")
    assert gwsend["crossings"] == [
        {"component": "Worker", "via": "gwsend", "kind": "host"}]


def test_no_goal_service_yields_no_report_not_an_empty_one():
    """A composition with no `Criterion`/`Guard` operation has no contract; the
    report is `None` (rendered as the "no goal service" line), never an empty
    report over zero criteria."""
    plain = compile_source(
        "service S { fn f() -> Int }\n"
        "component C provides s: S { provide s { fn f() = 0 } }\n")
    assert goal_operations(plain) == {}
    assert contract_components(plain) == []
    assert blind_spot(plain) is None
    assert "no goal service" in render(None)


def test_a_new_criterion_moves_a_capability_from_unobserved_to_observed():
    """Adding a criterion that observes a capability (by requiring the verifier
    whose provider reaches it) moves that token from UNOBSERVED to observed and
    changes nothing else."""
    before = blind_spot(compile_source(SIX))
    assert "gwsend" in [e["token"] for e in before["unobserved"]]

    # Give the contract a second verifier — a `Mailer` whose provider reaches
    # `gwsend` — and a criterion that reads it. `gwsend` is now in the cone.
    after_src = SIX.replace(
        "component Contract requires runner: Runner provides done: Done {",
        "service Mailer { fn last_sent() -> Int  emission fn flush() -> Int }\n"
        "component TheMailer provides mailer: Mailer {\n"
        "  provide mailer {\n"
        "    fn last_sent() = 0\n"
        "    fn flush() { let x = emit gwsend(\"api.stripe.com\") return x }\n"
        "  }\n"
        "}\n"
        "component Contract requires runner: Runner, mailer: Mailer "
        "provides done: Done {",
    ).replace(
        "    fn no_secret()  = Some(false)",
        "    fn no_secret()  = Some(false)\n"
        "    fn mail_ok()    = Some(mailer.last_sent() == 0)",
    ).replace(
        "service Done   { fn tests_pass() -> Criterion  fn no_secret() -> Guard }",
        "service Done   { fn tests_pass() -> Criterion  fn no_secret() -> Guard "
        " fn mail_ok() -> Criterion }",
    )
    after = blind_spot(compile_source(after_src))
    assert "gwsend" in [e["token"] for e in after["observed"]]
    assert "gwsend" not in [e["token"] for e in after["unobserved"]]
    # nothing else moved: shell is still the sole blind spot
    assert [e["token"] for e in after["unobserved"]] == ["shell"]


def test_report_is_byte_identical_across_two_compiles():
    a = blind_spot(compile_source(SIX))
    b = blind_spot(compile_source(SIX))
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert render(a) == render(b)


def test_witnessed_reach_is_class_a_and_not_counted_as_authority():
    """A `witnessed` extern (item 243) is a transactional class-(a) crossing,
    not class-(c) authority; it does not appear in the blind-spot surface."""
    src = """
service Done { fn ok() -> Criterion }
type W = { p: Str }
type E = { c: Str }
extern pure fn rel(w: W) -> Unit = @py { pass }
extern witnessed[fs] fn grab(p: Str) -> Result[W, E] undo idempotent rel(result) = @py { return (0, 0) }
extern emission fn gwsend(h: Str) -> Int = @py { return 0 }
component Contract provides done: Done { provide done { fn ok() = Some(true) } }
component Worker {
  let w = effect grab("f")
  emit gwsend("x")
}
"""
    report = blind_spot(compile_source(src))
    # only the plain emission `gwsend` is authority; the witnessed `grab`/`fs`
    # is class-(a) and absent.
    assert report["reachClassC"] == ["gwsend"]


# --------------------------------------------------------------- the CLI

def _run_cli(argv: list[str]) -> tuple[int, str]:
    main = importlib.import_module("revl.__main__").main
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(argv)
    return code, buf.getvalue()


def test_cli_goal_audit_human_and_json(tmp_path):
    src = tmp_path / "c.rvl"
    src.write_text(SIX)
    code, out = _run_cli(["goal", "audit", str(src)])
    assert code == 0
    assert "UNOBSERVED" in out and "gwsend" in out and "shell" in out
    assert "observes 1" in out

    code, out = _run_cli(["goal", "audit", "--json", str(src)])
    assert code == 0
    doc = json.loads(out)
    assert [e["token"] for e in doc["unobserved"]] == ["gwsend", "shell"]


def test_cli_goal_audit_no_contract(tmp_path):
    src = tmp_path / "plain.rvl"
    src.write_text("service S { fn f() -> Int }\n"
                   "component C provides s: S { provide s { fn f() = 0 } }\n")
    code, out = _run_cli(["goal", "audit", str(src)])
    assert code == 0
    assert "no goal service" in out
    # --json emits `null`, not an empty object
    code, out = _run_cli(["goal", "audit", "--json", str(src)])
    assert code == 0
    assert json.loads(out) is None
