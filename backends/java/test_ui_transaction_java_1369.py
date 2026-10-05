"""Issue #1369 (item 522 slice 3) on the java tier: the UI transaction unit.

The java mirror of tests/test_ui_transaction_runtime_1369.py (py),
backends/typescript/tests/ui_transaction.test.ts (ts), the go
`ui_transaction` scenario and backends/rust/test_ui_transaction_rust_1369.py.

A provide method that crosses a computer-use verb is one unit, the unit
`revl.ui_transaction.method_plan` reads. If its call throws, the unit settles
the entries this call registered, witnessed inverses first and compensations
second, each newest first and continue-and-record, and the failure propagates
unchanged. The entries never reach the activation's `fx`, so the clean unload
after the failed call does not discharge them and a later `abort()` does not
run them twice.

Measured on the base with this program and these checks (the record types
shimmed so the checks compile): 8 of the 11 checks fail and the 3 controls
pass. A failed call ran no compensation at all. Each declared compensation of
a crossing that returned stayed tracked on the activation and the clean unload
after the failed call discharged it, a crossing that threw registered nothing,
and nothing recorded a run.

Every host body goes through `UiDesk.step` (or `UiDesk.compensate` for
`clear_memo`): it records its line and throws on the occurrence a check
configured, so a check picks the failure without a second program. The source
lives here rather than in backends/java/scenarios/ so it does not join the
scoring corpora. Without a JDK the runtime test skips, and a skip is not a
pass: CI's `backend-java` job provisions one.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "tests") not in sys.path:
    sys.path.append(str(ROOT / "tests"))

from _load_by_path import load_by_path  # noqa: E402

javac_gate = load_by_path("javac_gate", HERE / "javac_gate.py")
emit = load_by_path("revl_java_emit_ui_1369", HERE / "emit.py").emit

from revl.compiler import compile_source  # noqa: E402

needs_jdk = pytest.mark.skipif(javac_gate.JAVAC is None, reason=javac_gate.NO_JDK)

SOURCE = r"""type UiTarget = {
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

extern pure fn unmark(w: Mark) -> Unit = @java {
    UiDesk.step("undo:" + w.line);
    return;
}

extern witnessed[fs] fn mark(line: Str) -> Result[Mark, MarkError] undo unmark(result) = @java {
    UiDesk.step("mark:" + line);
    return new RevlResult.Ok<>(new Mark(line));
}

extern pure fn restore_row() -> Unit = @java {
    UiDesk.step("compensate:restore_row");
    return;
}

extern emission fn put_row(body: Str) -> Int compensate restore_row() = @java {
    UiDesk.step("put:" + body);
    return 1L;
}

extern emission[screen.observe] fn read_pane(region: Str) -> Str = @java {
    UiDesk.step("observe");
    return "pane";
}

extern emission[ui.find] fn locate(pane: Str, name: Str) -> UiTarget = @java {
    UiDesk.step("locate:" + name);
    return new UiTarget("Billing", "", "", name, "", "", "", "", 0L, false);
}

extern pure fn clear_amount() -> Unit = @java {
    UiDesk.step("compensate:clear_amount");
    return;
}

extern pure fn clear_memo() -> Unit = @java {
    UiDesk.compensate("compensate:clear_memo");
    return;
}

extern pure fn clear_note() -> Unit = @java {
    UiDesk.step("compensate:clear_note");
    return;
}

extern emission[ui.text] fn type_amount(target: UiTarget, s: Str)
  compensate clear_amount()
  = @java {
    UiDesk.step("type_amount");
    return;
}

extern emission[ui.text] fn type_memo(target: UiTarget, s: Str)
  compensate clear_memo()
  = @java {
    UiDesk.step("type_memo");
    return;
}

extern emission[ui.click] fn actuate(target: UiTarget) = @java {
    UiDesk.step("actuate");
    return;
}

extern emission[ui.text] fn type_note(target: UiTarget, s: Str)
  compensate clear_note()
  = @java {
    UiDesk.step("type_note");
    return;
}

extern emission[ui.download] fn fetch_receipt(target: UiTarget) -> Str = @java {
    UiDesk.step("fetch_receipt");
    return "receipt";
}

extern emission[ui.find] async fn locate_async(pane: Str, name: Str) -> UiTarget = @java {
    UiDesk.step("locate:" + name);
    return new UiTarget("Billing", "", "", name, "", "", "", "", 0L, false);
}

extern emission[ui.click] async fn actuate_async(target: UiTarget) = @java {
    UiDesk.step("actuate");
    return;
}

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

UI_DESK = r"""package revl;

/** The fake desktop: every host body records its line, and throws on the
 * occurrence a check configured ("locate:Approve#2" is the second resolution
 * of Approve); clear_memo throws when compFail is set. */
public final class UiDesk {
    static final java.util.List<String> LOG = new java.util.ArrayList<>();
    static final java.util.Map<String, Integer> SEEN = new java.util.HashMap<>();
    static String fail = "";
    static boolean compFail = false;

    private UiDesk() {}

    static void reset(String failing) {
        LOG.clear();
        SEEN.clear();
        fail = failing;
        compFail = false;
    }

    public static void step(String line) {
        LOG.add(line);
        int n = SEEN.merge(line, 1, Integer::sum);
        String want = fail;
        int nth = 1;
        int hash = fail.indexOf('#');
        if (hash >= 0) {
            want = fail.substring(0, hash);
            nth = Integer.parseInt(fail.substring(hash + 1));
        }
        if (line.equals(want) && n == nth) {
            throw new RuntimeException("the substrate could not confirm " + line);
        }
    }

    public static void compensate(String line) {
        LOG.add(line);
        if (compFail) {
            throw new RuntimeException("the memo field is gone");
        }
    }
}
"""

UI_CHECKS = r"""package revl;

import io.cordis4j.core.Context;
import io.cordis4j.core.Disposable;
import java.util.List;

/** Issue #1369 on the java tier: the UI transaction unit. Each check prints
 * PASS or FAIL with its name; the process exits non-zero on any FAIL. */
public final class UiChecks {
    private static final String POSTCONDITION_UNMET = "locate:Approve#2";
    private static final List<String> FORWARD_TO_THE_CHECK = List.of(
            "observe", "locate:Amount", "type_amount",
            "observe", "locate:Memo", "type_memo",
            "observe", "locate:Approve", "actuate",
            "observe", "locate:Approve");
    private static int failures = 0;

    private UiChecks() {}

    private static void want(String what, Object got, Object expected) {
        if (!java.util.Objects.equals(got, expected)) {
            throw new AssertionError(what + ": got " + got + ", want " + expected);
        }
    }

    private static List<String> compensations() {
        return UiDesk.LOG.stream().filter(l -> l.startsWith("compensate:"))
                .map(l -> l.substring("compensate:".length())).toList();
    }

    private static String throwing(Runnable call) {
        try {
            call.run();
        } catch (RuntimeException failure) {
            return failure.getMessage();
        }
        throw new AssertionError("the call returned; want a throw");
    }

    private static Components.RevlUiRun soleRun(Disposable activation) {
        List<Components.RevlUiRun> runs = Components.RevlUi.runs(activation);
        want("settled units", runs.size(), 1);
        return runs.get(0);
    }

    private static List<String> ranSteps(Components.RevlUiRun run) {
        return run.ran().stream().map(Components.RevlUiRan::step).toList();
    }

    private static void check(String name, Runnable body) {
        try {
            body.run();
            System.out.println("PASS " + name);
        } catch (Throwable failure) {
            failures++;
            System.out.println("FAIL " + name + ": " + failure.getMessage());
        }
    }

    private static List<String> concat(List<String> a, String... b) {
        java.util.List<String> out = new java.util.ArrayList<>(a);
        out.addAll(List.of(b));
        return out;
    }

    public static void main(String[] args) {
        // a failed call settles its unit, then the clean unload runs nothing more
        check("five_step_oracle", () -> {
            UiDesk.reset(POSTCONDITION_UNMET);
            Context root = new Context();
            Disposable activation = new Components.AgentPlugin().apply(root);
            Components.Ops ops = root.get(Components.Ops.class);
            String error = throwing(() -> ops.run("r"));
            want("error", error.contains("could not confirm locate:Approve"), true);
            // performed at the failure, not at the unload
            want("trace", UiDesk.LOG, concat(FORWARD_TO_THE_CHECK,
                    "compensate:clear_memo", "compensate:clear_amount"));
            Components.RevlUiRun run = soleRun(activation);
            want("unit", run.unit(), "ops.run");
            want("failed step", run.failedStep(), "locate");
            want("ran steps", ranSteps(run), List.of("type_memo", "type_amount"));
            want("ran compensations", run.ran().stream().map(Components.RevlUiRan::compensation).toList(),
                    List.of("clear_memo", "clear_amount"));
            want("last crossing", run.crossed().get(run.crossed().size() - 1), "locate");
            want("type_note crossed", run.crossed().contains("type_note"), false);
            want("fetch_receipt crossed", run.crossed().contains("fetch_receipt"), false);
            want("residue", run.residue(), List.of());
            // the clean unload after the failed call: nothing runs again
            activation.dispose();
            want("compensations after unload", compensations(), List.of("clear_memo", "clear_amount"));
        });
        check("completed_call_runs_no_compensation_and_unload_discharges", () -> {
            UiDesk.reset("");
            Context root = new Context();
            Disposable activation = new Components.AgentPlugin().apply(root);
            Components.Ops ops = root.get(Components.Ops.class);
            want("result", ops.run("r"), 1L);
            want("compensations", compensations(), List.of());
            want("runs", Components.RevlUi.runs(activation), List.of());
            activation.dispose();
            want("compensations after unload", compensations(), List.of());
        });
        check("completed_calls_entries_wait_for_the_abort", () -> {
            UiDesk.reset("");
            Context root = new Context();
            Disposable activation = new Components.AgentPlugin().apply(root);
            root.get(Components.Ops.class).run("r");
            ((Components.RevlActivation) activation).abort();
            activation.dispose();
            want("compensations", compensations(), List.of("clear_note", "clear_memo", "clear_amount"));
        });
        // the rules the run keeps
        check("failing_crossings_own_compensation_runs_first", () -> {
            UiDesk.reset("type_memo");
            Context root = new Context();
            Disposable activation = new Components.AgentPlugin().apply(root);
            Components.Ops ops = root.get(Components.Ops.class);
            String error = throwing(() -> ops.run("r"));
            want("error", error.contains("could not confirm type_memo"), true);
            want("compensations", compensations(), List.of("clear_memo", "clear_amount"));
            Components.RevlUiRun run = soleRun(activation);
            want("failed step", run.failedStep(), "type_memo");
            want("ran steps", ranSteps(run), List.of("type_memo", "type_amount"));
            activation.dispose();
            want("compensations after unload", compensations(), List.of("clear_memo", "clear_amount"));
        });
        check("failure_before_any_compensatable_step_runs_nothing", () -> {
            UiDesk.reset("locate:Amount");
            Context root = new Context();
            Disposable activation = new Components.AgentPlugin().apply(root);
            Components.Ops ops = root.get(Components.Ops.class);
            throwing(() -> ops.run("r"));
            want("trace", UiDesk.LOG, List.of("observe", "locate:Amount"));
            want("ran", soleRun(activation).ran(), List.of());
            activation.dispose();
            want("compensations after unload", compensations(), List.of());
        });
        check("failing_compensation_is_residue_and_the_older_one_still_runs", () -> {
            UiDesk.reset(POSTCONDITION_UNMET);
            UiDesk.compFail = true;
            Context root = new Context();
            Disposable activation = new Components.AgentPlugin().apply(root);
            Components.Ops ops = root.get(Components.Ops.class);
            // the substrate's failure propagates, not the compensation's
            String error = throwing(() -> ops.run("r"));
            want("error", error.contains("could not confirm locate:Approve"), true);
            want("compensations", compensations(), List.of("clear_memo", "clear_amount"));
            Components.RevlUiRun run = soleRun(activation);
            want("ran", run.ran(), List.of(
                    new Components.RevlUiRan("type_memo", "clear_memo", true),
                    new Components.RevlUiRan("type_amount", "clear_amount", false)));
            want("residue records", run.residue().size(), 1);
            Components.RevlUiResidue residue = run.residue().get(0);
            want("residue kind", residue.kind(), "compensation-residue");
            want("residue outcome", residue.outcome(), "failed");
            want("residue error", residue.error().contains("the memo field is gone"), true);
            activation.dispose();
        });
        check("later_abort_does_not_run_a_settled_unit_twice", () -> {
            UiDesk.reset(POSTCONDITION_UNMET);
            Context root = new Context();
            Disposable activation = new Components.AgentPlugin().apply(root);
            Components.Ops ops = root.get(Components.Ops.class);
            throwing(() -> ops.run("r"));
            want("compensations", compensations(), List.of("clear_memo", "clear_amount"));
            UiDesk.fail = "";
            want("result", ops.run("r"), 1L);
            ((Components.RevlActivation) activation).abort();
            activation.dispose();
            want("compensations", compensations(), List.of(
                    "clear_memo", "clear_amount",
                    "clear_note", "clear_memo", "clear_amount"));
        });
        // a crossing wherever it is written
        check("tail_crossings_failure_is_the_units", () -> {
            UiDesk.reset("actuate");
            Context root = new Context();
            Disposable activation = new Components.TailAgentPlugin().apply(root);
            Components.TailOps tail = root.get(Components.TailOps.class);
            String error = throwing(() -> tail.run("r"));
            want("error", error.contains("could not confirm actuate"), true);
            want("trace", UiDesk.LOG, List.of("observe", "locate:Amount", "type_amount",
                    "locate:Approve", "actuate", "compensate:clear_amount"));
            Components.RevlUiRun run = soleRun(activation);
            want("failed step", run.failedStep(), "actuate");
            want("ran steps", ranSteps(run), List.of("type_amount"));
            activation.dispose();
            want("compensations after unload", compensations(), List.of("clear_amount"));
        });
        // java erases the async color, so an async method and async crossings
        // are the same unit
        check("async_method_is_one_unit", () -> {
            UiDesk.reset("actuate");
            Context root = new Context();
            Disposable activation = new Components.AsyncAgentPlugin().apply(root);
            Components.AsyncOps ops = root.get(Components.AsyncOps.class);
            throwing(() -> ops.run("r"));
            want("trace", UiDesk.LOG, List.of("locate:Amount", "type_amount", "locate:Approve",
                    "actuate", "compensate:clear_amount"));
            Components.RevlUiRun run = soleRun(activation);
            // the step is the extern the method crossed
            want("failed step", run.failedStep(), "actuate_async");
            want("ran steps", ranSteps(run), List.of("type_amount"));
            activation.dispose();
            want("compensations after unload", compensations(), List.of("clear_amount"));
        });
        // the unit's other entries: Phase 1 replays the witnessed inverse
        // before Phase 2 runs any compensation; a non-computer-use extern's
        // declared compensation is the unit's too; a site-spelled compensation
        // replaces the computer-use extern's declared one
        List<String> mixed = List.of("mark:a", "put:p", "locate:Amount", "type_amount", "actuate",
                "undo:a", "compensate:clear_note", "compensate:restore_row");
        check("mixed_unit_replays_witnessed_first_then_compensates_newest_first", () -> {
            UiDesk.reset("actuate");
            Context root = new Context();
            Disposable activation = new Components.MixedPlugin().apply(root);
            Components.MixedOps ops = root.get(Components.MixedOps.class);
            throwing(() -> ops.run("r"));
            want("trace", UiDesk.LOG, mixed);
            Components.RevlUiRun run = soleRun(activation);
            want("unit", run.unit(), "mixed.run");
            want("failed step", run.failedStep(), "actuate");
            want("replayed", run.replayed(), List.of("unmark"));
            want("ran", run.ran(), List.of(
                    new Components.RevlUiRan("type_amount", "clear_note", false),
                    new Components.RevlUiRan("", "restore_row", false)));
            activation.dispose();
            want("trace after unload", UiDesk.LOG, mixed);
        });
        check("mixed_unit_that_completes_waits_for_the_verdict", () -> {
            UiDesk.reset("");
            Context root = new Context();
            Disposable activation = new Components.MixedPlugin().apply(root);
            root.get(Components.MixedOps.class).run("r");
            ((Components.RevlActivation) activation).abort();
            activation.dispose();
            // the abort: the witnessed inverse replays in Phase 1, then the
            // compensations run newest first in Phase 2
            want("trace", UiDesk.LOG, mixed);
        });
        System.out.println("CHECKS " + (11 - failures) + " passed, " + failures + " failed");
        System.exit(failures == 0 ? 0 : 1);
    }
}
"""


def _emit() -> str:
    return emit(compile_source(SOURCE, "ui_transaction.rvl"))


def test_a_unit_method_runs_in_a_settling_scope():
    code = _emit()
    assert 'RevlUi _revlUi = new RevlUi(fx, frame, "ops.run", true);' in code
    assert "_revlUi.fail(_revlFailure);" in code and "_revlUi.flush();" in code
    # a computer-use crossing registers its declared compensation itself
    assert ('_revlUi.crossUnit("type_amount", "clear_amount", () -> clear_amount(), '
            '() -> type_amount(amount, "10"));') in code
    # a site-spelled clause replaces the declared one
    assert ('_revlUi.crossUnit("type_amount", "clear_note", () -> clear_note(), '
            '() -> type_amount(target, "10"));') in code
    # the other entries are the scope's until the call returns
    assert '_revlUi.transactional("mark", "unmark", () -> unmark(result));' in code
    assert ('_revlUi.declared("put_row", "restore_row", put_row("p"), '
            '() -> restore_row());') in code


def test_a_document_with_no_computer_use_extern_has_no_scope():
    code = emit(compile_source(
        "extern pure fn undo_put() -> Unit = @java { return; }\n"
        "extern emission fn put(k: Str) -> Int compensate undo_put() = @java { return 1L; }\n"
        "service Ops { emission fn run() -> Int }\n"
        "component Agent provides ops: Ops {\n"
        "  provide ops {\n    fn run() {\n      emit put(\"k\")\n      return 0\n    }\n  }\n}\n",
        "plain.rvl"))
    assert "RevlUi" not in code


@needs_jdk
def test_the_unit_settles_on_a_jvm(tmp_path):
    """The runtime proof: the emitted unit, the fake desktop and the checks,
    compiled against the in-repo cordis4j stubs and run on a JVM."""
    pkg = tmp_path / "revl"
    pkg.mkdir()
    (pkg / "Components.java").write_text(_emit(), encoding="utf-8")
    (pkg / "UiDesk.java").write_text(UI_DESK, encoding="utf-8")
    (pkg / "UiChecks.java").write_text(UI_CHECKS, encoding="utf-8")
    classes = tmp_path / "classes"
    classes.mkdir()
    built = subprocess.run(
        [javac_gate.JAVAC, "--release", javac_gate.RELEASE, "-d", str(classes)]
        + [str(s) for s in javac_gate.STUB_SOURCES]
        + [str(pkg / name) for name in ("Components.java", "UiDesk.java", "UiChecks.java")],
        capture_output=True, text=True, timeout=600)
    assert built.returncode == 0, built.stderr
    ran = subprocess.run(
        [javac_gate.JAVA, "-cp", str(classes), "revl.UiChecks"],
        capture_output=True, text=True, timeout=600)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "CHECKS 11 passed, 0 failed" in ran.stdout, ran.stdout
