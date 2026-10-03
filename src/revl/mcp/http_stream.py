"""Streams for MCP over HTTP (issue #1463, slice 2).

MCP revision 2026-07-28 gives an HTTP server two ways to send more than one
message for a request, and this module is both of them:

  * **An SSE response on `POST /mcp`.** A request's reply may be a
    `text/event-stream` that carries notifications about THAT request, then the
    response. The only such notification this server relays is
    `notifications/progress` from a proxied upstream. The response starts as an
    SSE stream only when the first progress event arrives; a request that gets
    none is answered with plain JSON, as in slice 1.
  * **The `subscriptions/listen` stream.** A long-lived SSE response that
    carries the change notifications the caller opted in to:
    `notifications/tools/list_changed` and friends, and
    `notifications/resources/updated` for the URIs the caller named.

Who sees what is the point of the module:

  * A progress event reaches only the request it belongs to. The caller's
    `progressToken` is replaced, on the way to the upstream, by a token this
    server mints for that one request, and mapped back on the way out. Two
    callers choosing the same token, or a late event from a finished request,
    therefore reach nobody else.
  * An event on a listen stream reaches only a stream whose filter asked for
    it, and only while the caller that opened the stream still authenticates
    as the same operator. The credential is checked again before every event
    and every keep-alive; while the operator profile is settling the event is
    held, and a caller who no longer authenticates has the stream closed with
    nothing more sent.
  * Nothing here takes the dispatch lock to deliver an event. A listen stream
    takes it only to subscribe or unsubscribe its URIs upstream.

docs/mcp-http-transport.md is the user-facing account.
"""

from __future__ import annotations

import copy
import json
import queue
import secrets
import select
import socket
import threading

SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"
PROGRESS = "notifications/progress"
RESOURCE_UPDATED = "notifications/resources/updated"
# notification method -> the filter flag that opts in to it
LIST_CHANGED = {
    "notifications/tools/list_changed": "toolsListChanged",
    "notifications/prompts/list_changed": "promptsListChanged",
    "notifications/resources/list_changed": "resourcesListChanged",
}
FLAGS = tuple(LIST_CHANGED.values())
RESOURCES = "resourceSubscriptions"

MAX_STREAMS_PER_OPERATOR = 4
MAX_STREAMS = 32
MAX_RESOURCE_SUBSCRIPTIONS = 64
QUEUE_LIMIT = 256
WRITE_TIMEOUT_S = 10.0

_SSE_HEAD = (b"HTTP/1.1 200 OK\r\n"
             b"Content-Type: text/event-stream\r\n"
             b"Cache-Control: no-cache, no-store\r\n"
             b"X-Accel-Buffering: no\r\n"
             b"Connection: close\r\n\r\n")


def accepts_sse(headers) -> bool:
    """Does the request's `Accept` list `text/event-stream` (not at q=0)?"""
    getter = getattr(headers, "get_all", None)
    values = list(getter("Accept") or []) if getter else [headers.get("Accept") or ""]
    for value in values:
        for item in str(value).split(","):
            kind, *params = [p.strip() for p in item.split(";")]
            if kind.lower() != "text/event-stream":
                continue
            refused = any(p.replace(" ", "").lower() in ("q=0", "q=0.0", "q=0.00",
                                                         "q=0.000")
                          for p in params)
            if not refused:
                return True
    return False


def listen_support(capabilities: dict | None) -> dict:
    """What a listen stream can honor, from the capabilities the dispatcher
    declares: a flag per list, and whether resources can be subscribed."""
    caps = capabilities if isinstance(capabilities, dict) else {}

    def flag(section, key):
        value = caps.get(section)
        return isinstance(value, dict) and value.get(key) is True

    return {"toolsListChanged": flag("tools", "listChanged"),
            "promptsListChanged": flag("prompts", "listChanged"),
            "resourcesListChanged": flag("resources", "listChanged"),
            RESOURCES: flag("resources", "subscribe")}


def filter_refusal(notifications) -> str | None:
    """Why a `subscriptions/listen` filter is malformed, or None."""
    if not isinstance(notifications, dict):
        return "subscriptions/listen needs a `notifications` object"
    for flag in FLAGS:
        if flag in notifications and not isinstance(notifications[flag], bool):
            return f"`notifications.{flag}` must be a boolean"
    uris = notifications.get(RESOURCES)
    if uris is not None:
        if not isinstance(uris, list) or not all(isinstance(u, str) and u for u in uris):
            return f"`notifications.{RESOURCES}` must be a list of URIs"
        if len(set(uris)) > MAX_RESOURCE_SUBSCRIPTIONS:
            return (f"at most {MAX_RESOURCE_SUBSCRIPTIONS} resource subscriptions "
                    f"per stream")
    return None


def _tagged(message: dict, subscription_id) -> dict:
    out = copy.deepcopy(message)
    params = out.get("params")
    params = dict(params) if isinstance(params, dict) else {}
    meta = dict(params.get("_meta") or {}) if isinstance(params.get("_meta"), dict) \
        else {}
    meta[SUBSCRIPTION_ID] = subscription_id
    params["_meta"] = meta
    out["params"] = params
    return out


# ---------------------------------------------------------------- the wire

class SseWriter:
    """Raw SSE on one connection. Writes from any thread, under one lock; a
    write that fails or stalls past `WRITE_TIMEOUT_S` kills the stream, and
    every later write is dropped."""

    def __init__(self, handler) -> None:
        self._handler = handler
        self._lock = threading.Lock()
        self.started = False
        self.dead = False

    def _raw(self, data: bytes) -> bool:
        with self._lock:
            if self.dead:
                return False
            try:
                if not self.started:
                    connection = getattr(self._handler, "connection", None)
                    if connection is not None:
                        connection.settimeout(WRITE_TIMEOUT_S)
                    self._handler.wfile.write(_SSE_HEAD)
                    self.started = True
                self._handler.wfile.write(data)
                self._handler.wfile.flush()
                return True
            except (OSError, ValueError):
                self.dead = True
                return False

    def event(self, message: dict) -> bool:
        return self._raw(b"event: message\ndata: "
                         + json.dumps(message).encode("utf-8") + b"\n\n")

    def comment(self) -> bool:
        return self._raw(b":\n\n")

    def open(self) -> bool:
        return self._raw(b"")


def _readable_now(connection) -> bool:
    """Is there something to read (data, or the peer's FIN) right now?

    `poll`, not `select`: `select()` refuses a descriptor at or above
    FD_SETSIZE (1024) with `ValueError`, so a process holding that many files
    read every connection as closed and ended every listen stream at its first
    quiet poll (issue #1716; it reddened `frontend-cordis`, whose full suite
    holds more than 1024 descriptors by the time it reaches the stream tests).
    `select` stays only where `poll` does not exist."""
    poll = getattr(select, "poll", None)
    if poll is None:  # pragma: no cover - Windows has no poll
        readable, _, _ = select.select([connection], [], [], 0)
        return bool(readable)
    poller = poll()
    poller.register(connection, select.POLLIN | select.POLLPRI
                    | select.POLLHUP | select.POLLERR)
    return bool(poller.poll(0))


def peer_closed(connection) -> bool:
    """Has the client closed the connection? Plain sockets only; under TLS a
    closed peer is found by the next write instead."""
    if connection is None or hasattr(connection, "getpeercert"):
        return False
    try:
        if not _readable_now(connection):
            return False
        return connection.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


class RequestStream:
    """The reply to one `POST /mcp` request: plain JSON, unless a notification
    for the request arrives while it is dispatched, which turns it into SSE.
    After `finish`, nothing more is sent for the request."""

    def __init__(self, handler, *, enabled: bool) -> None:
        self.enabled = enabled
        self._writer = SseWriter(handler)
        self._lock = threading.Lock()
        self._done = False

    @property
    def streaming(self) -> bool:
        return self._writer.started

    def notify(self, message: dict) -> None:
        with self._lock:
            if self._done or not self.enabled:
                return
            self._writer.event(message)

    def finish(self, body: dict | None) -> bool:
        """End the request. True when the reply was already SSE, and `body`
        went out as its last event; False to answer with plain JSON."""
        with self._lock:
            self._done = True
            if not self._writer.started:
                return False
            if body is not None:
                self._writer.event(body)
            return True


class ProgressRoutes:
    """Outbound progress tokens: minted per request, mapped back on the way in."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._routes: dict[str, tuple] = {}

    def bind(self, client_token, stream: RequestStream) -> str:
        token = f"revl-progress-{secrets.token_hex(12)}"
        with self._lock:
            self._routes[token] = (client_token, stream)
        return token

    def release(self, token: str) -> None:
        with self._lock:
            self._routes.pop(token, None)

    def route(self, message: dict) -> None:
        params = message.get("params")
        if not isinstance(params, dict):
            return
        with self._lock:
            found = self._routes.get(params.get("progressToken"))
        if found is None:
            return  # no request in flight owns it: delivered to nobody
        client_token, stream = found
        out = copy.deepcopy(message)
        out["params"]["progressToken"] = client_token
        stream.notify(out)


class Subscription:
    """One open `subscriptions/listen` stream."""

    def __init__(self, subscription_id, operator_token: str, headers, peer,
                 honored: dict) -> None:
        self.id = subscription_id
        self.token = operator_token
        self.headers = headers
        self.peer = peer
        self.flags = {flag for flag in FLAGS if honored.get(flag)}
        self.uris: list[str] = list(honored.get(RESOURCES) or [])
        self.queue: queue.Queue = queue.Queue(maxsize=QUEUE_LIMIT)
        self.overflowed = False
        self.shutting_down = threading.Event()
        self.ended = threading.Event()

    def offer(self, message: dict) -> None:
        try:
            self.queue.put_nowait(message)
        except queue.Full:
            # a caller that does not read cannot hold the upstream reader up;
            # its stream is closed instead, and it may listen again
            self.overflowed = True

    def acknowledgement(self) -> dict:
        honored = {flag: True for flag in FLAGS if flag in self.flags}
        if self.uris:
            honored[RESOURCES] = list(self.uris)
        return {"jsonrpc": "2.0", "method": "notifications/subscriptions/acknowledged",
                "params": {"_meta": {SUBSCRIPTION_ID: self.id},
                           "notifications": honored}}


class EventHub:
    """Routes every notification a dispatcher emits to the streams that may
    carry it, and to nothing else."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscriptions: list[Subscription] = []
        self._uri_refs: dict[str, int] = {}
        self.progress = ProgressRoutes()
        self.closed = False

    # -- membership ----------------------------------------------------------

    def admit(self, operator_token: str) -> str | None:
        with self._lock:
            if self.closed:
                return "the server is shutting down"
            if len(self._subscriptions) >= MAX_STREAMS:
                return f"at most {MAX_STREAMS} listen streams are open at once"
            mine = sum(1 for s in self._subscriptions if s.token == operator_token)
            if mine >= MAX_STREAMS_PER_OPERATOR:
                return (f"at most {MAX_STREAMS_PER_OPERATOR} listen streams per "
                        f"operator are open at once")
        return None

    def add(self, subscription: Subscription) -> bool:
        with self._lock:
            if self.closed:
                return False
            self._subscriptions.append(subscription)
            return True

    def remove(self, subscription: Subscription) -> None:
        with self._lock:
            if subscription in self._subscriptions:
                self._subscriptions.remove(subscription)

    def subscribers(self) -> list[Subscription]:
        with self._lock:
            return list(self._subscriptions)

    def uri_refs(self, uri: str) -> int:
        with self._lock:
            return self._uri_refs.get(uri, 0)

    def hold_uri(self, uri: str) -> None:
        with self._lock:
            self._uri_refs[uri] = self._uri_refs.get(uri, 0) + 1

    def drop_uri(self, uri: str) -> bool:
        """Release one hold; True when it was the last."""
        with self._lock:
            left = self._uri_refs.get(uri, 0) - 1
            if left > 0:
                self._uri_refs[uri] = left
                return False
            self._uri_refs.pop(uri, None)
            return True

    def close_all(self) -> list[Subscription]:
        with self._lock:
            self.closed = True
            open_now = list(self._subscriptions)
        for subscription in open_now:
            subscription.shutting_down.set()
        return open_now

    # -- routing -------------------------------------------------------------

    def route(self, message) -> None:
        """One notification from the dispatcher. Anything that is not a
        notification this server relays is dropped."""
        if not isinstance(message, dict) or "id" in message:
            return
        method = message.get("method")
        if method == PROGRESS:
            self.progress.route(message)
            return
        flag = LIST_CHANGED.get(method)
        uri = None
        if method == RESOURCE_UPDATED:
            params = message.get("params")
            uri = params.get("uri") if isinstance(params, dict) else None
            if not isinstance(uri, str):
                return
        elif flag is None:
            return  # notifications/message and the rest: not relayed over HTTP
        for subscription in self.subscribers():
            if (flag is not None and flag in subscription.flags) \
                    or (uri is not None and uri in subscription.uris):
                subscription.offer(_tagged(message, subscription.id))
