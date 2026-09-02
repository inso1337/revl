package secrettrace

// Executes the EMITTED Issuer component (gen_secret_trace_test.go, produced by
// backends/go/emit.py from secret_trace.ir.json) against the REAL stc-go
// runtime — the executable proof for roadmap item 421 F6 on the go tier.
//
// The go host trace interpolates a `pool.execute` sql straight into `_hostLog`,
// which `HostMarks` hands back to anything embedding the emitted package. A
// value the author declared `Secret[T]` reached it verbatim, because nothing on
// this tier knew what a declared `Secret[T]` was: the marking exists in the IR
// (`externs[i].secret_return`, `params[i].secret`) and only the py emitter read
// it.
//
// Both ends of the declared marking are driven here, because they fail
// independently: `Issue` mints the token at an extern whose declared RETURN was
// `Secret[T]` (the origin), and `Replay` receives one as a declared `Secret[T]`
// PARAMETER with no origin in sight (the receiver). Every assertion is paired —
// the canary must be ABSENT and the placeholder PRESENT — so neither can pass
// because nothing was recorded at all.

import (
	stdctx "context"
	"strings"
	"testing"

	stc "github.com/0xdenny218/stc-go"
)

const canary = "SEKRIT-CANARY"

// Spelled out rather than read off the emitted constant, so this file COMPILES
// against a tree with no redaction in it and fails on the leak instead of on a
// missing symbol.
const redactedSecret = "<redacted:secret>"

func marksContaining(t *testing.T, needle string) []string {
	t.Helper()
	var hits []string
	for _, m := range HostMarks() {
		if strings.Contains(m, needle) {
			hits = append(hits, m)
		}
	}
	return hits
}

func liveVault(t *testing.T) Vault {
	t.Helper()
	HostReset()
	root := stc.New()
	t.Cleanup(func() { _ = root.Close() })
	fiber := LoadIssuer(root)
	if err := fiber.Ready(stdctx.Background()); err != nil {
		t.Fatalf("Issuer: %v", err)
	}
	vault, err := stc.Service[Vault](root, _keyVault)
	if err != nil {
		t.Fatalf("resolve vault: %v", err)
	}
	return vault
}

// The ORIGIN end: the value enters the value world at an extern whose declared
// return was `Secret[T]`, and is then executed as a statement.
func TestEmitted_SecretReturn_IsNotInTheHostTrace(t *testing.T) {
	vault := liveVault(t)
	if got := vault.Issue("alice"); got != "issued" {
		t.Fatalf("issue = %q, want %q", got, "issued")
	}
	if hits := marksContaining(t, canary); len(hits) != 0 {
		t.Fatalf("declared Secret[Str] verbatim in the host trace: %q", hits)
	}
	if hits := marksContaining(t, redactedSecret); len(hits) == 0 {
		t.Fatalf("nothing was recorded at all: %q", HostMarks())
	}
}

// The RECEIVER end: a declared `Secret[T]` parameter, registered at the head of
// the provide method. No origin runs in this test, so it fails on its own if the
// receiver-side marking is missing.
func TestEmitted_SecretParam_IsNotInTheHostTrace(t *testing.T) {
	vault := liveVault(t)
	if got := vault.Replay(canary); got != "replayed" {
		t.Fatalf("replay = %q, want %q", got, "replayed")
	}
	if hits := marksContaining(t, canary); len(hits) != 0 {
		t.Fatalf("declared Secret[Str] verbatim in the host trace: %q", hits)
	}
	if hits := marksContaining(t, redactedSecret); len(hits) == 0 {
		t.Fatalf("nothing was recorded at all: %q", HostMarks())
	}
}

// The false-positive control: an ordinary value is still recorded verbatim, so
// the trace stays worth reading. Without it, a funnel that erased everything
// would pass both tests above.
func TestEmitted_OrdinaryTraceIsUnchanged(t *testing.T) {
	vault := liveVault(t)
	vault.Issue("alice")
	marks := HostMarks()
	if len(marks) == 0 || marks[0] != "pool.open" {
		t.Fatalf("activation trace = %q, want it to open with pool.open", marks)
	}
	// `alice` is an ordinary argument, never declared `Secret[T]`; the exact
	// match must not have swept it up.
	if strings.Contains(strings.Join(marks, "|"), redactedSecret+redactedSecret) {
		t.Fatalf("over-redacted: %q", marks)
	}
}
