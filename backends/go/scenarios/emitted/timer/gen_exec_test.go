package timer

// Executes the EMITTED timer components (gen.go, produced by
// backends/go/emit.py from timer.ir.json) against the REAL stc-go runtime.
// White-box (same package) so it can read the emitted unexported service keys
// and drive the emitted clock coeffect. This is the go tier's exit test for
// item 57 (docs/time-coeffect.md): deterministic firing under RevlClockAdvance
// and unload-cancels-no-residue.

import (
	stdctx "context"
	"testing"
	"time"

	stc "github.com/0xdenny218/stc-go"
)

func firingsOf(serial int) []int64 {
	var at []int64
	for _, f := range RevlClockFirings() {
		if int(f[0]) == serial {
			at = append(at, f[1])
		}
	}
	return at
}

func hostHas(marks []string, want string) bool {
	for _, m := range marks {
		if m == want {
			return true
		}
	}
	return false
}

// Property 1 (deterministic firing) + property 2 (unload cancels, residue-free)
// on emitted code:
//   - Heartbeat (requires log) arms an `every 30s` and an `after 5m` timer once
//     Sink (provides log) has activated it. Time does not pass on its own.
//   - RevlClockAdvance fires due timers as deterministic timeline steps: the
//     `every` fires on every 30s boundary; the `after` fires once at 300000ms.
//   - Disposing Heartbeat runs the timers' derived inverses (Cancel), so no
//     interval outlives the activation: RevlClockPending drops to 0, the R1
//     live-resource count returns to 0, and a further advance fires nothing.
func TestEmitted_Timer_DeterministicFiring_UnloadCancels(t *testing.T) {
	HostReset()
	RevlClockReset()
	g := stdctx.Background()
	root := stc.New()
	defer root.Close()

	sink := LoadSink(root)
	if err := sink.Ready(g); err != nil {
		t.Fatalf("Sink: %v", err)
	}
	hb := LoadHeartbeat(root)
	if err := hb.Ready(g); err != nil {
		t.Fatalf("Heartbeat: %v", err)
	}

	// Arming happened at activation, but no firing before time is advanced.
	if n := RevlClockPending(); n != 2 {
		t.Fatalf("pending timers after activation = %d, want 2", n)
	}
	if f := RevlClockFirings(); len(f) != 0 {
		t.Fatalf("nothing should fire unbidden, got %v", f)
	}
	marks := HostMarks()
	if !hostHas(marks, "timer#1.schedule every 30000ms") ||
		!hostHas(marks, "timer#2.schedule after 300000ms") {
		t.Fatalf("schedule not traced into the effect ledger: %v", marks)
	}
	if n := revlHostLive(); n != 2 {
		t.Fatalf("live resources after arming = %d, want 2 (both schedules)", n)
	}

	// Advance 30s: exactly the periodic timer's first firing.
	if fired := RevlClockAdvance(30000); fired != 1 {
		t.Fatalf("advance(30000) fired %d, want 1", fired)
	}
	if RevlClockNow() != 30000 {
		t.Fatalf("clock now = %d, want 30000", RevlClockNow())
	}

	// Advance another 60s: the periodic re-arms across the span (60000, 90000).
	if fired := RevlClockAdvance(60000); fired != 2 {
		t.Fatalf("advance(60000) fired %d, want 2", fired)
	}
	if got := firingsOf(1); len(got) != 3 ||
		got[0] != 30000 || got[1] != 60000 || got[2] != 90000 {
		t.Fatalf("periodic firings = %v, want [30000 60000 90000]", got)
	}
	// The one-shot has not come due yet.
	if got := firingsOf(2); len(got) != 0 {
		t.Fatalf("one-shot fired early: %v", got)
	}

	// Advance past 5m: the one-shot fires exactly once (at 300000ms) and does
	// not re-arm; the periodic keeps firing on each boundary.
	before := len(firingsOf(1))
	RevlClockAdvance(300000) // now 390000
	if got := firingsOf(2); len(got) != 1 || got[0] != 300000 {
		t.Fatalf("one-shot firings = %v, want [300000]", got)
	}
	if now := len(firingsOf(1)); now <= before {
		t.Fatalf("periodic stopped firing across the span (%d -> %d)", before, now)
	}
	// The spent one-shot released its slot; the periodic is still live.
	if n := RevlClockPending(); n != 1 {
		t.Fatalf("pending after one-shot spent = %d, want 1 (the periodic)", n)
	}
	if n := revlHostLive(); n != 1 {
		t.Fatalf("live resources after one-shot spent = %d, want 1", n)
	}

	// Unload Heartbeat: its timers' derived inverses (Cancel) run — no orphaned
	// interval outlives the activation.
	hb.Dispose()
	if err := hb.Gone(g); err != nil {
		t.Fatalf("Heartbeat dispose: %v", err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for RevlClockPending() != 0 {
		if time.Now().After(deadline) {
			t.Fatalf("teardown left %d live timers (residue)", RevlClockPending())
		}
		time.Sleep(5 * time.Millisecond)
	}
	if n := revlHostLive(); n != 0 {
		t.Fatalf("residue after unload: %d live resources, want 0", n)
	}
	if !hostHas(HostMarks(), "timer#1.cancel") {
		t.Fatalf("periodic timer was not cancelled on teardown: %v", HostMarks())
	}

	// No orphaned firing: advancing after teardown fires nothing.
	if fired := RevlClockAdvance(120000); fired != 0 {
		t.Fatalf("advance after unload fired %d, want 0 (no orphaned interval)", fired)
	}
}
