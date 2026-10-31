package bridge

import (
	"bufio"
	"encoding/json"
	"net"
	"testing"
)

// issue #2481: a served method that panics answers the crossing with an error
// reply, the way a py, ts or java provider that raises does, and the connection
// keeps serving. The panic value goes through the same funnel as a returned
// error, so a registered value it quotes is redacted on the raw reply.
func TestAPanickingHandlerRepliesWithAnErrorAndKeepsServing(t *testing.T) {
	installScrub(t, canary)
	path := socketPath(t)
	ln, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go Serve(ln, func(key, method string, args []json.RawMessage) (any, error) {
		if method == "boom" {
			panic("boom " + canary)
		}
		return "fine", nil
	})
	conn, err := Dial(path)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	r := bufio.NewReader(conn)
	send := func(method string) []byte {
		line, err := json.Marshal(request{Key: "vault", Method: method, Args: []any{"u1"}})
		if err != nil {
			t.Fatal(err)
		}
		if _, err := conn.Write(append(line, '\n')); err != nil {
			t.Fatal(err)
		}
		return readLine(t, r)
	}

	var rep errReply
	if err := json.Unmarshal(send("boom"), &rep); err != nil {
		t.Fatal(err)
	}
	if rep.Ok {
		t.Fatalf("expected an error reply, got ok")
	}
	if want := "panic: boom " + redactedSecret; rep.Error != want {
		t.Fatalf("expected %q, got %q", want, rep.Error)
	}

	var ok okReply
	if err := json.Unmarshal(send("show"), &ok); err != nil {
		t.Fatal(err)
	}
	if !ok.Ok || ok.Value != "fine" {
		t.Fatalf("the connection stopped serving after the panic: %+v", ok)
	}
}
