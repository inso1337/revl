// The operator E-Stop on the go tier — roadmap item 443, issue #122.
//
// Item 443 landed the halt on the py reference tier: a latch file, a crossing
// seam that refuses once it is armed, and an in-flight inventory. This suite
// pins the go tier honoring the latch:
//
//   - the latch READER reads a malformed latch as HALTED and an absent one as
//     not-halted, byte-for-byte the rule `src/revl/estop.py::read_latch` and
//     `backends/typescript/estop.ts::readLatch` already apply, so the tiers
//     cannot drift on what an operator's armed — or corrupted — latch means;
//   - the in-flight crossing REGISTRY records a crossing and clears it, so the
//     inventory can name what was AMBIGUOUS when the button was hit (item 440);
//   - the halt INVENTORY / halt LINE match the merged residue schema the
//     conductor's report reads (`src/revl/placement.py::_estop_halt_report`).
package estop

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestGoEstopLatchReaderAbsentIsNotHalted(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "nope.estop")
	if ReadLatch(missing) != nil {
		t.Fatalf("an absent latch must read as nil (not halted)")
	}
	if EstopEngagedAt(missing) {
		t.Fatalf("an absent latch must not engage the E-Stop")
	}
	if ReadLatch("") != nil {
		t.Fatalf("an empty path must read as nil")
	}
}

func TestGoEstopLatchReaderArmedCarriesFields(t *testing.T) {
	latch := filepath.Join(t.TempDir(), "halt.estop")
	if err := os.WriteFile(latch, []byte(`{"halted":true,"reason":"runaway loop","operator":"ops@example"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	record := ReadLatch(latch)
	if record == nil {
		t.Fatalf("an armed latch must read as halted")
	}
	if record["reason"] != "runaway loop" || record["operator"] != "ops@example" {
		t.Fatalf("the latch's fields must be carried: %v", record)
	}
	if !EstopEngagedAt(latch) {
		t.Fatalf("an armed latch must engage the E-Stop")
	}
}

func TestGoEstopFailsClosedOnMalformedLatch(t *testing.T) {
	// The one failure mode this feature exists to prevent. A corrupted
	// emergency stop reads as HALTED, matching estop.py::read_latch.
	garbage := filepath.Join(t.TempDir(), "garbage.estop")
	if err := os.WriteFile(garbage, []byte("{ this is not json"), 0o644); err != nil {
		t.Fatal(err)
	}
	record := ReadLatch(garbage)
	if record == nil || record["halted"] != true {
		t.Fatalf("a malformed latch must still halt: %v", record)
	}
	if !EstopEngagedAt(garbage) {
		t.Fatalf("a malformed latch must engage the E-Stop")
	}

	// A JSON value that is not an object (a bare array/number) also halts.
	arr := filepath.Join(t.TempDir(), "arr.estop")
	if err := os.WriteFile(arr, []byte("[1, 2, 3]"), 0o644); err != nil {
		t.Fatal(err)
	}
	if !EstopEngagedAt(arr) {
		t.Fatalf("a non-object JSON latch must engage the E-Stop")
	}
}

func TestGoEstopLatchPathPrecedence(t *testing.T) {
	if LatchPath("/a/b.estop", "", true) != "/a/b.estop" {
		t.Fatalf("explicit latch path must win")
	}
	if LatchPath("", "/run/session.wal", true) != "/run/session.wal.estop" {
		t.Fatalf("wal must derive <wal>.estop")
	}
	os.Unsetenv(LatchEnv)
	if LatchPath("", "", true) != "" {
		t.Fatalf("no env means no latch")
	}
	os.Setenv(LatchEnv, "/from/env.estop")
	defer os.Unsetenv(LatchEnv)
	if LatchPath("", "", true) != "/from/env.estop" {
		t.Fatalf("the ambient env must be the last resort")
	}
}

func TestGoEstopEngagedReadsAmbientLatch(t *testing.T) {
	latch := filepath.Join(t.TempDir(), "halt.estop")
	os.Setenv(LatchEnv, latch)
	defer os.Unsetenv(LatchEnv)
	if EstopEngaged() {
		t.Fatalf("no latch file yet: must not be engaged")
	}
	if err := os.WriteFile(latch, []byte(`{"halted":true}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if !EstopEngaged() {
		t.Fatalf("the ambient latch is armed: must be engaged")
	}
}

func TestGoEstopCrossingRegistry(t *testing.T) {
	seq := BeginCrossing("db", "query", "accept")
	found := false
	for _, c := range InFlightCrossings() {
		if c.Seq == seq && c.Key == "db" && c.Method == "query" && c.Direction == "accept" {
			found = true
		}
	}
	if !found {
		t.Fatalf("a begun crossing must appear in the in-flight registry")
	}
	EndCrossing(seq)
	for _, c := range InFlightCrossings() {
		if c.Seq == seq {
			t.Fatalf("an ended crossing must be cleared from the registry")
		}
	}
}

func TestGoEstopInventoryShapeAndHaltLine(t *testing.T) {
	s1 := BeginCrossing("db", "write", "accept")
	defer EndCrossing(s1)
	record := LatchRecord{"reason": "runaway loop", "operator": "ops@example"}
	inv := EstopInventory("edge", InFlightCrossings(), record)

	if inv["verdict"] != "halted" || inv["resumable"] != false {
		t.Fatalf("the inventory must carry the halted verdict and no resume: %v", inv)
	}
	if inv["reason"] != "runaway loop" || inv["operator"] != "ops@example" {
		t.Fatalf("the inventory must carry the latch's reason/operator: %v", inv)
	}
	inFlight, ok := inv["inFlight"].([]map[string]any)
	if !ok || len(inFlight) == 0 {
		t.Fatalf("the in-flight crossing must be reported: %v", inv["inFlight"])
	}
	entry := inFlight[0]
	if entry["kind"] != "estop-ambiguous" || entry["outcome"] != "unknown" {
		t.Fatalf("a crossing in flight at the halt must be AMBIGUOUS with unknown outcome: %v", entry)
	}
	if stranded, ok := inv["stranded"].([]any); !ok || len(stranded) != 0 {
		t.Fatalf("this tier keeps no witnessed-inverse ledger, so stranded is honestly empty: %v", inv["stranded"])
	}

	// The halt line the conductor parses off stdout: `[name] HALTED {json}`.
	line := EstopHaltLine("edge", InFlightCrossings(), record)
	if !strings.HasPrefix(line, "[edge] "+HaltedLine+" ") {
		t.Fatalf("the halt line must carry the conductor's prefix: %q", line)
	}
	payload := strings.TrimPrefix(line, "[edge] "+HaltedLine+" ")
	var parsed map[string]any
	if err := json.Unmarshal([]byte(payload), &parsed); err != nil {
		t.Fatalf("the halt line's payload must be valid JSON: %v", err)
	}
	if parsed["verdict"] != "halted" {
		t.Fatalf("the halt line's JSON must carry the halted verdict: %v", parsed)
	}
}
