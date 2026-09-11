package bridge

import (
	"bufio"
	"encoding/json"
	"errors"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// canary is the value the composition is trusted with in these tests. The
// registry that knows it lives in the generated `emitted` package, which imports
// this one, so the tests install the same hook the generated `init` installs.
const canary = "SEKRIT-CANARY-421-F5F6"

// redactedSecret is the generated registry's RevlRedactedSecret placeholder,
// spelled locally on purpose: importing it would make the test assert the
// symbol rather than the behaviour on the wire, and the python tier's suite
// spells its own copy for the same reason.
const redactedSecret = "<redacted:secret>"

// installScrub stands in for `func init() { bridge.SecretScrub = revlRedactText }`,
// which the emitter emits into the generated bridge half when a composition
// declares a `Secret[T]`. Longest needle first, as the generated registry does.
func installScrub(t *testing.T, values ...string) {
	t.Helper()
	ordered := append([]string(nil), values...)
	for i := 0; i < len(ordered); i++ {
		for j := i + 1; j < len(ordered); j++ {
			if len(ordered[j]) > len(ordered[i]) {
				ordered[i], ordered[j] = ordered[j], ordered[i]
			}
		}
	}
	SecretScrub = func(text string) string {
		for _, v := range ordered {
			text = strings.ReplaceAll(text, v, redactedSecret)
		}
		return text
	}
	t.Cleanup(func() { SecretScrub = nil })
}

func raw(value string) json.RawMessage {
	out, err := json.Marshal(value)
	if err != nil {
		panic(err)
	}
	return out
}

func socketPath(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "br")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	return filepath.Join(dir, "s.sock")
}

// --- stage 1: the caller's own bytes, which already worked ------------------

func TestSeamFailureScrubsTheCallersOwnArguments(t *testing.T) {
	text := SeamFailure(errors.New(`no such key "tok-9f1e2d3c" in map`),
		[]json.RawMessage{raw("tok-9f1e2d3c")})
	if strings.Contains(text, "tok-9f1e2d3c") {
		t.Fatalf("argument survived the funnel: %q", text)
	}
	if !strings.Contains(text, "no such key") || !strings.Contains(text, "in map") {
		t.Fatalf("the sentence around the argument was lost: %q", text)
	}
	if !strings.Contains(text, RedactedArg) {
		t.Fatalf("stage 1 marker missing: %q", text)
	}
}

// --- stage 2: the fix. A value the failure was NOT called with. -------------

func TestSeamFailureScrubsARegisteredValueItWasNotCalledWith(t *testing.T) {
	installScrub(t, canary)
	text := SeamFailure(errors.New("dial tcp 10.0.0.7:443: auth failed presenting "+canary),
		nil)
	if strings.Contains(text, canary) {
		t.Fatalf("registered secret survived the funnel: %q", text)
	}
	if !strings.Contains(text, redactedSecret) {
		t.Fatalf("stage 2 marker missing: %q", text)
	}
	if !strings.Contains(text, "dial tcp 10.0.0.7:443: auth failed presenting") {
		t.Fatalf("the diagnostic was shredded instead of redacted: %q", text)
	}
}

func TestSeamFailureIsUnchangedWithoutARegistry(t *testing.T) {
	// A composition declaring no `Secret[T]` anywhere installs no hook: nothing
	// was registered, so stage 2 has nothing to remove and the text must come
	// back byte-identical to what the host produced.
	text := SeamFailure(errors.New("auth failed presenting "+canary), nil)
	if !strings.Contains(text, canary) {
		t.Fatalf("a marking-free placement must be unchanged: %q", text)
	}
}

func TestScrubTextIsIdentityWithoutARegistry(t *testing.T) {
	if got := ScrubText("plain diagnostic " + canary); got != "plain diagnostic "+canary {
		t.Fatalf("ScrubText must be identity with a nil hook, got %q", got)
	}
}

func TestSeamFailureRunsBothStagesAndKeepsThemDistinguishable(t *testing.T) {
	installScrub(t, canary)
	arg := "arg-7a3b5c9d"
	text := SeamFailure(errors.New("store refused "+arg+" while holding "+canary),
		[]json.RawMessage{raw(arg)})
	if strings.Contains(text, arg) || strings.Contains(text, canary) {
		t.Fatalf("one of the two stages did not run: %q", text)
	}
	if !strings.Contains(text, RedactedArg) || !strings.Contains(text, redactedSecret) {
		t.Fatalf("both markers must be present and distinct: %q", text)
	}
}

func TestArgFloorIsSharedWithTheOtherTiers(t *testing.T) {
	// Below the bound a substring match is a coin flip against ordinary English,
	// so a short argument is left alone. Same bound as python and ts: a message
	// that happens to contain two adjacent letters keeps its wording.
	text := SeamFailure(errors.New("bad value ab here"), []json.RawMessage{raw("ab")})
	if !strings.Contains(text, "bad value ab here") {
		t.Fatalf("a sub-bound argument must not be redacted: %q", text)
	}
}

// --- the funnel as it is actually reached: over the wire --------------------

func readLine(t *testing.T, r *bufio.Reader) []byte {
	t.Helper()
	line, err := r.ReadBytes('\n')
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	return line
}

// The provider half: a failure raised by the served method, quoting a value the
// composition holds but the call did not carry. Asserted on the RAW reply, so a
// pass is the provider's own funnel and not the consumer's arrival scrub.
func TestWireReplyDoesNotCarryARegisteredValue(t *testing.T) {
	installScrub(t, canary)
	path := socketPath(t)
	ln, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go Serve(ln, func(key, method string, args []json.RawMessage) (any, error) {
		return nil, errors.New("upstream rejected the session on " + canary)
	})
	conn, err := Dial(path)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	line, err := json.Marshal(request{Key: "vault", Method: "open", Args: []any{"u1"}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := conn.Write(append(line, '\n')); err != nil {
		t.Fatal(err)
	}
	var rep errReply
	if err := json.Unmarshal(readLine(t, bufio.NewReader(conn)), &rep); err != nil {
		t.Fatal(err)
	}
	if rep.Ok {
		t.Fatalf("expected an error reply, got ok")
	}
	if strings.Contains(rep.Error, canary) {
		t.Fatalf("the raw reply carried the registered value: %q", rep.Error)
	}
	if !strings.Contains(rep.Error, redactedSecret) ||
		!strings.Contains(rep.Error, "upstream rejected the session on") {
		t.Fatalf("expected a redacted diagnostic, got %q", rep.Error)
	}
}

// The consumer half: a provider authored outside revl (spec §10) has no registry
// to consult, so the text it sends back can quote a value THIS process holds.
// Nothing the caller passed is involved, which is what makes it stage 2.
func TestClientRedactsTextFromAProviderWithNoRegistry(t *testing.T) {
	installScrub(t, canary)
	path := socketPath(t)
	ln, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		r := bufio.NewReader(conn)
		if _, err := r.ReadBytes('\n'); err != nil {
			return
		}
		out, err := json.Marshal(errReply{Ok: false, Error: "peer says the token " + canary + " is stale"})
		if err != nil {
			return
		}
		conn.Write(append(out, '\n'))
	}()
	client, err := NewClient(path)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	_, err = client.Call("vault", "open", []any{"u1"})
	if err == nil {
		t.Fatal("expected the peer's error to reach the caller")
	}
	if strings.Contains(err.Error(), canary) {
		t.Fatalf("the consumer surfaced an unredacted peer error: %q", err.Error())
	}
	if !strings.Contains(err.Error(), redactedSecret) ||
		!strings.Contains(err.Error(), "peer says the token") {
		t.Fatalf("expected a redacted diagnostic, got %q", err.Error())
	}
}

// Without a registry the peer's text arrives as sent: the arrival scrub must not
// start rewriting diagnostics for a placement that declared no marking.
func TestClientPassesTextThroughWithoutARegistry(t *testing.T) {
	path := socketPath(t)
	ln, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		r := bufio.NewReader(conn)
		if _, err := r.ReadBytes('\n'); err != nil {
			return
		}
		out, err := json.Marshal(errReply{Ok: false, Error: "peer says the token " + canary + " is stale"})
		if err != nil {
			return
		}
		conn.Write(append(out, '\n'))
	}()
	client, err := NewClient(path)
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	if _, err := client.Call("vault", "open", []any{"u1"}); err == nil {
		t.Fatal("expected the peer's error to reach the caller")
	} else if !strings.Contains(err.Error(), canary) {
		t.Fatalf("a marking-free placement must pass the text through: %q", err.Error())
	}
}
