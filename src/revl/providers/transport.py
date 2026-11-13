"""The one HTTP client revl uses to reach a model endpoint (issue #1461).

Every request to a model provider in this tree goes through `request_json`:
the runtime adapters in this package, and the `bench/` tools that used to carry
three private copies of the same urllib code. One client means one redirect
policy, one error shape and one place where a credential is kept out of a
message.

Standard library only. revl has no required runtime dependency, and a model
call is a POST with a JSON body, which `urllib` does without help.

THE FIVE RULES THIS FILE ENFORCES
---------------------------------
1. **No redirect is followed.** urllib follows a 301/302/303 by default and
   re-issues the POST as a GET, body dropped, at whatever host `Location`
   names. The endpoint is part of what the operator bound a model role to, and
   the placement check in `revl.providers.placement` judged THAT host. A
   redirect would move the prompt somewhere the check never saw, so it is a
   refusal, the same policy `revl.crossing_redirect` gives the emitted
   crossings.
2. **No ambient proxy is used.** urllib's default `ProxyHandler` reads
   `http_proxy`, `https_proxy` and friends from the environment. For a loopback
   endpoint that would send the prompt and its Authorization header to the
   proxy host, while `endpoint_residence` and `_check_url` judged the bytes to
   stay on this machine (issue #3109). The opener carries an empty proxy map,
   so a model request goes to the endpoint named by `base_url` and nowhere
   else. To route through a proxy, bind the role to the proxy's URL and
   declare its residence.
3. **A credential never reaches a message.** The caller passes every secret it
   put in a header as `secrets`, and every string this module raises is passed
   through `redact` first, including the provider's own error body, which is
   where a key most often comes back: several providers echo the rejected key
   in a 401. The urllib exception is not chained (`from None`), so a traceback
   does not carry the request object either.
4. **Only a JSON object is a response.** Anything else is a named error rather
   than a value the caller has to guess about. That includes JSON the decoder
   itself refuses (an integer of thousands of digits, nesting deeper than the
   interpreter's stack): each is a `ProviderError`, never the raw exception.
5. **A reply is bounded in size and in time** (issue #3111). The endpoint is
   whatever the operator bound the role to, and it may be a shared server or a
   compromised proxy. At most `max_bytes` of the reply are read; a larger one,
   declared by `Content-Length` or found by counting, is refused without being
   held in memory. `timeout` is also the deadline for the exchange once the
   connection is up, not only for each socket operation, so a server that
   sends a byte at a time cannot hold the call open: when it passes, the
   connection is shut down and the call fails with a timeout. Name lookup, the
   TCP connect and the TLS handshake are bounded per operation by `timeout`.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import urllib.error
import urllib.request

#: The text a redacted secret is replaced with.
REDACTED = "[redacted]"

#: How much of a provider's error body a message quotes. Enough to read the
#: provider's reason, not so much that a message becomes a dump of the reply.
ERROR_BODY_LIMIT = 400

#: The most bytes of one reply that are read, unless the caller names another
#: limit (a binding's `max_reply_bytes`). A completion is a few kilobytes; this
#: leaves room for a long one while keeping a hostile reply from holding more
#: than this in memory.
DEFAULT_MAX_REPLY_BYTES = 8 * 1024 * 1024

_READ_CHUNK = 64 * 1024


class ProviderError(RuntimeError):
    """A model endpoint could not be reached, refused the request, or answered
    with something that is not a completion.

    `status` is the HTTP status when there was one. The message is already
    redacted when this is raised; nothing on the instance holds a credential.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _RedirectRefused(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _RedirectRefused(code)


class _TooLarge(Exception):
    pass


def _shutdown(sock) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class _Deadline:
    """An overall time limit on one request.

    urllib's `timeout` bounds each socket operation, so a server that answers
    a byte at a time never trips it. This arms a timer for the connected exchange;
    when it fires it shuts down the request's sockets, which wakes any blocked
    read, and `expired` tells the caller why the read failed.
    """

    def __init__(self, seconds: float) -> None:
        self.expired = False
        self._lock = threading.Lock()
        self._socks: list = []
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True

    def start(self) -> None:
        self._timer.start()

    def cancel(self) -> None:
        self._timer.cancel()

    def watch(self, sock) -> None:
        with self._lock:
            self._socks.append(sock)
            late = self.expired
        if late:
            _shutdown(sock)

    def _fire(self) -> None:
        with self._lock:
            self.expired = True
            socks = list(self._socks)
        for sock in socks:
            _shutdown(sock)


def _watched(base, deadline: _Deadline):
    class Watched(base):
        def connect(self):
            super().connect()
            deadline.watch(self.sock)
    return Watched


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, deadline: _Deadline) -> None:
        super().__init__()
        self._connection = _watched(http.client.HTTPConnection, deadline)

    def http_open(self, req):
        return self.do_open(self._connection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, deadline: _Deadline) -> None:
        super().__init__()
        self._connection = _watched(http.client.HTTPSConnection, deadline)

    def https_open(self, req):
        return self.do_open(self._connection, req,
                            context=getattr(self, "_context", None))


def _read_capped(response, limit: int) -> bytes:
    """The body of `response`, or `_TooLarge` as soon as it exceeds `limit`
    bytes. A declared `Content-Length` over the limit is refused unread."""
    declared = response.headers.get("Content-Length")
    # ASCII only: `str.isdigit` is also true of "\u00b2", which `int` refuses,
    # and http.client decodes header bytes as latin-1
    length = (int(declared) if declared is not None and declared.isascii()
              and declared.strip().isdigit() else None)
    if length is not None and length > limit:
        raise _TooLarge
    chunks: list = []
    total = 0
    while True:
        chunk = response.read(min(_READ_CHUNK, limit + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise _TooLarge
        chunks.append(chunk)
    if length is not None and total < length:
        # `read(amt)` ends quietly at EOF; a whole-body read would have raised
        raise http.client.IncompleteRead(b"".join(chunks), length - total)
    return b"".join(chunks)


def redact(text: str, secrets=()) -> str:
    """`text` with every non-empty secret in `secrets` replaced by `[redacted]`.

    Secrets shorter than four characters are not treated as secrets: replacing
    every `a` in a message would make it unreadable and protect nothing a real
    credential needs.
    """
    out = str(text)
    for secret in secrets or ():
        if secret and len(secret) >= 4:
            out = out.replace(secret, REDACTED)
    return out


def request_json(url: str, *, body: dict | None = None,
                 headers: dict | None = None, timeout: float = 120.0,
                 secrets=(), label: str = "model endpoint",
                 max_bytes: int = DEFAULT_MAX_REPLY_BYTES) -> dict:
    """POST `body` as JSON to `url` (GET when `body` is None) and return the
    decoded JSON object.

    `timeout` is in seconds. It is the limit on each socket operation, and the
    deadline for the request and reply once the connection is up. `max_bytes` is the most of the reply that
    is read; a larger reply is a `ProviderError`.

    `secrets` are the credential values placed in `headers`; they are redacted
    from every error this raises. `label` names the caller in those errors
    (`anthropic adapter for role cloud`, `model pin`), so a reader knows which
    configuration produced the request.
    """
    data = None if body is None else json.dumps(body).encode("utf-8")
    send = dict(headers or {})
    if data is not None:
        send.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        url, data=data, method="GET" if data is None else "POST",
        headers=send)
    # An empty proxy map replaces urllib's default ProxyHandler, so the
    # environment's http_proxy/https_proxy/all_proxy cannot route this request
    # through a proxy (issue #3109). The endpoint the placement check judged is
    # the only host contacted.
    # The two handlers carry the request's deadline (rule 5) into the sockets.
    deadline = _Deadline(timeout)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect,
        _HTTPHandler(deadline), _HTTPSHandler(deadline))

    def fail(message: str, status: int | None = None) -> ProviderError:
        return ProviderError(redact(f"{label}: {message}", secrets),
                             status=status)

    def timed_out() -> ProviderError:
        return fail(f"timed out after {timeout}s waiting for {url}")

    deadline.start()
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            raw = _read_capped(response, max_bytes)
    except _RedirectRefused as exc:
        raise fail(f"HTTP {exc.code} redirect refused: {url} is the endpoint "
                   f"this role is bound to, and a redirect would send the "
                   f"prompt to a host the placement check never judged",
                   exc.code) from None
    except _TooLarge:
        raise fail(f"the reply from {url} is larger than {max_bytes} bytes and "
                   f"was refused; raise `max_reply_bytes` on the role's "
                   f"binding if a reply this large is expected") from None
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(ERROR_BODY_LIMIT).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - the body is best-effort context
            detail = ""
        raise fail(f"HTTP {exc.code} from {url}: {detail}", exc.code) from None
    except urllib.error.URLError as exc:
        raise (timed_out() if deadline.expired else
               fail(f"cannot reach {url}: {exc.reason}")) from None
    except TimeoutError:
        raise timed_out() from None
    except OSError as exc:
        raise (timed_out() if deadline.expired else
               fail(f"connection to {url} failed: {exc}")) from None
    except http.client.HTTPException as exc:
        # not an OSError: a body shorter than its Content-Length
        # (IncompleteRead), a status line that is not HTTP (BadStatusLine)
        raise (timed_out() if deadline.expired else
               fail(f"malformed HTTP response from {url}: "
                    f"{type(exc).__name__}")) from None
    finally:
        deadline.cancel()
    if deadline.expired:
        # the connection was shut mid-body and the read ended early, quietly
        raise timed_out()

    if status != 200:
        raise fail(f"HTTP {status} from {url}: "
                   f"{raw[:ERROR_BODY_LIMIT].decode('utf-8', 'replace')}",
                   status)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - see rule 4
        # not only JSONDecodeError: an integer of thousands of digits is a
        # ValueError and deep nesting a RecursionError
        raise fail(f"unparseable JSON from {url}: {type(exc).__name__}: "
                   f"{exc}") from None
    if not isinstance(parsed, dict):
        raise fail(f"{url} answered with a JSON {type(parsed).__name__}, "
                   f"not an object")
    return parsed
