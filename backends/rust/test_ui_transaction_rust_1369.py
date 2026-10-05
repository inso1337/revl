"""Issue #1369 (item 522 slice 3) on the rust tier: the UI transaction unit.

The rust mirror of tests/test_ui_transaction_runtime_1369.py (py),
backends/typescript/tests/ui_transaction.test.ts (ts) and
backends/go/scenarios/emitted/ui_transaction/exec_test.go (go).

A provide method that crosses a computer-use verb is one unit, the unit
`revl.ui_transaction.method_plan` reads. If its call panics, the unit settles
the entries this call registered, witnessed inverses first and compensations
second, each newest first and each caught, and the panic resumes unchanged.
The entries never reach the activation, so the clean unload after the failed
call does not discharge them and a later `revl_abort` does not run them twice.

Measured on the base with this program and harness (the record types shimmed
so the harness compiles): 8 of the 11 tests fail and the 3 controls pass. A
failed call ran no compensation at all. Each declared compensation of a
crossing that returned stayed registered on the activation and the clean
unload after the failed call discharged it, a crossing that panicked
registered nothing, and nothing recorded a run.

Every host body goes through `ui_step` (or `ui_compensate` for `clear_memo`),
defined in the harness: it records its line and panics on the occurrence a
test configured, so a test picks the failure without a second program. The
source lives here rather than in backends/rust/scenarios/ so it does not join
the scoring corpora.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from revl import compile_source  # noqa: E402

import test_emit_rust as rust_tests  # noqa: E402

_spec = importlib.util.spec_from_file_location("rust_emit_1369", ROOT / "backends" / "rust" / "emit.py")
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)

SOURCE = """\
type UiTarget = {
  application: Str
  window: Str
  role: Str
  name: Str
  evidence: Str
  action: Str
  session: Str
  bounds: Str
  expiry: Int
  confirm: Bool
}

type Mark = { line: Str }
type MarkError = { code: Str }

extern pure fn unmark(w: Mark) -> Unit = @rs { ui_step(format!("undo:{}", w.line)); }

extern witnessed[fs] fn mark(line: Str) -> Result[Mark, MarkError] undo unmark(result) = @rs {
    ui_step(format!("mark:{}", line));
    Ok(Mark { line })
}

extern pure fn restore_row() = @rs { ui_step(String::from("compensate:restore_row")); }

extern emission fn put_row(body: Str) -> Int compensate restore_row() = @rs {
    ui_step(format!("put:{}", body));
    1
}

extern emission[screen.observe] fn read_pane(region: Str) -> Str = @rs {
    ui_step(String::from("observe"));
    String::from("pane")
}

extern emission[ui.find] fn locate(pane: Str, name: Str) -> UiTarget = @rs {
    ui_step(format!("locate:{}", name));
    UiTarget { application: String::from("Billing"), window: String::new(), role: String::new(), name, evidence: String::new(), action: String::new(), session: String::new(), bounds: String::new(), expiry: 0, confirm: false }
}

extern pure fn clear_amount() = @rs { ui_step(String::from("compensate:clear_amount")); }
extern pure fn clear_memo() = @rs { ui_compensate(String::from("compensate:clear_memo")); }
extern pure fn clear_note() = @rs { ui_step(String::from("compensate:clear_note")); }

extern emission[ui.text] fn type_amount(target: UiTarget, s: Str)
  compensate clear_amount()
  = @rs { ui_step(String::from("type_amount")); }

extern emission[ui.text] fn type_memo(target: UiTarget, s: Str)
  compensate clear_memo()
  = @rs { ui_step(String::from("type_memo")); }

extern emission[ui.click] fn actuate(target: UiTarget) = @rs { ui_step(String::from("actuate")); }

extern emission[ui.text] fn type_note(target: UiTarget, s: Str)
  compensate clear_note()
  = @rs { ui_step(String::from("type_note")); }

extern emission[ui.download] fn fetch_receipt(target: UiTarget) -> Str = @rs {
    ui_step(String::from("fetch_receipt"));
    String::from("receipt")
}

extern emission[ui.find] async fn locate_async(pane: Str, name: Str) -> UiTarget = @rs {
    ui_step(format!("locate:{}", name));
    UiTarget { application: String::from("Billing"), window: String::new(), role: String::new(), name, evidence: String::new(), action: String::new(), session: String::new(), bounds: String::new(), expiry: 0, confirm: false }
}

extern emission[ui.click] async fn actuate_async(target: UiTarget) = @rs { ui_step(String::from("actuate")); }

service Ops { emission fn run(region: Str) -> Int }
service TailOps { emission fn run(region: Str) }
service AsyncOps { async emission fn run(region: Str) -> Int }
service MixedOps { emission fn run(region: Str) }

component Agent provides ops: Ops {
  provide ops {
    fn run(region) {
      let pane1 = emit read_pane(region)
      let amount = emit locate(pane1, "Amount")
      emit type_amount(amount, "10")
      let pane2 = emit read_pane(region)
      let memo = emit locate(pane2, "Memo")
      emit type_memo(memo, "m")
      let pane3 = emit read_pane(region)
      let approve = emit locate(pane3, "Approve")
      emit actuate(approve)
      let pane4 = emit read_pane(region)
      let checked = emit locate(pane4, "Approve")
      let pane5 = emit read_pane(region)
      let note = emit locate(pane5, "Note")
      emit type_note(note, "n")
      let pane6 = emit read_pane(region)
      let receipt = emit locate(pane6, "Attach")
      let saved = emit fetch_receipt(receipt)
      return 1
    }
  }
}

component TailAgent provides tail: TailOps {
  provide tail {
    fn run(region) {
      let pane = emit read_pane(region)
      let amount = emit locate(pane, "Amount")
      emit type_amount(amount, "10")
      let approve = emit locate(pane, "Approve")
      emit actuate(approve)
    }
  }
}

component AsyncAgent provides async_ops: AsyncOps {
  provide async_ops {
    async fn run(region) {
      let amount = emit locate_async(region, "Amount")
      emit type_amount(amount, "10")
      let approve = emit locate_async(region, "Approve")
      emit actuate_async(approve)
      return 1
    }
  }
}

component Mixed provides mixed: MixedOps {
  provide mixed {
    fn run(region) {
      effect mark("a")
      let n = emit put_row("p")
      let target = emit locate(region, "Amount")
      emit type_amount(target, "10") compensate clear_note()
      emit actuate(target)
    }
  }
}
"""

HARNESS = r"""
// the fake desktop: every host body records its line, and panics on the
// occurrence a test configured ("locate:Approve#2" is the second resolution
// of Approve); clear_memo panics when UI_COMP_FAIL is set
struct UiDesk {
    log: Vec<String>,
    seen: std::collections::HashMap<String, usize>,
    fail: String,
    comp_fail: bool,
}

static UI_DESK: std::sync::Mutex<Option<UiDesk>> = std::sync::Mutex::new(None);

fn ui_desk<R>(f: impl FnOnce(&mut UiDesk) -> R) -> R {
    let mut guard = UI_DESK.lock().unwrap_or_else(|e| e.into_inner());
    let desk = guard.get_or_insert_with(|| UiDesk {
        log: Vec::new(), seen: std::collections::HashMap::new(), fail: String::new(), comp_fail: false,
    });
    f(desk)
}

fn ui_step(line: String) {
    let fail = ui_desk(|d| {
        d.log.push(line.clone());
        let n = d.seen.entry(line.clone()).or_insert(0);
        *n += 1;
        let (want, nth) = match d.fail.split_once('#') {
            Some((w, n)) => (w.to_string(), n.parse::<usize>().unwrap_or(1)),
            None => (d.fail.clone(), 1),
        };
        line == want && *n == nth
    });
    if fail {
        panic!("the substrate could not confirm {}", line);
    }
}

fn ui_compensate(line: String) {
    let fail = ui_desk(|d| {
        d.log.push(line);
        d.comp_fail
    });
    if fail {
        panic!("the memo field is gone");
    }
}

#[cfg(test)]
mod ui_transaction_1369 {
    use super::*;

    const POSTCONDITION_UNMET: &str = "locate:Approve#2";
    const FORWARD_TO_THE_CHECK: &[&str] = &[
        "observe", "locate:Amount", "type_amount",
        "observe", "locate:Memo", "type_memo",
        "observe", "locate:Approve", "actuate",
        "observe", "locate:Approve",
    ];

    fn reset(fail: &str) -> cordis::Context {
        ui_desk(|d| {
            d.log.clear();
            d.seen.clear();
            d.fail = fail.to_string();
            d.comp_fail = false;
        });
        cordis::Context::new()
    }

    fn trace() -> Vec<String> {
        ui_desk(|d| d.log.clone())
    }

    fn compensations() -> Vec<String> {
        trace().into_iter()
            .filter_map(|l| l.strip_prefix("compensate:").map(str::to_string))
            .collect()
    }

    fn strings(xs: &[&str]) -> Vec<String> {
        xs.iter().map(|s| s.to_string()).collect()
    }

    fn panics(call: impl FnOnce()) -> String {
        match std::panic::catch_unwind(std::panic::AssertUnwindSafe(call)) {
            Ok(()) => panic!("the call returned; want a panic"),
            Err(payload) => revl_panic_text(payload.as_ref()),
        }
    }

    fn sole_run(label: &str) -> RevlUiRun {
        let runs = revl_ui_transaction_runs(label);
        assert_eq!(runs.len(), 1, "want exactly one settled unit, got {runs:?}");
        runs[0].clone()
    }

    fn ran_steps(run: &RevlUiRun) -> Vec<String> {
        run.ran.iter().map(|e| e.step.clone()).collect()
    }

    // a failed call settles its unit, then the clean unload runs nothing more

    #[test]
    fn five_step_oracle_compensates_steps_two_and_one_in_order() {
        let root = reset(POSTCONDITION_UNMET);
        let fiber = root.plugin(agent(), ());
        fiber.try_wait().unwrap();
        let ops = root.require::<Box<dyn Ops>>("ops").unwrap();
        let error = panics(|| { ops.run("r".to_string()); });
        assert!(error.contains("could not confirm locate:Approve"), "{error}");
        // performed at the failure, not at the unload
        let mut expected = strings(FORWARD_TO_THE_CHECK);
        expected.extend(strings(&["compensate:clear_memo", "compensate:clear_amount"]));
        assert_eq!(trace(), expected);
        let run = sole_run("Agent.teardown.phase2");
        assert_eq!(run.unit, "ops.run");
        assert_eq!(run.failed_step, "locate");
        assert_eq!(ran_steps(&run), strings(&["type_memo", "type_amount"]));
        let comps: Vec<String> = run.ran.iter().map(|e| e.compensation.clone()).collect();
        assert_eq!(comps, strings(&["clear_memo", "clear_amount"]));
        assert_eq!(run.crossed.last().unwrap(), "locate");
        assert!(!run.crossed.contains(&"type_note".to_string()));
        assert!(!run.crossed.contains(&"fetch_receipt".to_string()));
        assert!(run.error.contains("could not confirm locate:Approve"));
        // the clean unload after the failed call: nothing runs again
        drop(ops);
        fiber.dispose().unwrap();
        assert_eq!(compensations(), strings(&["clear_memo", "clear_amount"]));
    }

    #[test]
    fn completed_call_runs_no_compensation_and_unload_discharges() {
        let root = reset("");
        let fiber = root.plugin(agent(), ());
        fiber.try_wait().unwrap();
        let ops = root.require::<Box<dyn Ops>>("ops").unwrap();
        assert_eq!(ops.run("r".to_string()), 1);
        assert_eq!(compensations(), Vec::<String>::new());
        assert!(revl_ui_transaction_runs("Agent.teardown.phase2").is_empty());
        drop(ops);
        fiber.dispose().unwrap();
        assert_eq!(compensations(), Vec::<String>::new());
    }

    #[test]
    fn completed_calls_entries_wait_for_the_abort() {
        let root = reset("");
        let fiber = root.plugin(agent(), ());
        fiber.try_wait().unwrap();
        let ops = root.require::<Box<dyn Ops>>("ops").unwrap();
        ops.run("r".to_string());
        revl_abort("Agent.teardown.phase2");
        drop(ops);
        fiber.dispose().unwrap();
        assert_eq!(compensations(), strings(&["clear_note", "clear_memo", "clear_amount"]));
    }

    // the rules the run keeps

    #[test]
    fn failing_crossings_own_compensation_runs_first() {
        let root = reset("type_memo");
        let fiber = root.plugin(agent(), ());
        fiber.try_wait().unwrap();
        let ops = root.require::<Box<dyn Ops>>("ops").unwrap();
        let error = panics(|| { ops.run("r".to_string()); });
        assert!(error.contains("could not confirm type_memo"), "{error}");
        assert_eq!(compensations(), strings(&["clear_memo", "clear_amount"]));
        let run = sole_run("Agent.teardown.phase2");
        assert_eq!(run.failed_step, "type_memo");
        assert_eq!(ran_steps(&run), strings(&["type_memo", "type_amount"]));
        drop(ops);
        fiber.dispose().unwrap();
        assert_eq!(compensations(), strings(&["clear_memo", "clear_amount"]));
    }

    #[test]
    fn failure_before_any_compensatable_step_runs_nothing() {
        let root = reset("locate:Amount");
        let fiber = root.plugin(agent(), ());
        fiber.try_wait().unwrap();
        let ops = root.require::<Box<dyn Ops>>("ops").unwrap();
        panics(|| { ops.run("r".to_string()); });
        assert_eq!(trace(), strings(&["observe", "locate:Amount"]));
        assert!(sole_run("Agent.teardown.phase2").ran.is_empty());
        drop(ops);
        fiber.dispose().unwrap();
        assert_eq!(compensations(), Vec::<String>::new());
    }

    #[test]
    fn failing_compensation_is_recorded_and_the_older_one_still_runs() {
        let root = reset(POSTCONDITION_UNMET);
        ui_desk(|d| d.comp_fail = true);
        let fiber = root.plugin(agent(), ());
        fiber.try_wait().unwrap();
        let ops = root.require::<Box<dyn Ops>>("ops").unwrap();
        // the substrate's panic propagates, not the compensation's
        let error = panics(|| { ops.run("r".to_string()); });
        assert!(error.contains("could not confirm locate:Approve"), "{error}");
        assert_eq!(compensations(), strings(&["clear_memo", "clear_amount"]));
        let run = sole_run("Agent.teardown.phase2");
        let failed: Vec<(String, bool)> = run.ran.iter().map(|e| (e.step.clone(), e.failed)).collect();
        assert_eq!(failed, vec![("type_memo".to_string(), true), ("type_amount".to_string(), false)]);
        drop(ops);
        fiber.dispose().unwrap();
    }

    #[test]
    fn later_abort_does_not_run_a_settled_unit_twice() {
        let root = reset(POSTCONDITION_UNMET);
        let fiber = root.plugin(agent(), ());
        fiber.try_wait().unwrap();
        let ops = root.require::<Box<dyn Ops>>("ops").unwrap();
        panics(|| { ops.run("r".to_string()); });
        assert_eq!(compensations(), strings(&["clear_memo", "clear_amount"]));
        ui_desk(|d| d.fail.clear());
        assert_eq!(ops.run("r".to_string()), 1);
        revl_abort("Agent.teardown.phase2");
        drop(ops);
        fiber.dispose().unwrap();
        assert_eq!(compensations(), strings(&[
            "clear_memo", "clear_amount",               // the failed call's unit
            "clear_note", "clear_memo", "clear_amount", // the abort, LIFO
        ]));
    }

    // a crossing wherever it is written

    #[test]
    fn tail_crossings_failure_is_the_units() {
        let root = reset("actuate");
        let fiber = root.plugin(tail_agent(), ());
        fiber.try_wait().unwrap();
        let tail = root.require::<Box<dyn TailOps>>("tail").unwrap();
        let error = panics(|| { tail.run("r".to_string()); });
        assert!(error.contains("could not confirm actuate"), "{error}");
        assert_eq!(trace(), strings(&["observe", "locate:Amount", "type_amount",
            "locate:Approve", "actuate", "compensate:clear_amount"]));
        let run = sole_run("TailAgent.teardown.phase2");
        assert_eq!(run.failed_step, "actuate");
        assert_eq!(ran_steps(&run), strings(&["type_amount"]));
        drop(tail);
        fiber.dispose().unwrap();
        assert_eq!(compensations(), strings(&["clear_amount"]));
    }

    // rust erases the async color, so an async method and async crossings are
    // the same unit
    #[test]
    fn async_method_is_one_unit() {
        let root = reset("actuate");
        let fiber = root.plugin(async_agent(), ());
        fiber.try_wait().unwrap();
        let ops = root.require::<Box<dyn AsyncOps>>("async_ops").unwrap();
        panics(|| { ops.run("r".to_string()); });
        assert_eq!(trace(), strings(&["locate:Amount", "type_amount", "locate:Approve",
            "actuate", "compensate:clear_amount"]));
        let run = sole_run("AsyncAgent.teardown.phase2");
        // the step is the extern the method crossed
        assert_eq!(run.failed_step, "actuate_async");
        assert_eq!(ran_steps(&run), strings(&["type_amount"]));
        drop(ops);
        fiber.dispose().unwrap();
        assert_eq!(compensations(), strings(&["clear_amount"]));
    }

    // the unit's other entries: Phase 1 replays the witnessed inverse before
    // Phase 2 runs any compensation; a non-computer-use extern's declared
    // compensation is the unit's too; a site-spelled compensation replaces
    // the computer-use extern's declared one
    #[test]
    fn mixed_unit_replays_witnessed_first_then_compensates_newest_first() {
        let root = reset("actuate");
        let fiber = root.plugin(mixed(), ());
        fiber.try_wait().unwrap();
        let ops = root.require::<Box<dyn MixedOps>>("mixed").unwrap();
        panics(|| { ops.run("r".to_string()); });
        let expected = strings(&["mark:a", "put:p", "locate:Amount", "type_amount", "actuate",
            "undo:a", "compensate:clear_note", "compensate:restore_row"]);
        assert_eq!(trace(), expected);
        let run = sole_run("Mixed.teardown.phase2");
        assert_eq!(run.unit, "mixed.run");
        assert_eq!(run.failed_step, "actuate");
        assert_eq!(run.replayed, strings(&["unmark"]));
        assert_eq!(run.ran, vec![
            RevlUiRan { step: "type_amount".into(), compensation: "clear_note".into(), failed: false },
            RevlUiRan { step: "".into(), compensation: "restore_row".into(), failed: false },
        ]);
        drop(ops);
        fiber.dispose().unwrap();
        assert_eq!(trace(), expected);
    }

    #[test]
    fn mixed_unit_that_completes_waits_for_the_verdict() {
        let root = reset("");
        let fiber = root.plugin(mixed(), ());
        fiber.try_wait().unwrap();
        let ops = root.require::<Box<dyn MixedOps>>("mixed").unwrap();
        ops.run("r".to_string());
        revl_abort("Mixed.teardown.phase2");
        drop(ops);
        fiber.dispose().unwrap();
        // the abort: the witnessed inverse replays in Phase 1, then the
        // compensations run newest first in Phase 2
        assert_eq!(trace(), strings(&["mark:a", "put:p", "locate:Amount", "type_amount", "actuate",
            "undo:a", "compensate:clear_note", "compensate:restore_row"]));
    }
}
"""


def _emit() -> str:
    return emit.emit(compile_source(SOURCE, "ui_transaction.rvl"))


def test_a_unit_method_runs_in_a_settling_scope():
    src = _emit()
    assert 'let _revl_ui = RevlUiScope::new("ops.run", true);' in src
    assert 'let _revl_ui = RevlUiScope::new("mixed.run", true);' in src
    assert "_revl_ui.close(&self.ctx, _revl_out)" in src
    # a computer-use crossing registers its declared compensation itself
    assert ('revl_ui_cross(&_revl_ui, "type_amount", Some(RevlUiComp { label: '
            '"Agent.run.compensate.2", compensation: "clear_amount"') in src
    # a site-spelled clause replaces the declared one
    assert ('revl_ui_cross(&_revl_ui, "type_amount", Some(RevlUiComp { label: '
            '"Mixed.run.compensate.3", compensation: "clear_note"') in src
    # the other entries are the scope's until the call returns
    assert '_revl_ui.witnessed("Mixed.run.witnessed", "unmark",' in src
    assert '_revl_ui.register("", Some(RevlUiComp { label: "Mixed.run.declared.compensate"' in src


def test_a_document_with_no_computer_use_extern_has_no_scope():
    src = emit.emit(compile_source(rust_tests._EXTERN_COMPENSATE_RVL, "extern_compensate.rvl"))
    assert "RevlUiScope" not in src
    assert "revl_ui_cross" not in src


@rust_tests.needs_cargo
def test_the_unit_settles_on_real_cordis_rs(tmp_path):
    """The runtime proof, against the real cordis-rs crate. One static desk
    is shared by every #[test] fn, so the crate runs single-threaded."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(_emit() + "\n" + HARNESS, encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_check"), encoding="utf-8")
    result = rust_tests._cargo("test", tmp_path, "--", "--test-threads=1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "11 passed" in result.stdout, result.stdout
