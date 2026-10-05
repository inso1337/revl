package uitransaction

// Issue #1369 (item 522 slice 3) on the go tier: the UI transaction unit. The
// mirror of tests/test_ui_transaction_runtime_1369.py (py) and
// backends/typescript/tests/ui_transaction.test.ts (ts), driving the EMITTED
// components (gen_ui_transaction_test.go) against the real stc-go runtime.
//
// A provide method that crosses a computer-use verb is one unit, the unit
// `revl.ui_transaction.method_plan` reads. If its call panics, the unit
// settles the entries this call registered, witnessed inverses first and
// compensations second, each newest first and continue-and-record, and the
// panic propagates unchanged. The entries never reach the frame, so the clean
// unload after the failed call does not discharge them and a later Abort()
// does not run them twice.
//
// Measured on the base with this scenario: a failed call ran no compensation
// at all. Each declared compensation of a crossing that returned stayed
// parked on the activation frame, and the clean unload after the failed call
// DISCHARGED them, so the transaction stopped half way and kept what it
// typed; a crossing that panicked registered nothing, and nothing recorded a
// run.
//
// Every host body records what it ran through the host trace (`observe`,
// `locate:<name>`, `type_amount`, `compensate:<fn>`), so the order is
// observed, not inferred. White-box (same package) so it reaches the
// activation frame via RevlFrames().

import (
	stdctx "context"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	stc "github.com/0xdenny218/stc-go"
)

// the fake desktop's failure switches: uiFail names the line and occurrence
// that panics ("locate:Approve#2" is the second resolution of Approve);
// uiCompFail makes clear_memo panic.
var (
	uiFail     string
	uiCompFail bool
	uiSeen     map[string]int
)

// uiStep is every fake-desktop host body: it records its line and panics on
// the configured occurrence.
func uiStep(line string) {
	hostRecord(line)
	uiSeen[line]++
	want, nth, _ := strings.Cut(uiFail, "#")
	if nth == "" {
		nth = "1"
	}
	if line == want && fmt.Sprint(uiSeen[line]) == nth {
		panic("the substrate could not confirm " + line)
	}
}

// uiCompensate is clear_memo's body: it records, then panics when uiCompFail.
func uiCompensate(line string) {
	hostRecord(line)
	if uiCompFail {
		panic("the memo field is gone")
	}
}

// postconditionUnmet is the read that checks `actuate`: the second resolution
// of Approve. Its failure is how the transaction learns the click did not take.
const postconditionUnmet = "locate:Approve#2"

// forwardToTheCheck is the forward crossings the five-step program makes
// before that read fails.
var forwardToTheCheck = []string{
	"observe", "locate:Amount", "type_amount",
	"observe", "locate:Memo", "type_memo",
	"observe", "locate:Approve", "actuate",
	"observe", "locate:Approve",
}

func reset(fail string) *stc.Context {
	HostReset()
	RevlResetFrames()
	uiFail = fail
	uiCompFail = false
	uiSeen = map[string]int{}
	return stc.New()
}

func waitSettled(f *stc.Fiber) {
	for i := 0; i < 400 && f.State() != stc.StateGone && f.State() != stc.StateFailed; i++ {
		time.Sleep(5 * time.Millisecond)
	}
}

func activate(t *testing.T, root *stc.Context, plug func(*stc.Context) *stc.Fiber) (*stc.Fiber, *RevlFrame) {
	t.Helper()
	f := plug(root)
	if err := f.Ready(stdctx.Background()); err != nil {
		t.Fatalf("load: %v", err)
	}
	frames := RevlFrames()
	if len(frames) != 1 {
		t.Fatalf("want exactly one activation frame, got %d", len(frames))
	}
	return f, frames[0]
}

func unload(f *stc.Fiber) {
	f.Dispose()
	waitSettled(f)
}

// panics runs call and returns what it panicked with, or nil.
func panics(call func()) (r any) {
	defer func() { r = recover() }()
	call()
	return nil
}

func mustPanic(t *testing.T, call func(), contains string) {
	t.Helper()
	r := panics(call)
	if r == nil {
		t.Fatalf("the call returned; want a panic containing %q", contains)
	}
	if !strings.Contains(fmt.Sprint(r), contains) {
		t.Fatalf("the call panicked with %q; want it to contain %q", fmt.Sprint(r), contains)
	}
}

func trace() []string {
	return HostMarks()
}

func compensations() []string {
	out := []string{}
	for _, m := range HostMarks() {
		if rest, ok := strings.CutPrefix(m, "compensate:"); ok {
			out = append(out, rest)
		}
	}
	return out
}

func want(t *testing.T, what string, got, expected any) {
	t.Helper()
	if !reflect.DeepEqual(got, expected) {
		t.Fatalf("%s: got %v, want %v", what, got, expected)
	}
}

func soleRun(t *testing.T, frame *RevlFrame) RevlUiRun {
	t.Helper()
	runs := frame.UiTransactionRuns()
	if len(runs) != 1 {
		t.Fatalf("want exactly one settled unit, got %d", len(runs))
	}
	return runs[0]
}

func ranSteps(run RevlUiRun) []string {
	out := []string{}
	for _, e := range run.Ran {
		out = append(out, e.Step)
	}
	return out
}

func ranCompensations(run RevlUiRun) []string {
	out := []string{}
	for _, e := range run.Ran {
		out = append(out, e.Compensation)
	}
	return out
}

func ops(t *testing.T, root *stc.Context) Ops {
	t.Helper()
	o, err := stc.Service[Ops](root, _keyOps)
	if err != nil {
		t.Fatalf("resolve ops: %v", err)
	}
	return o
}

// a failed call settles its unit, then the clean unload runs nothing more

func TestFiveStepOracleCompensatesStepsTwoAndOneInOrder(t *testing.T) {
	root := reset(postconditionUnmet)
	f, frame := activate(t, root, LoadAgent)
	mustPanic(t, func() { ops(t, root).Run("r") }, "could not confirm locate:Approve")
	// performed at the failure, not at the unload
	want(t, "host trace", trace(), append(append([]string{}, forwardToTheCheck...),
		"compensate:clear_memo", "compensate:clear_amount"))
	run := soleRun(t, frame)
	want(t, "unit", run.Unit, "ops.run")
	want(t, "failed step", run.FailedStep, "locate")
	want(t, "ran steps", ranSteps(run), []string{"type_memo", "type_amount"})
	want(t, "ran compensations", ranCompensations(run), []string{"clear_memo", "clear_amount"})
	want(t, "last crossing", run.Crossed[len(run.Crossed)-1], "locate")
	for _, later := range []string{"type_note", "fetch_receipt"} {
		for _, c := range run.Crossed {
			if c == later {
				t.Fatalf("crossed %v includes %s, which the call never reached", run.Crossed, later)
			}
		}
	}
	want(t, "residue", len(run.Residue), 0)
	// the clean unload after the failed call: nothing is discharged twice and
	// nothing runs again
	unload(f)
	want(t, "compensations after unload", compensations(), []string{"clear_memo", "clear_amount"})
	want(t, "frame residue", len(frame.Residue()), 0)
}

func TestCompletedCallRunsNoCompensationAndUnloadDischarges(t *testing.T) {
	root := reset("")
	f, frame := activate(t, root, LoadAgent)
	want(t, "result", ops(t, root).Run("r"), int64(1))
	want(t, "compensations", compensations(), []string{})
	want(t, "runs", len(frame.UiTransactionRuns()), 0)
	unload(f)
	want(t, "compensations after unload", compensations(), []string{})
}

func TestCompletedCallsEntriesWaitForTheAbort(t *testing.T) {
	root := reset("")
	f, frame := activate(t, root, LoadAgent)
	ops(t, root).Run("r")
	frame.Abort()
	unload(f)
	want(t, "compensations", compensations(), []string{"clear_note", "clear_memo", "clear_amount"})
}

// the rules the run keeps

func TestFailingCrossingsOwnCompensationRunsFirst(t *testing.T) {
	root := reset("type_memo")
	f, frame := activate(t, root, LoadAgent)
	mustPanic(t, func() { ops(t, root).Run("r") }, "could not confirm type_memo")
	want(t, "compensations", compensations(), []string{"clear_memo", "clear_amount"})
	run := soleRun(t, frame)
	want(t, "failed step", run.FailedStep, "type_memo")
	want(t, "ran steps", ranSteps(run), []string{"type_memo", "type_amount"})
	unload(f)
	want(t, "compensations after unload", compensations(), []string{"clear_memo", "clear_amount"})
}

func TestFailureBeforeAnyCompensatableStepRunsNothing(t *testing.T) {
	root := reset("locate:Amount")
	f, frame := activate(t, root, LoadAgent)
	mustPanic(t, func() { ops(t, root).Run("r") }, "could not confirm locate:Amount")
	want(t, "host trace", trace(), []string{"observe", "locate:Amount"})
	want(t, "ran", len(soleRun(t, frame).Ran), 0)
	unload(f)
	want(t, "compensations after unload", compensations(), []string{})
}

func TestFailingCompensationIsResidueAndTheOlderOneStillRuns(t *testing.T) {
	root := reset(postconditionUnmet)
	uiCompFail = true
	f, frame := activate(t, root, LoadAgent)
	// the substrate's panic propagates, not the compensation's
	mustPanic(t, func() { ops(t, root).Run("r") }, "could not confirm locate:Approve")
	want(t, "compensations", compensations(), []string{"clear_memo", "clear_amount"})
	run := soleRun(t, frame)
	want(t, "ran", run.Ran, []RevlUiRan{
		{Step: "type_memo", Compensation: "clear_memo", Failed: true},
		{Step: "type_amount", Compensation: "clear_amount", Failed: false},
	})
	if len(run.Residue) != 1 {
		t.Fatalf("want one residue record, got %v", run.Residue)
	}
	residue := run.Residue[0]
	want(t, "residue kind", residue.Kind, "compensation-residue")
	want(t, "residue outcome", residue.Outcome, "failed")
	if !strings.Contains(residue.ErrorMessage, "the memo field is gone") {
		t.Fatalf("residue error %q does not carry the compensation's failure", residue.ErrorMessage)
	}
	want(t, "frame residue", frame.Residue(), run.Residue)
	unload(f)
}

func TestLaterAbortDoesNotRunASettledUnitTwice(t *testing.T) {
	root := reset(postconditionUnmet)
	f, frame := activate(t, root, LoadAgent)
	mustPanic(t, func() { ops(t, root).Run("r") }, "could not confirm")
	want(t, "compensations", compensations(), []string{"clear_memo", "clear_amount"})
	uiFail = ""
	want(t, "result", ops(t, root).Run("r"), int64(1))
	frame.Abort()
	unload(f)
	want(t, "compensations", compensations(), []string{
		"clear_memo", "clear_amount", // the failed call's unit
		"clear_note", "clear_memo", "clear_amount", // the abort, LIFO
	})
}

// a crossing wherever it is written

func TestTailCrossingsFailureIsTheUnits(t *testing.T) {
	root := reset("actuate")
	f, frame := activate(t, root, LoadTailAgent)
	tail, err := stc.Service[TailOps](root, _keyTail)
	if err != nil {
		t.Fatalf("resolve tail: %v", err)
	}
	mustPanic(t, func() { tail.Run("r") }, "could not confirm actuate")
	want(t, "host trace", trace(), []string{"observe", "locate:Amount", "type_amount",
		"locate:Approve", "actuate", "compensate:clear_amount"})
	run := soleRun(t, frame)
	want(t, "failed step", run.FailedStep, "actuate")
	want(t, "ran steps", ranSteps(run), []string{"type_amount"})
	unload(f)
	want(t, "compensations after unload", compensations(), []string{"clear_amount"})
}

// go erases the async color, so an async method and async crossings are the
// same unit
func TestAsyncMethodIsOneUnit(t *testing.T) {
	root := reset("actuate")
	f, frame := activate(t, root, LoadAsyncAgent)
	async, err := stc.Service[AsyncOps](root, _keyAsyncOps)
	if err != nil {
		t.Fatalf("resolve async_ops: %v", err)
	}
	mustPanic(t, func() { async.Run("r") }, "could not confirm actuate")
	want(t, "host trace", trace(), []string{"locate:Amount", "type_amount", "locate:Approve",
		"actuate", "compensate:clear_amount"})
	run := soleRun(t, frame)
	// the step is the extern the method crossed
	want(t, "failed step", run.FailedStep, "actuate_async")
	want(t, "ran steps", ranSteps(run), []string{"type_amount"})
	unload(f)
	want(t, "compensations after unload", compensations(), []string{"clear_amount"})
}

// the unit's other entries: Phase 1 replays the witnessed inverse before
// Phase 2 runs any compensation; a non-computer-use extern's declared
// compensation is the unit's too; a site-spelled compensation replaces the
// computer-use extern's declared one
func TestMixedUnitReplaysWitnessedFirstThenCompensatesNewestFirst(t *testing.T) {
	root := reset("actuate")
	f, frame := activate(t, root, LoadMixed)
	mixed, err := stc.Service[MixedOps](root, _keyMixed)
	if err != nil {
		t.Fatalf("resolve mixed: %v", err)
	}
	mustPanic(t, func() { mixed.Run("r") }, "could not confirm actuate")
	want(t, "host trace", trace(), []string{"mark:a", "put:p", "locate:Amount", "type_amount", "actuate",
		"undo:a", "compensate:clear_note", "compensate:restore_row"})
	run := soleRun(t, frame)
	want(t, "unit", run.Unit, "mixed.run")
	want(t, "failed step", run.FailedStep, "actuate")
	want(t, "replayed", run.Replayed, []string{"unmark"})
	want(t, "ran", run.Ran, []RevlUiRan{
		{Step: "type_amount", Compensation: "clear_note"},
		{Step: "", Compensation: "restore_row"},
	})
	unload(f)
	want(t, "host trace after unload", len(trace()), 8)
}

func TestMixedUnitThatCompletesWaitsForTheVerdict(t *testing.T) {
	root := reset("")
	f, frame := activate(t, root, LoadMixed)
	mixed, err := stc.Service[MixedOps](root, _keyMixed)
	if err != nil {
		t.Fatalf("resolve mixed: %v", err)
	}
	mixed.Run("r")
	frame.Abort()
	unload(f)
	// the abort: the witnessed inverse replays in Phase 1, then the
	// compensations run newest first in Phase 2
	want(t, "host trace", trace(), []string{"mark:a", "put:p", "locate:Amount", "type_amount", "actuate",
		"undo:a", "compensate:clear_note", "compensate:restore_row"})
}
