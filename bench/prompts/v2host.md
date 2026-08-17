# Addendum — host blocks (`extern`)

(Everything in the 2.0 grammar above applies unchanged. This variant adds one
construct.)

## Host blocks — full host-language power, honestly labeled

An `extern` declares a function whose body is *verbatim host code*
(TypeScript and/or Python). The boundary is typed; the inside is unchecked;
the whole construct sits on the audit surface.

```revl
extern pure fn sha256(data: Str) -> Str
  = @ts { return require("crypto").createHash("sha256").update(data).digest("hex") }
  = @py { import hashlib; return hashlib.sha256(data.encode()).hexdigest() }

extern acquire fn listen(port: Int) -> Socket undo close(socket)
  = @ts { /* ... real TS ... */ } = @py { # ... real Python ... }

extern emission fn send(sock: Socket, data: Str)
  compensate log_unsent(sock, data)
  = @ts { /* ... */ } = @py { # ... }
```

- Classification is **mandatory and semantic**:
  - `pure` — no observable effect (trusted, audited),
  - `acquire` — must carry `undo`,
  - `emission` — may carry `compensate`.
  An unclassified extern does not parse.
- Give both `@ts` and `@py` bodies when you can — each body makes the extern
  portable to that backend.
- Inside a host block, write *real* TypeScript or Python — this is exactly
  the place full host fluency is wanted.
- Call an `extern pure fn` from anywhere an ordinary `fn` is callable
  (including component expression positions).

**Guidance for this task:** when the task involves host-level computation
(hashing, encoding, time, randomness, parsing beyond the stdlib), prefer a
small `extern pure fn` with real host bodies over contorting the pure
subset.
