package declaredcompensate

// Extern-declared compensations on the go tier (issue #1511), the go mirror of
// tests/test_declared_compensate_positions.py. It drives the EMITTED
// components (gen_declared_compensate_test.go, from
// scenarios/declared_compensate.rvl) against the real stc-go runtime:
//
//   * a provide-method crossing of an extern that declares `compensate`, in
//     each position (statement, let, return, argument, if arm, nested, and a
//     Unit extern), owes that compensation: an abort runs it, once;
//   * a clean unload commits and runs none of them;
//   * one call crossing every position, with a site-spelled compensation among
//     them and a declared crossing in the activation body, runs them newest
//     first on abort, whichever way each was registered;
//   * a crossing that panics registers nothing, and the earlier one still runs.
//
// `Agent`'s activation carries a site-spelled compensation (`site`), which
// runs last on every abort. On the base it was the only one that ran.

import (
	stdctx "context"
	"reflect"
	"strings"
	"testing"
	"time"

	stc "github.com/0xdenny218/stc-go"
)

func waitGoneDC(f *stc.Fiber) {
	for i := 0; i < 400 && f.State() != stc.StateGone && f.State() != stc.StateFailed; i++ {
		time.Sleep(5 * time.Millisecond)
	}
}

func soleFrameDC(t *testing.T) *RevlFrame {
	t.Helper()
	frames := RevlFrames()
	if len(frames) != 1 {
		t.Fatalf("want exactly one activation frame, got %d", len(frames))
	}
	return frames[0]
}

// compensations is the ordered list of compensations the host trace recorded.
func compensations() []string {
	out := []string{}
	for _, mark := range HostMarks() {
		if strings.HasPrefix(mark, "comp:") {
			out = append(out, strings.TrimPrefix(mark, "comp:"))
		}
	}
	return out
}

func loadAgentDC(t *testing.T) (*stc.Context, *stc.Fiber, Ops) {
	t.Helper()
	HostReset()
	RevlResetFrames()
	root := stc.New()
	f := LoadAgent(root)
	if err := f.Ready(stdctx.Background()); err != nil {
		t.Fatalf("load Agent: %v", err)
	}
	ops, err := stc.Service[Ops](root, _keyOps)
	if err != nil {
		t.Fatalf("resolve ops: %v", err)
	}
	return root, f, ops
}

var positions = []struct {
	tag  string
	call func(Ops) int64
}{
	{"statement", func(o Ops) int64 { return o.RunStatement() }},
	{"let", func(o Ops) int64 { return o.RunLet() }},
	{"return", func(o Ops) int64 { return o.RunReturn() }},
	{"argument", func(o Ops) int64 { return o.RunArgument() }},
	{"ifarm", func(o Ops) int64 { return o.RunIfarm(true) }},
	{"nested", func(o Ops) int64 { return o.RunNested() }},
	{"unit", func(o Ops) int64 { return o.RunUnit() }},
}

func TestAnAbortRunsTheDeclaredCompensationInEveryPosition(t *testing.T) {
	for _, pos := range positions {
		t.Run(pos.tag, func(t *testing.T) {
			root, f, ops := loadAgentDC(t)
			frame := soleFrameDC(t)
			pos.call(ops)
			if got := compensations(); len(got) != 0 {
				t.Fatalf("owed, not run: nothing failed yet, but %v ran", got)
			}
			frame.Abort()
			f.Dispose()
			waitGoneDC(f)
			if got, want := compensations(), []string{pos.tag, "site"}; !reflect.DeepEqual(got, want) {
				t.Fatalf("abort after the %s crossing ran %v, want %v", pos.tag, got, want)
			}
			if got := frame.Residue(); len(got) != 0 {
				t.Fatalf("abort surfaced residue: %v", got)
			}
			if got := len(root.Fibers()); got != 0 {
				t.Fatalf("residue: %d fiber(s) still registered", got)
			}
		})
	}
}

func TestACleanCommitDischargesTheDeclaredCompensation(t *testing.T) {
	for _, pos := range positions {
		t.Run(pos.tag, func(t *testing.T) {
			_, f, ops := loadAgentDC(t)
			frame := soleFrameDC(t)
			pos.call(ops)
			if got := len(frame.deferred); got != 1 {
				t.Fatalf("want the one declared entry parked after the call, got %d", got)
			}
			f.Dispose() // clean unload == implicit commit
			waitGoneDC(f)
			if got := compensations(); len(got) != 0 {
				t.Fatalf("a clean commit ran %v; the emission was the deliverable", got)
			}
		})
	}
}

func TestAFalseIfArmCrossesNothingAndOwesNothing(t *testing.T) {
	_, f, ops := loadAgentDC(t)
	frame := soleFrameDC(t)
	ops.RunIfarm(false)
	frame.Abort()
	f.Dispose()
	waitGoneDC(f)
	if got, want := compensations(), []string{"site"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("an arm that never ran owed more than the activation's: %v", got)
	}
}

func TestTheAbortRunsEveryCompensationNewestFirst(t *testing.T) {
	HostReset()
	RevlResetFrames()
	root := stc.New()
	f := LoadFull(root)
	if err := f.Ready(stdctx.Background()); err != nil {
		t.Fatalf("load Full: %v", err)
	}
	every, err := stc.Service[Every](root, _keyAll)
	if err != nil {
		t.Fatalf("resolve all: %v", err)
	}
	frame := soleFrameDC(t)
	every.Run()
	frame.Abort()
	f.Dispose()
	waitGoneDC(f)
	want := []string{"return", "nested", "site", "ifarm", "argument", "let", "statement", "act"}
	if got := compensations(); !reflect.DeepEqual(got, want) {
		t.Fatalf("abort order %v, want %v", got, want)
	}
	if got := frame.Residue(); len(got) != 0 {
		t.Fatalf("abort surfaced residue: %v", got)
	}
}

func TestACrossingThatPanicsRegistersNothing(t *testing.T) {
	_, f, ops := loadAgentDC(t)
	frame := soleFrameDC(t)
	func() {
		defer func() {
			if r := recover(); r == nil {
				t.Fatalf("put_boom did not panic")
			}
		}()
		ops.RunBoom()
	}()
	if got := len(frame.deferred); got != 1 {
		t.Fatalf("want only put_first's entry parked, got %d", got)
	}
	frame.Abort()
	f.Dispose()
	waitGoneDC(f)
	if got, want := compensations(), []string{"first", "site"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("abort ran %v, want %v", got, want)
	}
}
