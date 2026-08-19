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
	"encoding/json"
	"errors"
	"net"
	"sync"
	"time"
)

// dialAttempts / dialDelay mirror backends/python/bridge.py::_connect: under
// placement the provider and consumer start concurrently, so the socket may
// not exist yet; retrying makes start order irrelevant.
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
	if args == nil {
		args = []any{}
	}
	line, err := json.Marshal(request{Key: key, Method: method, Args: args})
	if err != nil {
		return nil, err
	}
	line = append(line, '\n')

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
				value, ierr := invoke(req.Key, req.Method, req.Args)
				if ierr != nil {
					out, _ = json.Marshal(errReply{Ok: false, Error: ierr.Error()})
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
