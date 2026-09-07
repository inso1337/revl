// Package bridge is the cordis-go side of revl's cross-process interop bridge
// (docs/interop-bridge.md §3): a service provided in one OS process, consumed
// in another, over a Unix-domain socket.
//
// It is the transport half only — connection, newline-delimited JSON framing,
// the request/reply envelope, and peer-death monitoring. The value marshalling
// (records, ADTs, Result, Opt) is done by per-service code the emitter
// generates (backends/go/emit.py), because cordis-go services are static Go
// interfaces (like cordis-rs traits): a runtime-generic proxy is impossible, so
// generality is codegen, not reflection.
//
// The wire protocol is byte-compatible with backends/python/bridge.py: a
// request is {"key","method","args"} and a reply {"ok":true,"value":…} or
// {"ok":false,"error":…}, one JSON object per line. A Go proxy calling a Python
// stub (and vice-versa) interoperate because both speak exactly this envelope.
//
// Peer death is withdrawal (R2/R3): Monitor holds an idle second connection and
// fires its callback on EOF when the provider process goes away, so the runner
// can dispose the proxy fiber and let every dependent deactivate reactively —
// the same reactive path a local provider swap takes, now spanning processes.
package bridge

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"net"
	"sort"
	"strings"
	"sync"
	"time"

	"revl.goplacement/estop"
)

// dialAttempts / dialDelay mirror backends/python/bridge.py::_connect: under
// placement the provider and consumer start concurrently, so the socket may
// not exist yet; retrying makes start order irrelevant.
//
// Only while the PROCESS graph is acyclic. Two processes that each require a
// key the other provides both block in the dial and neither reaches its
// listen, so both die once the attempts run out. Nothing rejects that
// composition today; see docs/design/438-petri-reachability.md §5.2.
const (
	dialAttempts = 200
	dialDelay    = 50 * time.Millisecond
)

// Dial connects to a Unix socket, retrying while the provider comes up.
func Dial(path string) (net.Conn, error) {
	var last error
	for i := 0; i < dialAttempts; i++ {
		conn, err := net.Dial("unix", path)
		if err == nil {
			return conn, nil
		}
		last = err
		time.Sleep(dialDelay)
	}
	if last == nil {
		last = errors.New("bridge: could not connect to " + path)
	}
	return nil, last
}

// request is the consumer -> provider envelope. Field order (key, method, args)
// and JSON names match backends/python/bridge.py exactly.
type request struct {
	Key    string `json:"key"`
	Method string `json:"method"`
	Args   []any  `json:"args"`
}

// reply is the provider -> consumer envelope. `value` is decoded lazily by the
// generated proxy at the method's static return type; `error` carries a failure
// marshalled across the seam.
type reply struct {
	Ok    bool            `json:"ok"`
	Value json.RawMessage `json:"value"`
	Error string          `json:"error"`
}

// Client is one synchronous RPC connection to a provider stub. cordis-go
// provided methods are called synchronously, so a call is a blocking
// round-trip; peer death is observed on a separate Monitor connection.
type Client struct {
	mu   sync.Mutex
	conn net.Conn
	r    *bufio.Reader
}

// NewClient dials the stub at path (retrying) and returns a ready client.
func NewClient(path string) (*Client, error) {
	conn, err := Dial(path)
	if err != nil {
		return nil, err
	}
	return &Client{conn: conn, r: bufio.NewReader(conn)}, nil
}

// Call marshals one request, writes it, and returns the reply's raw `value`
// (or the remote error). `args` are already canonical-encoded values.
func (c *Client) Call(key, method string, args []any) (json.RawMessage, error) {
	// item 443 / issue #122 — the DISPATCH side of the E-Stop seam. Once an
	// operator arms the latch, this process stops DISPATCHING new crossings: the
	// outgoing call is REFUSED before it leaves the process, so nothing new
	// crosses the boundary. It returns an error rather than withdrawing the
	// proxy, because a halt is not a peer death: reactive withdrawal would
	// propagate a cooperative teardown to this proxy's dependents, which is
	// exactly the graceful unwind the E-Stop exists to avoid. The refused
	// caller's attempt lands in item 440's ambiguous tier, the designed outcome
	// of a halt (docs/design/443-estop.md). A go process is a consumer as well
	// as a provider, and the py design puts the check at every point that
	// dispatches OR accepts a crossing.
	if estop.EstopEngaged() {
		return nil, errors.New("revl E-Stop engaged: this process is HALTED and " +
			"refuses to dispatch new crossings (key " + key + ", method " + method +
			") — docs/design/443-estop.md")
	}
	if args == nil {
		args = []any{}
	}
	line, err := json.Marshal(request{Key: key, Method: method, Args: args})
	if err != nil {
		return nil, err
	}
	line = append(line, '\n')

	// Record the crossing as in flight for its round-trip: a crossing still out
	// when the latch trips is the AMBIGUOUS one the halt inventory names.
	seq := estop.BeginCrossing(key, method, "dispatch")
	defer estop.EndCrossing(seq)

	c.mu.Lock()
	defer c.mu.Unlock()
	if _, err := c.conn.Write(line); err != nil {
		return nil, err
	}
	respLine, err := c.r.ReadBytes('\n')
	if err != nil {
		return nil, errors.New("bridge peer closed the connection")
	}
	var rep reply
	if err := json.Unmarshal(respLine, &rep); err != nil {
		return nil, err
	}
	if !rep.Ok {
		if rep.Error == "" {
			rep.Error = "remote error"
		}
		return nil, errors.New(rep.Error)
	}
	return rep.Value, nil
}

// Close tears the RPC transport down.
func (c *Client) Close() error {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.conn.Close()
}

// Monitor dials a second, idle connection to path and calls onLost exactly once
// when the provider dies (the connection hits EOF). The monitor connection
// never sends: the stub's read loop simply sees EOF when the provider process
// (and its listener) go away.
func Monitor(path string, onLost func()) {
	conn, err := Dial(path)
	if err != nil {
		onLost()
		return
	}
	go func() {
		defer conn.Close()
		buf := make([]byte, 64)
		for {
			if _, err := conn.Read(buf); err != nil {
				break // EOF or error: the provider is gone
			}
		}
		onLost()
	}()
}

// Invoke dispatches one incoming call to a local service and returns the value
// to marshal back (or an error to report across the seam). The generated
// RevlInvoke has this shape.
type Invoke func(key, method string, args []json.RawMessage) (any, error)

// okReply / errReply are the provider's two reply shapes. Kept as explicit
// structs (not maps) so the JSON is deterministic and matches the reference.
type okReply struct {
	Ok    bool `json:"ok"`
	Value any  `json:"value"`
}

type errReply struct {
	Ok    bool   `json:"ok"`
	Error string `json:"error"`
}

// --- what a failure may carry BACK across the seam (roadmap item 421 F5) -----
//
// Mirror of backends/python/bridge.py's seam_failure and the same helper in
// backends/typescript/bridge.ts. The consumer is on the other side of a trust
// boundary: a forward crossing into a declared Secret[T] receiver authorises
// disclosure TO THE RECEIVER, and does not authorise the error channel to
// perform the reverse crossing the checker refuses statically. The trigger needs
// no author interpolation - a plain map miss produces a message quoting the key.
//
// So every argument value the call was made with is scrubbed out of the host
// error text, while the sentence around it survives: the reply still says what
// went wrong, just without the caller's bytes in it.

// RedactedArg is the placeholder a caller's own argument becomes inside seam
// error text. Must equal confidential.REDACTED_ARG on the python tier, so a
// polyglot seam produces the SAME marker whichever tier answered.
const RedactedArg = "<redacted:arg>"

// minMatchableArg is the length below which an argument is left alone: a shorter
// substring match is a coin flip against ordinary English and replacing it would
// shred the diagnostic for no confidentiality gain. Same bound as python and ts.
const minMatchableArg = 3

// argNeedles collects the string forms a decoded argument can take inside host
// error text. Booleans and null are skipped (their renderings are ordinary
// words); a record's KEYS are skipped too, because they are field names the
// author wrote rather than the caller's data.
func argNeedles(value any, into map[string]struct{}) {
	switch v := value.(type) {
	case string:
		if len(v) >= minMatchableArg {
			into[v] = struct{}{}
		}
	case json.Number:
		if len(v.String()) >= minMatchableArg {
			into[v.String()] = struct{}{}
		}
	case []any:
		for _, item := range v {
			argNeedles(item, into)
		}
	case map[string]any:
		for _, item := range v {
			argNeedles(item, into)
		}
	}
}

// SeamFailure is the error text a provider-side failure is allowed to send back
// to the consumer, with this call's own argument values replaced by RedactedArg.
// Longest needle first, so one that contains another leaves no tail behind.
func SeamFailure(err error, args []json.RawMessage) string {
	text := err.Error()
	needles := map[string]struct{}{}
	for _, raw := range args {
		decoder := json.NewDecoder(bytes.NewReader(raw))
		// UseNumber keeps an integer argument spelled the way the wire spelled
		// it, so 8675309 is matched as "8675309" and not as "8.675309e+06".
		decoder.UseNumber()
		var value any
		if decoder.Decode(&value) == nil {
			argNeedles(value, needles)
		}
	}
	ordered := make([]string, 0, len(needles))
	for needle := range needles {
		ordered = append(ordered, needle)
	}
	sort.Slice(ordered, func(i, j int) bool { return len(ordered[i]) > len(ordered[j]) })
	for _, needle := range ordered {
		text = strings.ReplaceAll(text, needle, RedactedArg)
	}
	return text
}

// Serve accepts connections on ln and answers each request via invoke until ln
// is closed. Each connection is handled on its own goroutine.
func Serve(ln net.Listener, invoke Invoke) {
	for {
		conn, err := ln.Accept()
		if err != nil {
			return // listener closed: process is shutting down
		}
		go serveConn(conn, invoke)
	}
}

func serveConn(conn net.Conn, invoke Invoke) {
	defer conn.Close()
	r := bufio.NewReader(conn)
	for {
		line, err := r.ReadBytes('\n')
		if len(line) > 0 {
			var req struct {
				Key    string            `json:"key"`
				Method string            `json:"method"`
				Args   []json.RawMessage `json:"args"`
			}
			if json.Unmarshal(line, &req) == nil {
				var out []byte
				if estop.EstopEngaged() {
					// item 443 / issue #122 — the ACCEPT side of the E-Stop seam.
					// An armed latch means an operator hit the button, so this
					// crossing is REFUSED before the service method runs: nothing
					// new crosses the boundary. The reply is an error, not a
					// value, and (unlike a cooperative teardown) no inverse is
					// replayed and nothing is discharged — the caller's attempt
					// lands in item 440's ambiguous tier, the designed outcome of
					// a halt (docs/design/443-estop.md).
					out, _ = json.Marshal(errReply{Ok: false, Error: "revl E-Stop engaged: " +
						"this process is HALTED and refuses new crossings (key " + req.Key +
						", method " + req.Method + ") — docs/design/443-estop.md"})
					out = append(out, '\n')
					if _, werr := conn.Write(out); werr != nil {
						return
					}
					if err != nil {
						return
					}
					continue
				}
				// Record the crossing as in flight WHILE its handler runs: a
				// crossing still executing when the latch trips is the AMBIGUOUS
				// one the halt inventory names (item 440). Cleared in a deferred
				// call so a panicking handler still leaves the registry clean.
				value, ierr := func() (any, error) {
					seq := estop.BeginCrossing(req.Key, req.Method, "accept")
					defer estop.EndCrossing(seq)
					return invoke(req.Key, req.Method, req.Args)
				}()
				if ierr != nil {
					// item 421 F5: never hand the consumer back the values it
					// called with.
					out, _ = json.Marshal(errReply{Ok: false, Error: SeamFailure(ierr, req.Args)})
				} else {
					out, _ = json.Marshal(okReply{Ok: true, Value: value})
				}
				out = append(out, '\n')
				if _, werr := conn.Write(out); werr != nil {
					return
				}
			}
		}
		if err != nil {
			return // EOF (monitor connection) or a read error
		}
	}
}
