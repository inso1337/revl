package externcompensate

// Issue #1592 on the go tier: an emission extern that DECLARES its own
// `compensate` (item 254) registers that compensation at every site it is
// emitted from. Before the fix the go emitter rendered only the forward call,
// so an abort left "put ..." in the host trace with no "restore". This drives
// the EMITTED components (gen_extern_compensate_test.go) against the real
// stc-go runtime:
//
//   * activation body: a clean unload discharges, a failed activation runs the
//     compensation in Phase 2;
//   * provide-method body: a call then a clean unload discharges, a call then
//     RevlFrame.Abort() runs it;
//   * `every` timer firing: one registration per firing, discharged on a clean
//     unload, all run on Abort();
//   * the site-spelled control, which go registered before the fix.
//
// White-box (same package) so it reaches the activation frame via
// RevlFrames(), as the method_compensate scenario does.

import (
	stdctx "context"
	"reflect"
	"strings"
	"testing"
	"time"

	stc "github.com/0xdenny218/stc-go"
)

func waitSettled(f *stc.Fiber) {
	for i := 0; i < 400 && f.State() != stc.StateGone && f.State() != stc.StateFailed; i++ {
		time.Sleep(5 * time.Millisecond)
	}
}

// calls is the host trace without the clock's own entries.
func calls() []string {
	out := []string{}
	for _, m := range HostMarks() {
		if strings.HasPrefix(m, "put ") || strings.HasPrefix(m, "note ") || m == "restore" {
			out = append(out, m)
		}
	}
	return out
}

func reset() *stc.Context {
	HostReset()
	RevlResetFrames()
	RevlClockReset()
	return stc.New()
}

func load(t *testing.T, root *stc.Context, plug func(*stc.Context) *stc.Fiber) *stc.Fiber {
	t.Helper()
	f := plug(root)
	if err := f.Ready(stdctx.Background()); err != nil {
		t.Fatalf("load: %v", err)
	}
	return f
}

func soleFrame(t *testing.T) *RevlFrame {
	t.Helper()
	frames := RevlFrames()
	if len(frames) != 1 {
		t.Fatalf("want exactly one activation frame, got %d", len(frames))
	}
	return frames[0]
}

func want(t *testing.T, got []string, expected ...string) {
	t.Helper()
	if !reflect.DeepEqual(got, expected) {
		t.Fatalf("host trace: got %v, want %v", got, expected)
	}
}

func failedActivation(root *stc.Context, plug func(*stc.Context) *stc.Fiber) {
	f := plug(root)
	_ = f.Ready(stdctx.Background())
	waitSettled(f)
}

func TestSiteSpelledCompensationRunsOnAbort(t *testing.T) {
	failedActivation(reset(), LoadSiteAbort)
	want(t, calls(), "note s", "restore")
}

func TestSiteSpelledMethodCompensationRunsOnAbort(t *testing.T) {
	root := reset()
	f := load(t, root, LoadAgent)
	ops, err := stc.Service[Ops](root, _keyOps)
	if err != nil {
		t.Fatalf("resolve ops: %v", err)
	}
	ops.Site("m")
	soleFrame(t).Abort()
	f.Dispose()
	waitSettled(f)
	want(t, calls(), "note m", "restore")
}

func TestActivationExternCompensationDischargesOnCleanUnload(t *testing.T) {
	f := load(t, reset(), LoadExternOk)
	f.Dispose()
	waitSettled(f)
	want(t, calls(), "put y")
}

func TestActivationExternCompensationRunsOnAbort(t *testing.T) {
	failedActivation(reset(), LoadExternAbort)
	want(t, calls(), "put y", "restore")
}

func TestMethodExternCompensationDischargesOnCleanUnload(t *testing.T) {
	root := reset()
	f := load(t, root, LoadAgent)
	ops, err := stc.Service[Ops](root, _keyOps)
	if err != nil {
		t.Fatalf("resolve ops: %v", err)
	}
	ops.Run("m")
	f.Dispose()
	waitSettled(f)
	want(t, calls(), "put m")
}

func TestMethodExternCompensationRunsOnAbort(t *testing.T) {
	root := reset()
	f := load(t, root, LoadAgent)
	ops, err := stc.Service[Ops](root, _keyOps)
	if err != nil {
		t.Fatalf("resolve ops: %v", err)
	}
	ops.Run("m")
	soleFrame(t).Abort()
	f.Dispose()
	waitSettled(f)
	want(t, calls(), "put m", "restore")
}

func TestTimerExternCompensationDischargesOnCleanUnload(t *testing.T) {
	f := load(t, reset(), LoadBeat)
	RevlClockAdvance(25000) // fires at 10s and 20s
	want(t, calls(), "put x", "put x")
	f.Dispose()
	waitSettled(f)
	want(t, calls(), "put x", "put x")
}

func TestTimerExternCompensationRunsOncePerFiringOnAbort(t *testing.T) {
	f := load(t, reset(), LoadBeat)
	RevlClockAdvance(25000)
	soleFrame(t).Abort()
	f.Dispose()
	waitSettled(f)
	want(t, calls(), "put x", "put x", "restore", "restore")
}

// value positions inside a provide method (issue #1592, the positions of
// #1511): a `let`, a `return`, an argument and a nested operand each register
// the extern's compensation once the call returns.
func valuePositions() map[string]func(Ops) {
	return map[string]func(Ops){
		"let":      func(o Ops) { o.Bound("let") },
		"return":   func(o Ops) { o.Returned("return") },
		"argument": func(o Ops) { o.Argument("argument") },
		"nested":   func(o Ops) { o.Nested("nested") },
	}
}

func TestValuePositionCompensationRunsOnAbort(t *testing.T) {
	for tag, call := range valuePositions() {
		t.Run(tag, func(t *testing.T) {
			root := reset()
			f := load(t, root, LoadAgent)
			ops, err := stc.Service[Ops](root, _keyOps)
			if err != nil {
				t.Fatalf("resolve ops: %v", err)
			}
			call(ops)
			soleFrame(t).Abort()
			f.Dispose()
			waitSettled(f)
			want(t, calls(), "put "+tag, "restore")
		})
	}
}

func TestValuePositionCompensationDischargesOnCleanUnload(t *testing.T) {
	for tag, call := range valuePositions() {
		t.Run(tag, func(t *testing.T) {
			root := reset()
			f := load(t, root, LoadAgent)
			ops, err := stc.Service[Ops](root, _keyOps)
			if err != nil {
				t.Fatalf("resolve ops: %v", err)
			}
			call(ops)
			f.Dispose()
			waitSettled(f)
			want(t, calls(), "put "+tag)
		})
	}
}
