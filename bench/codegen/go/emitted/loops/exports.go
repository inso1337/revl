// Hand-written shim, not generated. The emitter lowers a revl `pub fn` to an
// UNEXPORTED Go func (`pub fn scan` -> `func scan`), so a benchmark in another
// package cannot call it. These wrappers are the only bridge; each is a direct
// tail call so it inlines and adds nothing to the measurement.
package loops

// SumIds is the emitted `sum_ids`.
func SumIds(xs []int64) int64 { return sum_ids(xs) }

// Scan is the emitted `scan`.
func Scan(s string) int64 { return scan(s) }

// FirstAt is the emitted `first_at`: a scan loop with a `return` out of it.
func FirstAt(s string, c int64) int64 { return first_at(s, c) }

// RunLen is the emitted `run_len`: a scan loop that `break`s and then reads the
// index after the loop, which is what pins the rewritten loop's bookkeeping.
func RunLen(s string, c int64) int64 { return run_len(s, c) }

// Step2 is the emitted `step2`: an index that advances by 2, which the scan
// rewrite must REFUSE. It is here as the negative control.
func Step2(s string) int64 { return step2(s) }

// Build is the emitted `build`.
func Build(xs []string, sep string) string { return build(xs, sep) }

// Label is the emitted `label`.
func Label(name string, n int64) string { return label(name, n) }

// Collect is the emitted `collect`.
func Collect(n int64) []int64 { return collect(n) }

// Tag is the emitted `tag`: the interpolation `${a}/${b}` where BOTH operands
// are statically Str.
func Tag(a, b string) string { return tag(a, b) }

// Joined is the emitted `joined`: `a + "/" + b`, the same value written with
// the concatenation operator instead of interpolation. It is the in-language
// control for Tag, so the pair isolates the interpolation lowering itself.
func Joined(a, b string) string { return joined(a, b) }
