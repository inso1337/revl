// Package ab holds the A/B codegen benchmarks for the go backend: every
// benchmark runs the EMITTED lowering and the HAND-WRITTEN yardstick over the
// same input, so `go test -bench` reports the pair side by side.
//
//	cd bench/codegen/go && go test ./ab/ -bench . -benchmem -benchtime=100x -run XXX
//
// READ allocs/op AND B/op ONLY. They are exact, reproducible and independent
// of machine load, and every finding in roadmap item 433 is written against
// them. ns/op from this suite is NOT evidence and was deliberately not
// reported: the audit ran on a machine with a dozen concurrent agents, where
// even an interleaved A/B ratio samples two different load conditions.
// A later timing pass on a quiet machine is exactly this same command with a
// larger -benchtime; nothing here needs to change for it.
//
// The size-scaled pairs (Collect/Tally/Build/Scan at two N) are load-
// independent complexity evidence in their own right: B/op growing ~N^2 as N
// grows 10x is a quadratic, whatever the clock says.
//
// The correctness tests in this file are not decoration: they are what makes
// the comparison meaningful. If a Test fails, the hand-written side is no
// longer the same program and its numbers mean nothing.
package ab

import (
	"strings"
	"testing"

	"revl.bench/codegen/emitted/loops"
	"revl.bench/codegen/emitted/values"
	"revl.bench/codegen/hand"
)

// ---- fixtures ----------------------------------------------------------

const asciiText = "the quick brown fox jumps over the lazy dog, and then does it again twice more"

// mixedText carries multi-byte code points so the code-point-indexing
// semantics are actually exercised, not just the ASCII fast path.
const mixedText = "naïve café, ünïcödé sämplé with a few multi-byte runes sprinkled through"

func words(n int) []string {
	out := make([]string, n)
	for i := range out {
		out[i] = "word" + string(rune('a'+i%26)) + string(rune('a'+(i/26)%26))
	}
	return out
}

func nums(n int) []int64 {
	out := make([]int64, n)
	for i := range out {
		out[i] = int64(i)
	}
	return out
}

// stringMap's values are deliberately larger than 255. Go's runtime keeps a
// static table of boxed integers 0..255 (runtime.staticuint64s), so a small
// value put into an interface costs no allocation and would have hidden the
// F9 boxing entirely.
func stringMap(n int) map[string]int64 {
	m := make(map[string]int64, n)
	for i, w := range words(n) {
		m[w] = int64(i)*1000003 + 4096
	}
	return m
}

// ---- equivalence: the hand side must be the same program ---------------

func TestHandMatchesEmitted(t *testing.T) {
	if got, want := hand.SumIds(nums(64)), loops.SumIds(nums(64)); got != want {
		t.Errorf("SumIds: hand %v, emitted %v", got, want)
	}
	for _, s := range []string{"", "a", asciiText, mixedText} {
		if got, want := hand.Scan(s), loops.Scan(s); got != want {
			t.Errorf("Scan(%q): hand %v, emitted %v", s, got, want)
		}
	}
	if got, want := hand.Build(words(16), ","), loops.Build(words(16), ","); got != want {
		t.Errorf("Build: hand %q, emitted %q", got, want)
	}
	for _, n := range []int64{0, 1, -1, 42, 9223372036854775807, -9223372036854775808} {
		if got, want := hand.Label("x", n), loops.Label("x", n); got != want {
			t.Errorf("Label(_, %d): hand %q, emitted %q", n, got, want)
		}
		if got, want := hand.Render(n), values.Render(n); got != want {
			t.Errorf("Render(%d): hand %q, emitted %q", n, got, want)
		}
	}
	got, want := hand.Collect(32), loops.Collect(32)
	if len(got) != len(want) {
		t.Fatalf("Collect: hand len %d, emitted len %d", len(got), len(want))
	}
	for i := range got {
		if got[i] != want[i] {
			t.Fatalf("Collect[%d]: hand %v, emitted %v", i, got[i], want[i])
		}
	}
}

func TestHandMatchesEmittedStrings(t *testing.T) {
	for _, s := range []string{"", "a", asciiText, mixedText} {
		for _, sub := range []string{"", "a", "é", "quick", "zzz", s} {
			if got, want := hand.IndexOf(s, sub), values.IndexOf(s, sub); got != want {
				t.Errorf("IndexOf(%q, %q): hand %v, emitted %v", s, sub, got, want)
			}
		}
		n := int64(len([]rune(s)))
		for _, ab := range [][2]int64{{0, 0}, {0, 1}, {1, 3}, {0, n}, {n, n}, {-5, 2}, {2, 1}, {0, n + 9}} {
			if got, want := hand.Take(s, ab[0], ab[1]), values.Take(s, ab[0], ab[1]); got != want {
				t.Errorf("Take(%q, %d, %d): hand %q, emitted %q", s, ab[0], ab[1], got, want)
			}
		}
	}
}

func TestHandMatchesEmittedValues(t *testing.T) {
	if got, want := hand.Describe(hand.OutcomeOk{Value: hand.Row{Id: 1, Name: "n"}}), values.Describe(values.NewOk(1, "n")); got != want {
		t.Errorf("Describe(Ok): hand %q, emitted %q", got, want)
	}
	if got, want := hand.Describe(hand.OutcomeInvalid{Value: "why"}), values.Describe(values.NewInvalid("why")); got != want {
		t.Errorf("Describe(Invalid): hand %q, emitted %q", got, want)
	}
	for _, n := range []int64{-1, 0, 1} {
		if got, want := hand.Bucket(n), values.Bucket(n); got != want {
			t.Errorf("Bucket(%d): hand %q, emitted %q", n, got, want)
		}
	}
	w := words(50)
	if got, want := hand.Tally(w), values.Tally(w); got != want {
		t.Errorf("Tally: hand %v, emitted %v", got, want)
	}
	m := stringMap(50)
	for _, k := range []string{w[0], w[len(w)-1], "absent"} {
		if got, want := hand.Find(m, k), values.Find(m, k); got != want {
			t.Errorf("Find(%q): hand %v, emitted %v", k, got, want)
		}
	}
	hk, ek := hand.SortedKeys(m), values.KeyList(m)
	if strings.Join(hk, "\x00") != strings.Join(ek, "\x00") {
		t.Errorf("SortedKeys: hand %v, emitted %v", hk, ek)
	}
}

// ---- F1: Str.charCodeAt / codepoint_at in a loop -----------------------
//
// The emitter lowers every code-point read to revlStrCharCodeAt, which does
// `[]rune(s)` per call. In a scanning loop that is one heap allocation of the
// whole string per character.

func BenchmarkScanEmitted78(b *testing.B)  { benchScanEmitted(b, asciiText) }
func BenchmarkScanHand78(b *testing.B)     { benchScanHand(b, asciiText) }
func BenchmarkScanEmitted780(b *testing.B) { benchScanEmitted(b, strings.Repeat(asciiText, 10)) }
func BenchmarkScanHand780(b *testing.B)    { benchScanHand(b, strings.Repeat(asciiText, 10)) }

func benchScanEmitted(b *testing.B, s string) {
	b.ReportAllocs()
	for b.Loop() {
		sink64 = loops.Scan(s)
	}
}

func benchScanHand(b *testing.B, s string) {
	b.ReportAllocs()
	for b.Loop() {
		sink64 = hand.Scan(s)
	}
}

func BenchmarkCharCodeAtEmitted(b *testing.B) {
	b.ReportAllocs()
	for b.Loop() {
		sink64 = values.CharAt(asciiText, 3)
	}
}

func BenchmarkCharCodeAtHand(b *testing.B) {
	b.ReportAllocs()
	for b.Loop() {
		sink64 = handCharAt(asciiText, 3)
	}
}

// handCharAt is the single-code-point read a Go developer writes: walk to the
// index and stop, never materializing the string as []rune.
func handCharAt(s string, i int64) int64 {
	var n int64
	for _, r := range s {
		if n == i {
			return int64(r)
		}
		n++
	}
	panic("index out of range")
}

// ---- F2: string concatenation in a loop --------------------------------

func BenchmarkBuildEmitted100(b *testing.B)  { benchBuildEmitted(b, 100) }
func BenchmarkBuildHand100(b *testing.B)     { benchBuildHand(b, 100) }
func BenchmarkBuildEmitted1000(b *testing.B) { benchBuildEmitted(b, 1000) }
func BenchmarkBuildHand1000(b *testing.B)    { benchBuildHand(b, 1000) }

func benchBuildEmitted(b *testing.B, n int) {
	xs := words(n)
	b.ReportAllocs()
	for b.Loop() {
		sinkStr = loops.Build(xs, ",")
	}
}

func benchBuildHand(b *testing.B, n int) {
	xs := words(n)
	b.ReportAllocs()
	for b.Loop() {
		sinkStr = hand.Build(xs, ",")
	}
}

// ---- F3: List.push in a loop -------------------------------------------

func BenchmarkCollectEmitted100(b *testing.B)  { benchCollectEmitted(b, 100) }
func BenchmarkCollectHand100(b *testing.B)     { benchCollectHand(b, 100) }
func BenchmarkCollectEmitted1000(b *testing.B) { benchCollectEmitted(b, 1000) }
func BenchmarkCollectHand1000(b *testing.B)    { benchCollectHand(b, 1000) }

func benchCollectEmitted(b *testing.B, n int64) {
	b.ReportAllocs()
	for b.Loop() {
		sinkI64s = loops.Collect(n)
	}
}

func benchCollectHand(b *testing.B, n int64) {
	b.ReportAllocs()
	for b.Loop() {
		sinkI64s = hand.Collect(n)
	}
}

// ---- F4: Map.set in a loop ---------------------------------------------

func BenchmarkTallyEmitted100(b *testing.B)  { benchTallyEmitted(b, 100) }
func BenchmarkTallyHand100(b *testing.B)     { benchTallyHand(b, 100) }
func BenchmarkTallyEmitted1000(b *testing.B) { benchTallyEmitted(b, 1000) }
func BenchmarkTallyHand1000(b *testing.B)    { benchTallyHand(b, 1000) }

func benchTallyEmitted(b *testing.B, n int) {
	xs := words(n)
	b.ReportAllocs()
	for b.Loop() {
		sink64 = values.Tally(xs)
	}
}

func benchTallyHand(b *testing.B, n int) {
	xs := words(n)
	b.ReportAllocs()
	for b.Loop() {
		sink64 = hand.Tally(xs)
	}
}

// ---- F5: Opt boxed through a sealed interface --------------------------

func BenchmarkFindEmitted(b *testing.B) {
	m := stringMap(64)
	k := words(64)[7]
	b.ReportAllocs()
	for b.Loop() {
		sink64 = values.Find(m, k)
	}
}

func BenchmarkFindHand(b *testing.B) {
	m := stringMap(64)
	k := words(64)[7]
	b.ReportAllocs()
	for b.Loop() {
		sink64 = hand.Find(m, k)
	}
}

// ---- F6: interpolation and Int.to_str through fmt.Sprintf --------------

// The inputs are drawn from a slice rather than written as literals. With
// literal arguments the compiler inlines the whole call and folds the boxed
// interface operands into read-only static data, so the emitted and hand
// forms both report 1 alloc/op and the boxing disappears from the numbers.
// That is an artifact of the benchmark, not of the lowering: a real program
// passes runtime values.
var labelNames = words(64)

func BenchmarkLabelEmitted(b *testing.B) {
	i := 0
	b.ReportAllocs()
	for b.Loop() {
		sinkStr = loops.Label(labelNames[i&63], int64(i)+4711)
		i++
	}
}

func BenchmarkLabelHand(b *testing.B) {
	i := 0
	b.ReportAllocs()
	for b.Loop() {
		sinkStr = hand.Label(labelNames[i&63], int64(i)+4711)
		i++
	}
}

func BenchmarkRenderEmitted(b *testing.B) {
	i := 0
	b.ReportAllocs()
	for b.Loop() {
		sinkStr = values.Render(int64(i) + 4711)
		i++
	}
}

func BenchmarkRenderHand(b *testing.B) {
	i := 0
	b.ReportAllocs()
	for b.Loop() {
		sinkStr = hand.Render(int64(i) + 4711)
		i++
	}
}

// ---- F7: Str.indexOf and Str.slice through []rune ----------------------

func BenchmarkIndexOfEmitted(b *testing.B) {
	b.ReportAllocs()
	for b.Loop() {
		sink64 = values.IndexOf(asciiText, "lazy")
	}
}

func BenchmarkIndexOfHand(b *testing.B) {
	b.ReportAllocs()
	for b.Loop() {
		sink64 = hand.IndexOf(asciiText, "lazy")
	}
}

func BenchmarkTakeEmitted(b *testing.B) {
	b.ReportAllocs()
	for b.Loop() {
		sinkStr = values.Take(asciiText, 4, 9)
	}
}

func BenchmarkTakeHand(b *testing.B) {
	b.ReportAllocs()
	for b.Loop() {
		sinkStr = hand.Take(asciiText, 4, 9)
	}
}

// ---- F8: Map.keys sorted by insertion sort -----------------------------
//
// Run at two sizes: the ratio between them is what shows the quadratic, and a
// within-run ratio survives a loaded machine far better than any absolute.

func BenchmarkKeysEmitted64(b *testing.B)   { benchKeysEmitted(b, 64) }
func BenchmarkKeysHand64(b *testing.B)      { benchKeysHand(b, 64) }
func BenchmarkKeysEmitted1024(b *testing.B) { benchKeysEmitted(b, 1024) }
func BenchmarkKeysHand1024(b *testing.B)    { benchKeysHand(b, 1024) }

func benchKeysEmitted(b *testing.B, n int) {
	m := stringMap(n)
	b.ReportAllocs()
	for b.Loop() {
		sinkStrs = values.KeyList(m)
	}
}

func benchKeysHand(b *testing.B, n int) {
	m := stringMap(n)
	b.ReportAllocs()
	for b.Loop() {
		sinkStrs = hand.SortedKeys(m)
	}
}

// ---- F9: Opt[T] boxed into a sealed interface across a boundary --------
//
// BenchmarkFind* above shows the case Go already fixes for free: the Opt never
// leaves the function, so escape analysis keeps it on the stack. These two
// show what happens when it does leave.

func BenchmarkMaybeEmitted(b *testing.B) {
	m := stringMap(64)
	k := words(64)[7]
	b.ReportAllocs()
	for b.Loop() {
		sinkOpt = values.Maybe(m, k)
	}
}

func BenchmarkMaybeHand(b *testing.B) {
	m := stringMap(64)
	k := words(64)[7]
	b.ReportAllocs()
	for b.Loop() {
		sink64, sinkBool = hand.LookupOpt(m, k)
	}
}

func BenchmarkBoxedListEmitted(b *testing.B) {
	xs := words(200)
	m := stringMap(200)
	b.ReportAllocs()
	for b.Loop() {
		sinkOpts = values.Boxed(xs, m)
	}
}

func BenchmarkBoxedListHand(b *testing.B) {
	xs := words(200)
	m := stringMap(200)
	b.ReportAllocs()
	for b.Loop() {
		sinkHandOpts = hand.Boxed(xs, m)
	}
}

// ---- controls: expected to be a dead heat ------------------------------

func BenchmarkSumIdsEmitted(b *testing.B) {
	xs := nums(1000)
	b.ReportAllocs()
	for b.Loop() {
		sink64 = loops.SumIds(xs)
	}
}

func BenchmarkSumIdsHand(b *testing.B) {
	xs := nums(1000)
	b.ReportAllocs()
	for b.Loop() {
		sink64 = hand.SumIds(xs)
	}
}

func BenchmarkBucketEmitted(b *testing.B) {
	b.ReportAllocs()
	for b.Loop() {
		sinkStr = values.Bucket(7)
	}
}

func BenchmarkBucketHand(b *testing.B) {
	b.ReportAllocs()
	for b.Loop() {
		sinkStr = hand.Bucket(7)
	}
}

func BenchmarkDescribeEmitted(b *testing.B) {
	o := values.NewOk(1, "name")
	b.ReportAllocs()
	for b.Loop() {
		sinkStr = values.Describe(o)
	}
}

func BenchmarkDescribeHand(b *testing.B) {
	o := hand.Outcome(hand.OutcomeOk{Value: hand.Row{Id: 1, Name: "name"}})
	b.ReportAllocs()
	for b.Loop() {
		sinkStr = hand.Describe(o)
	}
}

// Sinks keep the compiler from folding the calls away.
var (
	sink64       int64
	sinkBool     bool
	sinkStr      string
	sinkI64s     []int64
	sinkStrs     []string
	sinkOpt      values.RevlOpt[int64]
	sinkOpts     []values.RevlOpt[int64]
	sinkHandOpts []hand.OptI64
)

// TestOptEquivalence pins that the emitted Opt and the hand two-value form
// answer the same thing, so the F9 pair is comparing like with like.
func TestOptEquivalence(t *testing.T) {
	m := stringMap(64)
	for _, k := range []string{words(64)[0], words(64)[63], "absent"} {
		ev, eok := values.OptIsSome(values.Maybe(m, k))
		hv, hok := hand.LookupOpt(m, k)
		if eok != hok || (eok && ev != hv) {
			t.Errorf("Maybe(%q): emitted (%v,%v), hand (%v,%v)", k, ev, eok, hv, hok)
		}
	}
	xs := words(32)
	eb, hb := values.Boxed(xs, m), hand.Boxed(xs, m)
	if len(eb) != len(hb) {
		t.Fatalf("Boxed: emitted len %d, hand len %d", len(eb), len(hb))
	}
	for i := range eb {
		ev, eok := values.OptIsSome(eb[i])
		if eok != hb[i].Ok || (eok && ev != hb[i].Value) {
			t.Errorf("Boxed[%d]: emitted (%v,%v), hand (%v,%v)", i, ev, eok, hb[i].Value, hb[i].Ok)
		}
	}
}

// ---- F6b: an all-Str interpolation, with its own in-language control ---
//
// `${a}/${b}` and `a + "/" + b` are the same value and both operands are
// statically Str. BenchmarkTagJoinedEmitted runs the SECOND spelling through
// the SAME emitter, so the pair measures the interpolation lowering with the
// backend, the toolchain and the machine held fixed.

func BenchmarkTagEmitted(b *testing.B) {
	i := 0
	b.ReportAllocs()
	for b.Loop() {
		sinkStr = loops.Tag(labelNames[i&63], labelNames[(i+1)&63])
		i++
	}
}

func BenchmarkTagJoinedEmitted(b *testing.B) {
	i := 0
	b.ReportAllocs()
	for b.Loop() {
		sinkStr = loops.Joined(labelNames[i&63], labelNames[(i+1)&63])
		i++
	}
}

func BenchmarkTagHand(b *testing.B) {
	i := 0
	b.ReportAllocs()
	for b.Loop() {
		sinkStr = hand.Tag(labelNames[i&63], labelNames[(i+1)&63])
		i++
	}
}

// TestTagEquivalence pins that all three spellings answer the same string.
func TestTagEquivalence(t *testing.T) {
	for _, p := range [][2]string{{"", ""}, {"a", "b"}, {"alpha", "beta"}, {"é", "ü"}} {
		e, c, h := loops.Tag(p[0], p[1]), loops.Joined(p[0], p[1]), hand.Tag(p[0], p[1])
		if e != c || e != h {
			t.Errorf("Tag(%q,%q): interp %q, concat %q, hand %q", p[0], p[1], e, c, h)
		}
	}
}
