// Package estop is the operator E-Stop's shared vocabulary on the go tier —
// roadmap item 443, issue #122. The go twin of `backends/typescript/estop.ts`
// and `src/revl/estop.py`.
//
// `docs/design/443-estop.md` is the reasoning of record. Item 443 landed the
// halt on the py reference tier: a latch file, a crossing seam that refuses
// once it is armed, and an in-flight inventory. The non-py tiers kept their
// cooperative teardown and had NO E-Stop, so a placement halt SIGKILLed a go
// child and reported its residue UNKNOWN. This package is the go tier honoring
// the latch:
//
//   - the latch READER (`LatchPath`, `ReadLatch`, `EstopEngaged`), byte-for-byte
//     the rule `src/revl/estop.py::read_latch` applies — including the
//     fail-closed rule that a malformed latch still reads as HALTED — so the
//     tiers cannot drift on what an armed (or corrupted) latch means;
//   - the in-flight crossing REGISTRY (`BeginCrossing`/`EndCrossing`/
//     `InFlightCrossings`): a crossing still executing when the button is hit is
//     the AMBIGUOUS one (item 440);
//   - the halt INVENTORY (`EstopInventory`/`EstopHaltLine`), shaped into the
//     merged residue schema `src/revl/placement.py::_estop_halt_report` reads.
//
// The seam wiring lives in the `bridge` package (the accept and dispatch seams
// consult `EstopEngaged` and record crossings) and the idle watcher lives in
// the runner (`main.go`); this package is the vocabulary those two share.
package estop

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"sync"
)

// LatchEnv is the ambient latch path, equivalent to `--estop-latch FILE`. The
// conductor (`src/revl/placement.py`) hands a honoring child the latch in its
// spec; the runner publishes it here so the seams and the watcher all read one
// latch. Kept identical to `estop.py::LATCH_ENV` and `estop.ts::LATCH_ENV`.
const LatchEnv = "REVL_ESTOP_LATCH"

// HaltedLine is what a latch-honoring child prints when the latch trips: its
// own in-flight inventory, on one line, so the conductor merges it without a
// second channel. Kept identical to `estop.py::HALTED_LINE`.
const HaltedLine = "HALTED"

// LatchRecord is the halt an operator armed. `halted` is implicit — the file's
// mere presence is the halt — and the rest is carried into the report by name.
type LatchRecord map[string]any

// LatchPath is the latch file to act on: an explicit path, else `<wal>.estop`,
// else the ambient `REVL_ESTOP_LATCH`. Mirrors `estop.py::latch_path` /
// `estop.ts::latchPath`: deriving it from the WAL is not a convenience but the
// durable rendezvous the reconciliation path (`revl recover --wal`) names.
func LatchPath(latch, wal string, env bool) string {
	if latch != "" {
		return latch
	}
	if wal != "" {
		return wal + ".estop"
	}
	if env {
		return os.Getenv(LatchEnv)
	}
	return ""
}

// ReadLatch is the halt an operator wrote at `path`, or nil when the latch is
// absent.
//
// A latch that EXISTS but does not parse still reads as HALTED. Failing open on
// a malformed emergency stop is the one failure mode this feature exists to
// prevent, so every reader — the py runtime seam, the CLI, the conductor, the
// ts tier and now this — applies the same rule. A latch the OS refuses to open
// at all (missing file, permission error) reads as absent, matching
// `estop.py::read_latch` (FileNotFoundError/OSError -> None).
func ReadLatch(path string) LatchRecord {
	if path == "" {
		return nil
	}
	text, err := os.ReadFile(path)
	if err != nil {
		// ENOENT or any other OS-level read failure: the latch is not readable,
		// so it is not a halt. (A malformed BUT readable latch is handled below.)
		return nil
	}
	var record any
	if err := json.Unmarshal(text, &record); err != nil {
		return unreadable()
	}
	if obj, ok := record.(map[string]any); ok {
		return LatchRecord(obj)
	}
	// A JSON value that is not an object (a bare array/number/string) still halts.
	return unreadable()
}

func unreadable() LatchRecord {
	return LatchRecord{
		"halted":   true,
		"reason":   "operator halt (unreadable latch)",
		"operator": "unknown",
	}
}

// EstopEngagedAt reports whether a halt is in force on the latch at `path`.
func EstopEngagedAt(path string) bool {
	return ReadLatch(path) != nil
}

// EstopEngaged reports whether a halt is in force on the latch this process
// watches (the ambient `REVL_ESTOP_LATCH`). The seams consult this on each
// incoming or outgoing crossing: the cost is one file read per crossing WHILE a
// latch is armed, and nothing at all when none is — the default — because
// `LatchPath` short-circuits to "".
func EstopEngaged() bool {
	return EstopEngagedAt(LatchPath("", "", true))
}

// --- the in-flight crossing registry (item 443, issue #122) ------------------

// Crossing is one boundary crossing recorded while it is in flight. A crossing
// still in the registry when the latch trips is AMBIGUOUS: its at-most-once
// attempt may or may not have landed (item 440).
type Crossing struct {
	Key       string
	Method    string
	Direction string // "accept" (an incoming call the serve seam is answering) or
	// "dispatch" (an outgoing call this process's proxy made)
	Seq int64
}

var (
	registryMu sync.Mutex
	inFlight   = map[int64]Crossing{}
	seqCounter int64
)

// BeginCrossing records a crossing as in flight and returns its sequence number.
// The seam pairs it with `EndCrossing` in a deferred call so a panicking handler
// still leaves the registry clean.
func BeginCrossing(key, method, direction string) int64 {
	registryMu.Lock()
	defer registryMu.Unlock()
	seqCounter++
	seq := seqCounter
	inFlight[seq] = Crossing{Key: key, Method: method, Direction: direction, Seq: seq}
	return seq
}

// EndCrossing clears a recorded crossing once its handler returns.
func EndCrossing(seq int64) {
	registryMu.Lock()
	defer registryMu.Unlock()
	delete(inFlight, seq)
}

// InFlightCrossings is a snapshot of the crossings executing right now, ordered
// by sequence so the inventory is deterministic.
func InFlightCrossings() []Crossing {
	registryMu.Lock()
	defer registryMu.Unlock()
	out := make([]Crossing, 0, len(inFlight))
	for _, c := range inFlight {
		out = append(out, c)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Seq < out[j].Seq })
	return out
}

// --- the halt inventory (item 443, issue #122) -------------------------------

func stringField(record LatchRecord, key, fallback string) string {
	if record != nil {
		if v, ok := record[key].(string); ok && v != "" {
			return v
		}
	}
	return fallback
}

// EstopInventory shapes the crossings that were in flight when the button was
// hit into the merged residue schema (`src/revl/placement.py::_estop_halt_report`),
// byte-compatible with the shape the py runner and the ts tier emit.
//
// A crossing still executing when the operator armed the latch is AMBIGUOUS —
// its at-most-once attempt may or may not have landed (item 440), the designed
// outcome of an operator halt, not an edge case. This tier keeps no
// witnessed-inverse ledger, so `stranded` is empty and HONESTLY so: the halt
// reports what it can name (the crossings in flight) rather than inventing a
// book it does not keep, and the conductor never reads that empty list as
// "nothing was owed" because the ambiguous crossings are still reported.
func EstopInventory(process string, crossings []Crossing, record LatchRecord) map[string]any {
	inFlightEntries := make([]map[string]any, 0, len(crossings))
	for _, c := range crossings {
		inFlightEntries = append(inFlightEntries, map[string]any{
			"kind":          "estop-ambiguous",
			"state":         "unresolved",
			"component":     c.Key,
			"method":        c.Method,
			"seq":           c.Seq,
			"entry":         "crossing",
			"direction":     c.Direction,
			"attemptedFlag": true,
			"outcome":       "unknown",
		})
	}
	return map[string]any{
		"process":     process,
		"verdict":     "halted",
		"reason":      stringField(record, "reason", "operator halt"),
		"operator":    stringField(record, "operator", "unknown"),
		"activations": []any{},
		"inFlight":    inFlightEntries,
		"stranded":    []any{},
		"resumable":   false,
	}
}

// EstopHaltLine is the single line a latch-honoring child prints when the button
// is hit: `[name] HALTED {inventory}`. The conductor parses it off stdout by the
// `HaltedLine` prefix (`src/revl/placement.py::pump`) and merges the inventory
// into the halt report without a second channel — the exact contract the py
// runner and the ts tier already meet.
func EstopHaltLine(process string, crossings []Crossing, record LatchRecord) string {
	encoded, err := json.Marshal(EstopInventory(process, crossings, record))
	if err != nil {
		encoded = []byte("{}")
	}
	return fmt.Sprintf("[%s] %s %s", process, HaltedLine, string(encoded))
}
