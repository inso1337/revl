// Hand-written shim, not generated. See emitted/loops/exports.go for why the
// wrappers exist.
package values

// IndexOf is the emitted `index_of`.
func IndexOf(hay, needle string) int64 { return index_of(hay, needle) }

// Take is the emitted `take`.
func Take(s string, a, b int64) string { return take(s, a, b) }

// Chars is the emitted `chars` (`Str.split("")` -> revlStrSplit).
func Chars(s string) []string { return chars(s) }

// Render is the emitted `render` (`Int.to_str`).
func Render(n int64) string { return render(n) }

// Describe is the emitted `describe` (an ADT match).
func Describe(o Outcome) string { return describe(o) }

// Bucket is the emitted `bucket` (a statement-position if chain).
func Bucket(n int64) string { return bucket(n) }

// Tally is the emitted `tally` (Map.set in a loop).
func Tally(words []string) int64 { return tally(words) }

// Find is the emitted `find` (Map.lookup -> Opt -> match).
func Find(m map[string]int64, k string) int64 { return find(m, k) }

// NewOk builds the emitted ADT's Ok arm so benchmarks can feed `Describe`.
func NewOk(id int64, name string) Outcome { return OutcomeOk{Value: Row{Id: id, Name: name}} }

// NewInvalid builds the emitted ADT's Invalid arm.
func NewInvalid(why string) Outcome { return OutcomeInvalid{Value: why} }

// KeyList is the emitted `key_list` (`Map.keys()` -> revlMapKeys).
func KeyList(m map[string]int64) []string { return key_list(m) }

// CharAt is the emitted `char_at` (`Str.charCodeAt`).
func CharAt(s string, i int64) int64 { return char_at(s, i) }

// Maybe is the emitted `maybe`: an `Opt[Int]` crossing a function boundary.
func Maybe(m map[string]int64, k string) RevlOpt[int64] { return maybe(m, k) }

// OptIsSome answers the emitted Opt without the caller depending on the
// sealed-interface spelling.
func OptIsSome(o RevlOpt[int64]) (int64, bool) {
	if s, ok := o.(RevlSome[int64]); ok {
		return s.Value, true
	}
	return 0, false
}

// Boxed is the emitted `boxed`: a List[Opt[Int]], every element boxed.
func Boxed(xs []string, m map[string]int64) []RevlOpt[int64] { return boxed(xs, m) }
