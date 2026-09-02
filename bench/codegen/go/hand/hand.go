// Package hand is the YARDSTICK: the Go a competent Go developer would write
// by hand for the same semantics as bench/codegen/go/probe.rvl and probe2.rvl.
//
// The rules this package holds itself to, so the comparison is honest:
//
//   - Same observable semantics, including revl's checked Int arithmetic. Add
//     keeps the emitter's overflow panic verbatim (see addChecked below), so a
//     measured gap is never just "the hand version dropped a safety check".
//   - Same code-point (not byte) indexing for Str, per docs/strings.md.
//   - Same persistence where the revl program actually observes it. Where a
//     revl value is provably dead after the step that rebuilds it (the `out =
//     out.push(i)` and `m = m.set(k, v)` loops), the hand version mutates in
//     place, because that is what a Go developer writes and it is what an
//     emitter with a liveness check could emit.
//   - No cheating with a different algorithm: HandIndexOf still answers a
//     code-point index, HandTake still clamps exactly as revlStrSlice does.
package hand

import (
	"slices"
	"strconv"
	"strings"
	"unicode/utf8"
)

// addChecked is byte-for-byte the emitter's revlAdd, so the arithmetic
// semantics of the two sides match and the delta is purely codegen shape.
func addChecked(a, b int64) int64 {
	s := a + b
	if (a > 0 && b > 0 && s < 0) || (a < 0 && b < 0 && s >= 0) {
		panic("revl: Int overflow")
	}
	return s
}

// ---- probe.rvl ---------------------------------------------------------

// SumIds mirrors `sum_ids`. The emitted form is already this, modulo the
// blank-assign; it is here as the control.
func SumIds(xs []int64) int64 {
	var n int64
	for _, x := range xs {
		n = addChecked(n, x)
	}
	return n
}

// Scan mirrors `scan`: the sum of the code points of s. `range` over a string
// already walks code points, so no []rune materialization is needed.
func Scan(s string) int64 {
	var acc int64
	for _, r := range s {
		acc = addChecked(acc, int64(r))
	}
	return acc
}

// FirstAt mirrors `first_at`: the code-point index of the first scalar equal to
// c, or -1. `range` walks code points, so the index is bookkeeping and nothing
// re-walks the string.
func FirstAt(s string, c int64) int64 {
	var i int64
	for _, r := range s {
		if int64(r) == c {
			return i
		}
		i++
	}
	return -1
}

// RunLen mirrors `run_len`: how many leading code points equal c. The index is
// read AFTER the loop, so it pins that a rewritten scan still leaves it right.
func RunLen(s string, c int64) int64 {
	var i int64
	for _, r := range s {
		if int64(r) != c {
			break
		}
		i++
	}
	return i
}

// Step2 mirrors `step2`: every other code point. The index advances by 2, so
// the scan rewrite must refuse it; this is the negative control.
func Step2(s string) int64 {
	r := []rune(s)
	var acc int64
	for i := 0; i < len(r); i += 2 {
		acc = addChecked(acc, int64(r[i]))
	}
	return acc
}

// Build mirrors `build`: xs joined with sep, trailing sep included.
func Build(xs []string, sep string) string {
	n := 0
	for _, x := range xs {
		n += len(x) + len(sep)
	}
	var b strings.Builder
	b.Grow(n)
	for _, x := range xs {
		b.WriteString(x)
		b.WriteString(sep)
	}
	return b.String()
}

// Label mirrors `label`: the interpolation `item ${name} #${n}`. Both operand
// types are statically known (Str, Int), so no reflective formatting is needed.
func Label(name string, n int64) string {
	var digits [20]byte
	d := strconv.AppendInt(digits[:0], n, 10)
	var b strings.Builder
	b.Grow(len("item ") + len(name) + len(" #") + len(d))
	b.WriteString("item ")
	b.WriteString(name)
	b.WriteString(" #")
	b.Write(d)
	return b.String()
}

// Tag mirrors `tag`: the interpolation `${a}/${b}` over two Str operands. Both
// types are known statically, so this is concatenation and nothing else.
func Tag(a, b string) string { return a + "/" + b }

// Collect mirrors `collect`: [0, n). The revl source rebinds `out` from its own
// push and never aliases the old value, so the list is linear and append is
// the faithful lowering.
func Collect(n int64) []int64 {
	out := make([]int64, 0, n)
	for i := int64(0); i < n; i = addChecked(i, 1) {
		out = append(out, i)
	}
	return out
}

// ---- probe2.rvl --------------------------------------------------------

// IndexOf mirrors `index_of`: the CODE-POINT index of needle in hay, or -1.
// A byte search plus one rune count over the prefix gives the same answer
// without materializing either operand as []rune.
func IndexOf(hay, needle string) int64 {
	if needle == "" {
		return 0
	}
	if !utf8.ValidString(hay) || !utf8.ValidString(needle) {
		// An invalid byte is one U+FFFD code point everywhere else in the
		// language, so it must compare as one here too; strings.Index would
		// compare it as a raw byte. Same guard the emitted helper carries.
		rh, rn := []rune(hay), []rune(needle)
		for i := 0; i+len(rn) <= len(rh); i++ {
			if slices.Equal(rh[i:i+len(rn)], rn) {
				return int64(i)
			}
		}
		return -1
	}
	b := strings.Index(hay, needle)
	if b < 0 {
		return -1
	}
	return int64(utf8.RuneCountInString(hay[:b]))
}

// Take mirrors `take` / revlStrSlice: the [a, b) code-point slice, clamped.
func Take(s string, a, b int64) string {
	n := int64(utf8.RuneCountInString(s))
	if a < 0 {
		a = 0
	}
	if a > n {
		a = n
	}
	if b > n {
		b = n
	}
	if b < a {
		b = a
	}
	// a == n and b == n both mean "the end of s", which `range` never yields,
	// so both offsets start there and are only pulled back if the walk finds
	// the code point.
	lo, hi, i := len(s), len(s), int64(0)
	for off, r := range s {
		if i == a {
			lo = off
		}
		if i == b {
			hi = off
			break
		}
		if r == utf8.RuneError {
			if _, w := utf8.DecodeRuneInString(s[off:]); w == 1 && i >= a {
				// An invalid byte inside the requested range. revl's Str is a
				// sequence of code points and an invalid byte reads as one
				// U+FFFD everywhere else in the language (revlStrCharAt
				// answers U+FFFD for it too), so the result cannot be a shared
				// substring and this is the one case that copies.
				return string([]rune(s)[a:b])
			}
		}
		i++
	}
	return s[lo:hi]
}

// Chars mirrors `chars` / revlStrSplit with an empty separator: one string per
// code point. Each element is a substring, so it shares s's bytes and the only
// allocation is the slice itself.
func Chars(s string) []string {
	out := make([]string, 0, utf8.RuneCountInString(s))
	for off := 0; off < len(s); {
		r, w := utf8.DecodeRuneInString(s[off:])
		if r == utf8.RuneError && w == 1 {
			out = append(out, string(utf8.RuneError))
		} else {
			out = append(out, s[off:off+w])
		}
		off += w
	}
	return out
}

// Render mirrors `render` (`Int.to_str`).
func Render(n int64) string { return strconv.FormatInt(n, 10) }

// ---- the ADT and its match --------------------------------------------

// Outcome mirrors the revl ADT `Ok(Row) | NotFound | Invalid(Str)`. The shape
// is the emitter's own sealed-interface encoding, kept deliberately identical
// so Describe measures the match lowering and not the data representation.
type Outcome interface{ isOutcome() }

// Row mirrors the revl record.
type Row struct {
	Id   int64
	Name string
}

// OutcomeOk is the Ok arm.
type OutcomeOk struct{ Value Row }

func (OutcomeOk) isOutcome() {}

// OutcomeNotFound is the NotFound arm.
type OutcomeNotFound struct{}

func (OutcomeNotFound) isOutcome() {}

// OutcomeInvalid is the Invalid arm.
type OutcomeInvalid struct{ Value string }

func (OutcomeInvalid) isOutcome() {}

// Describe mirrors `describe`: the same type switch, without the emitter's
// immediately-invoked func literal wrapper.
func Describe(o Outcome) string {
	switch m := o.(type) {
	case OutcomeOk:
		return m.Value.Name
	case OutcomeNotFound:
		return "not found"
	case OutcomeInvalid:
		return m.Value
	default:
		panic("unreachable: non-exhaustive match")
	}
}

// Bucket mirrors `bucket`. The emitted form is already this; the control.
func Bucket(n int64) string {
	if n < 0 {
		return "neg"
	}
	if n == 0 {
		return "zero"
	}
	return "pos"
}

// Tally mirrors `tally`: how many distinct words. The revl `m = m.set(w, 1)`
// drops the previous map on every step, so a single mutable map is faithful.
func Tally(words []string) int64 {
	m := make(map[string]int64, len(words))
	for _, w := range words {
		m[w] = 1
	}
	return int64(len(m))
}

// Find mirrors `find`: the value under k, or -1. revl's Opt is a two-value
// answer, and Go spells that natively.
func Find(m map[string]int64, k string) int64 {
	if v, ok := m[k]; ok {
		return v
	}
	return -1
}

// ---- helper-level yardsticks ------------------------------------------

// SortedKeys mirrors revlMapKeys: the keys in ascending byte order (Go's
// string < is exactly UTF-8 byte lexicographic, so this is the same order).
// The emitter ships a hand-rolled insertion sort; the standard library sorts
// in n log n and needs no extra allocation.
func SortedKeys(m map[string]int64) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	return keys
}

// LookupOpt mirrors `maybe`: revl's Opt is a two-value answer, and Go spells
// that natively with no interface value to box.
func LookupOpt(m map[string]int64, k string) (int64, bool) {
	v, ok := m[k]
	return v, ok
}

// OptI64 is what an unboxed Opt[Int] element looks like in a list: a two-word
// struct, no interface header, no per-element heap cell.
type OptI64 struct {
	Value int64
	Ok    bool
}

// Boxed mirrors `boxed`: a list of Opt[Int] built by lookup.
func Boxed(xs []string, m map[string]int64) []OptI64 {
	out := make([]OptI64, 0, len(xs))
	for _, x := range xs {
		v, ok := m[x]
		out = append(out, OptI64{Value: v, Ok: ok})
	}
	return out
}
