package types_functions

// Hand-written execution harness for the EMITTED v3_types_functions module
// (gen.go, produced by backends/go/emit.py from
// backends/typescript/tests/fixtures/v3_types_functions.ir.json).
// White-box (same package) so it can read unexported record fields and the
// sealed-variant case structs. These assert COMPUTED values — this is the
// real execution gate for the pure typed-core tier.

import "testing"

func TestV3AddAndArithmetic(t *testing.T) {
	if got := add(2, 40); got != 42 {
		t.Fatalf("add(2,40) = %d, want 42", got)
	}
	if got := neg(5); got != -5 {
		t.Fatalf("neg(5) = %d, want -5", got)
	}
	if got := negate(true); got != false {
		t.Fatalf("negate(true) = %v, want false", got)
	}
}

func TestV3Classify(t *testing.T) {
	cases := []struct {
		in   int64
		want string
	}{{-3, "neg"}, {0, "zero"}, {7, "pos"}}
	for _, c := range cases {
		if got := classify(c.in); got != c.want {
			t.Fatalf("classify(%d) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestV3ChooseAndFirst(t *testing.T) {
	if got := choose(true); got != 1 {
		t.Fatalf("choose(true) = %d, want 1", got)
	}
	if got := choose(false); got != 2 {
		t.Fatalf("choose(false) = %d, want 2", got)
	}
	if got := first(list()); got != 1 {
		t.Fatalf("first(list()) = %d, want 1", got)
	}
}

func TestV3RecordAndMatch(t *testing.T) {
	r := makeRow(7, "ada")
	if r.Id != 7 || r.Name != "ada" {
		t.Fatalf("makeRow = %+v", r)
	}
	// match over the sealed Outcome variant (type switch)
	if got := describe(OutcomeOk{Value: r}); got != "ada" {
		t.Fatalf("describe(Ok) = %q, want ada", got)
	}
	if got := describe(OutcomeNotFound{}); got != "not found" {
		t.Fatalf("describe(NotFound) = %q", got)
	}
	if got := describe(OutcomeInvalid{Value: "bad"}); got != "bad" {
		t.Fatalf("describe(Invalid) = %q", got)
	}
	// wildcard arm
	if got := label(OutcomeNotFound{}); got != "other" {
		t.Fatalf("label(NotFound) = %q, want other", got)
	}
	if got := label(OutcomeOk{Value: r}); got != "ada" {
		t.Fatalf("label(Ok) = %q, want ada", got)
	}
}

func TestV3Extern(t *testing.T) {
	if got := greet("world"); got != "hello world" {
		t.Fatalf("greet(world) = %q", got)
	}
}
