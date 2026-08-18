// Package scenarios executes revl's backend contract against the REAL stc-go
// runtime (github.com/0xdenny218/stc-go, pinned in go.mod), never a stub.
//
// A Probe service records every effect/undo/emission as an ordered mark;
// assertions are on exact call order. This is the executable oracle the
// cordis-go emitter targets: the hand-written reference (reference_test.go)
// and the emitter's generated output (emitted_user_cache.go) are driven
// through the same harness and must produce the same marks.
package scenarios

import (
	"sync"

	stc "github.com/0xdenny218/stc-go"
)

// Probe is a coeffect-provided recorder. Effects mark do:/undo:, emissions
// mark emit:. It mirrors the Rust backend's Probe trait so the two backends'
// scenarios assert identical semantics.
type Probe interface {
	Mark(m string) int
	Send(m string) int
}

// ProbeKey is the service key the Probe is provided under (root context).
var ProbeKey = stc.NewKey[Probe]("probe")

// Recorder is the concrete Probe: a mutex-guarded ordered log.
type Recorder struct {
	mu  sync.Mutex
	log []string
}

func (r *Recorder) Mark(m string) int {
	r.mu.Lock()
	r.log = append(r.log, m)
	r.mu.Unlock()
	return 0
}

func (r *Recorder) Send(m string) int {
	r.mu.Lock()
	r.log = append(r.log, "emit:"+m)
	r.mu.Unlock()
	return 0
}

// Marks returns a snapshot copy of the ordered marks.
func (r *Recorder) Marks() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]string, len(r.log))
	copy(out, r.log)
	return out
}

// RootWithProbe builds a root context with the Probe provided at root so
// every fiber can read it as a coeffect.
func RootWithProbe() (*stc.Context, *Recorder) {
	root := stc.New()
	rec := &Recorder{}
	if _, err := root.Provide(ProbeKey, Probe(rec)); err != nil {
		panic(err)
	}
	return root, rec
}

// probe reads the Probe coeffect from any context on the tree.
func probe(ctx *stc.Context) Probe {
	p, err := stc.Service[Probe](ctx, ProbeKey)
	if err != nil {
		panic(err)
	}
	return p
}
