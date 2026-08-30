package stdlib

// Hand-written execution harness for the EMITTED v3_stdlib module (gen.go,
// from backends/typescript/tests/fixtures/v3_stdlib.ir.json). White-box so it
// can construct the emitted Opt/Result tagged values. Asserts COMPUTED values,
// including persistence (push/concat do not mutate their input).

import (
	"reflect"
	"testing"
)

func TestV3StrBuiltins(t *testing.T) {
	if got := strLen("héllo"); got != 5 { // code-point length, not bytes
		t.Fatalf("strLen = %d, want 5", got)
	}
	if got := repeated("ab", 3); got != "ababab" {
		t.Fatalf("repeated = %q", got)
	}
	if got := indexOfSub("hello", "ll"); got != 2 {
		t.Fatalf("indexOf = %d, want 2", got)
	}
	if got := indexOfSub("hello", "zz"); got != -1 {
		t.Fatalf("indexOf(absent) = %d, want -1", got)
	}
	if got := charAtOf("abc", 1); got != "b" {
		t.Fatalf("charAt = %q", got)
	}
	if got := codeAtOf("A", 0); got != 65 {
		t.Fatalf("charCodeAt = %d, want 65", got)
	}
	if got := splitOn("a,b,c", ","); !reflect.DeepEqual(got, []string{"a", "b", "c"}) {
		t.Fatalf("split = %v", got)
	}
	if got := joinWith([]string{"a", "b", "c"}, "-"); got != "a-b-c" {
		t.Fatalf("join = %q", got)
	}
}

func TestV3ListBuiltins(t *testing.T) {
	if got := concatLists([]int64{1, 2}, []int64{3}); !reflect.DeepEqual(got, []int64{1, 2, 3}) {
		t.Fatalf("concat = %v", got)
	}
	if got := sliced([]int64{10, 20, 30, 40}); !reflect.DeepEqual(got, []int64{20, 30}) {
		t.Fatalf("slice = %v", got)
	}
}

func TestV3Persistence(t *testing.T) {
	xs := []int64{1, 2, 3}
	got := pushed(xs, 9)
	if !reflect.DeepEqual(got, []int64{1, 2, 3, 9}) {
		t.Fatalf("pushed = %v", got)
	}
	if !reflect.DeepEqual(xs, []int64{1, 2, 3}) {
		t.Fatalf("push MUTATED its input: %v", xs)
	}
}

func TestV3Interp(t *testing.T) {
	if got := greetN("Ada", 3); got != "hi Ada#3!" {
		t.Fatalf("greetN = %q", got)
	}
}

func TestV3Opt(t *testing.T) {
	// optfield reads through a value and short-circuits on None.
	some := optName(RevlSome[Row]{Value: Row{Id: 1, Name: "ada"}})
	if s, ok := some.(RevlSome[string]); !ok || s.Value != "ada" {
		t.Fatalf("optName(Some) = %#v", some)
	}
	if _, ok := optName(RevlNone[Row]{}).(RevlNone[string]); !ok {
		t.Fatalf("optName(None) should be None")
	}
	// optcall
	code := optCode(RevlSome[string]{Value: "A"})
	if c, ok := code.(RevlSome[int64]); !ok || c.Value != 65 {
		t.Fatalf("optCode(Some) = %#v", code)
	}
	if _, ok := optCode(RevlNone[string]{}).(RevlNone[int64]); !ok {
		t.Fatalf("optCode(None) should be None")
	}
	// Some/None match
	if got := unwrapOr(RevlSome[int64]{Value: 7}, 99); got != 7 {
		t.Fatalf("unwrapOr(Some) = %d, want 7", got)
	}
	if got := unwrapOr(RevlNone[int64]{}, 99); got != 99 {
		t.Fatalf("unwrapOr(None) = %d, want 99", got)
	}
}

func TestV3Result(t *testing.T) {
	if got := resultFold(okOf(42)); got != 42 {
		t.Fatalf("resultFold(Ok 42) = %d", got)
	}
	if got := resultFold(errOf("boom")); got != -4 { // -len("boom")
		t.Fatalf("resultFold(Err) = %d, want -4", got)
	}
}

func TestV3Arrow(t *testing.T) {
	if got := adder()(41); got != 42 {
		t.Fatalf("adder()(41) = %d", got)
	}
}
