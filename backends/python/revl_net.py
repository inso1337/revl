"""Session-neutral HTTPS request engine for the `@py` bodies of stdlib/net.rvl
(plan slice R1, the outbound client primitive).

This is the py-tier support module the `@py` extern bodies in `stdlib/net.rvl`
and in a per-host wrapper import. The emitted module runs with `backends/python`
on `sys.path` (`src/revl/test.py`, `backends/python/loader.py`), so a body can
`import revl_net` and reach `perform`. Other tiers get no body at all: the
extern refuses at emit time, naming the backend (`stdlib/auth.rvl`'s pattern).

# Why one implementation behind a thin wire record

`perform(call)` is the ONLY function that touches the network. A generic call
and a per-host wrapper both hand it the same `NetCall` record (a plain dict on
the emitted py tier), so the HTTP semantics - HTTPS-only, no redirects, timeout,
bounded body, redaction - are enforced in exactly one place instead of being
re-derived per wrapper. The crossing carries one extra fact: `expected_host`.
When it is passed, the runtime refuses a URL whose host is not that value, which
is what makes the declared `net(host=...)` capability an ENFORCED destination
rather than prose.

# Errors are data, never exceptions

`perform` always returns the `NetReply` dict shape and never raises. Every
refusal is a `{"ok": False, "code": ..., "message": ...}` reply; the message
names the failure class and at most the host. It never contains a header value,
a request body, a URL path or a URL query string, so an `Authorization` token
cannot leak into a log line or an exception traceback through this module.
Unexpected host faults are folded into `internal_error` with the exception class
name only.

# Deliberate limits (documented in the module header as well)

  * HTTPS only. Plain `http://` is refused before a socket is opened.
  * URLs carrying userinfo (`https://user:pass@host/`) are refused: the module
    will not put a secret in a URL even when the caller does.
  * No redirect following. `http.client` never follows, so a 3xx is returned as
    an ordinary response with its status and `Location` header.
  * No retries, no proxy support, no cookies, no client certificates, no
    streaming, no decompression. `Accept-Encoding: identity` is sent by default
    and a caller-supplied `Accept-Encoding` is refused so the returned bytes are
    not silently compressed.
  * One timeout per socket operation, not a whole-request deadline.
  * `max_bytes` bounds the RESPONSE BODY only; response headers are bounded by
    `http.client`'s own cap (100 headers) and are not counted against it.
  * Bodies are text: request bodies are sent as UTF-8, responses must decode as
    strict UTF-8 (a non-UTF-8 body is `response_not_utf8`, never a lossy
    replacement).
  * Only `GET` and `POST`. The wire record carries a method string, and the
    public builders are the typed way to fill it.
"""

from __future__ import annotations

import http.client
import re
import socket
import ssl
from urllib.parse import urlencode, urlsplit

#: The methods the module sends. A wire record naming anything else is refused
#: with `invalid_request`; the public builders are the typed way to fill it.
METHODS = ("GET", "POST")

#: The two content types a `NetCall` may carry. `""` means "no body" (GET) or a
#: raw caller-supplied body with no inferred content type (POST).
CONTENT_JSON = "application/json"
CONTENT_FORM = "application/x-www-form-urlencoded"
CONTENT_TYPES = ("", CONTENT_JSON, CONTENT_FORM)

#: Ceilings chosen to bound a single request in a server process. They are
#: limits of this module, not of HTTP: a caller that needs more must make more
#: than one request.
MAX_TIMEOUT_MS = 600_000
MAX_RESPONSE_BYTES = 64 * 1024 * 1024

#: Headers the module computes itself. Setting one from the caller would let a
#: request smuggle a mismatched transport claim past the caller's own intent,
#: so a caller-supplied one is refused rather than silently overwritten.
RESERVED_HEADERS = frozenset({
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "content-type",
    "accept-encoding",
})

#: RFC 9110 field-name token. A name outside this set is refused rather than
#: handed to `http.client`, which would accept some shapes a proxy must not.
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def _err(code, message):
    """A `NetReply` refusal: the same shape as a success reply, so a caller can
    decode one type instead of handling an exception path."""
    return {
        "ok": False,
        "status": 0,
        "headers": [],
        "body": "",
        "code": code,
        "message": message,
    }


def _ok(status, headers, body):
    return {
        "ok": True,
        "status": status,
        "headers": headers,
        "body": body,
        "code": "",
        "message": "",
    }


def _host_of(url):
    """`(host, port, path_query)` for an https URL, or `(None, None, reason)`.

    Parsing lives here and nowhere else so every refusal precedes the socket.
    The reason strings are the only text derived from the URL that can reach a
    message, and none of them carries the URL itself.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None, None, "url_not_parseable"
    if parts.scheme.lower() != "https":
        return None, None, "insecure_scheme"
    if parts.username is not None or parts.password is not None:
        return None, None, "credentials_in_url"
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None, None, "url_not_parseable"
    if not host:
        return None, None, "url_not_parseable"
    path = parts.path or "/"
    if parts.query:
        path = path + "?" + parts.query
    return host, port or 443, path


def _headers_of(rows):
    """`(list of (name, value) pairs, None)` or `(None, reason)`.

    The name is checked against the RFC 9110 token grammar; both name and value
    are checked for CR/LF/NUL so a value can never split the header block. The
    refusal messages name the FAILURE, never the offending value.
    """
    if not isinstance(rows, list):
        return None, "headers_not_a_list"
    pairs = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            return None, "header_not_a_record"
        name = row.get("name")
        value = row.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            return None, "header_not_text"
        if not _HEADER_NAME.match(name):
            return None, "header_name_invalid"
        if any(ch in name or ch in value for ch in ("\r", "\n", "\x00")):
            return None, "header_contains_control"
        if name.lower() in RESERVED_HEADERS:
            # The module computes these itself. Overwriting silently would let
            # a caller believe it set a transport claim the module then
            # contradicted; refusing says so.
            return None, "reserved_header:" + name.lower()
        if name.lower() in seen:
            return None, "duplicate_header:" + name.lower()
        seen.add(name.lower())
        pairs.append((name, value))
    return pairs, None


def _form_body(fields):
    """The urlencoded body for `fields`, or `(None, reason)`.

    `urllib.parse.urlencode` is the stdlib encoder: spaces become `+`, a name
    or value byte outside the safe set is percent-encoded, so the body is the
    exact `application/x-www-form-urlencoded` form a server-side parser
    expects. It never raises for str input.
    """
    if not isinstance(fields, list):
        return None, "form_not_a_list"
    pairs = []
    for field in fields:
        if not isinstance(field, dict):
            return None, "form_field_not_a_record"
        name = field.get("name")
        value = field.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            return None, "form_field_not_text"
        pairs.append((name, value))
    return urlencode(pairs), None


def perform(call, expected_host=None):
    """Execute one `NetCall` and return a `NetReply` dict. Never raises.

    `expected_host` is the capability destination the caller declared. The
    generic extern passes its `host` argument here; a trusted per-host wrapper
    passes its constant host. When it is set, a URL whose host is anything else
    is refused (`host_mismatch`) before a socket opens. It is matched
    case-insensitively on `urlsplit().hostname`, so `https://HOST/` and
    `https://host/` agree; a port, a path, a query string and a fragment do not
    participate in the check.
    """
    try:
        return _perform(call, expected_host)
    except Exception as error:  # noqa: BLE001 - the wire contract is data, not raises
        return _err(
            "internal_error",
            "net: the request could not be prepared (" + type(error).__name__ + ")",
        )


def _perform(call, expected_host):
    if not isinstance(call, dict):
        return _err("invalid_request", "net: the call is not a record")

    method = call.get("method")
    if method not in METHODS:
        return _err("invalid_request", "net: method must be GET or POST")

    timeout_ms = call.get("timeout_ms")
    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) \
            or timeout_ms < 1 or timeout_ms > MAX_TIMEOUT_MS:
        return _err(
            "invalid_timeout",
            "net: timeout_ms must be a whole number from 1 to "
            + str(MAX_TIMEOUT_MS),
        )

    max_bytes = call.get("max_bytes")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) \
            or max_bytes < 1 or max_bytes > MAX_RESPONSE_BYTES:
        return _err(
            "invalid_size_bound",
            "net: max_bytes must be a whole number from 1 to "
            + str(MAX_RESPONSE_BYTES),
        )

    url = call.get("url")
    if not isinstance(url, str) or not url:
        return _err("url_not_parseable", "net: the url must be a non-empty string")
    host, port, target = _host_of(url)
    if host is None:
        if target == "insecure_scheme":
            return _err(
                "insecure_scheme",
                "net: only https URLs are sent; this request was refused "
                "before a connection was opened",
            )
        if target == "credentials_in_url":
            return _err(
                "credentials_in_url",
                "net: the url carries credentials; a secret belongs in a "
                "header, never in a URL",
            )
        return _err("url_not_parseable", "net: the url could not be parsed")

    if expected_host is not None:
        if not isinstance(expected_host, str) or not expected_host:
            return _err(
                "invalid_request",
                "net: the declared destination host must be a non-empty string",
            )
        if host.lower() != expected_host.lower():
            return _err(
                "host_mismatch",
                "net: the url host is not the destination the declared reach "
                "names (the request does not state its host twice); the "
                "request was refused",
            )

    pairs, problem = _headers_of(call.get("headers"))
    if problem:
        return _err("invalid_header", "net: a request header was refused (" + problem + ")")

    body = call.get("body")
    if not isinstance(body, str):
        return _err("invalid_request", "net: the request body must be a string")
    content_type = call.get("content_type")
    if content_type not in CONTENT_TYPES:
        return _err("invalid_request", "net: unsupported content type")
    form = call.get("form")
    if not isinstance(form, list):
        return _err("invalid_request", "net: form must be a list of fields")
    if form and content_type != CONTENT_FORM:
        return _err(
            "invalid_request",
            "net: form fields are only sent with the urlencoded content type",
        )

    if method == "GET":
        if body or form:
            return _err("invalid_request", "net: GET carries no body")
    else:
        if content_type == CONTENT_FORM:
            encoded, problem = _form_body(form)
            if problem:
                return _err(
                    "invalid_request",
                    "net: a form field was refused (" + problem + ")",
                )
            body = encoded
        elif form:
            return _err("invalid_request", "net: form fields need the urlencoded content type")

    # A caller-supplied content type is refused above (reserved header), so the
    # wire's declared body kind is the only one sent. http.client takes a
    # mapping, and duplicate caller names are refused above, so a dict loses
    # nothing.
    header_pairs = dict(pairs)
    if content_type:
        header_pairs["Content-Type"] = content_type
    header_pairs["Accept-Encoding"] = "identity"

    ca_file = call.get("ca_file")
    if ca_file is not None and not isinstance(ca_file, str):
        return _err("invalid_request", "net: ca_file must be a path or absent")
    try:
        context = ssl.create_default_context(cafile=ca_file)
    except (OSError, ssl.SSLError):
        return _err(
            "ca_file_unreadable",
            "net: the cafile could not be read; the request was not sent",
        )
    context.minimum_version = ssl.TLSVersion.TLSv1_2

    body_bytes = body.encode("utf-8") if method == "POST" and body else None
    if method == "POST" and body == "" and content_type:
        body_bytes = b""

    connection = None
    try:
        connection = http.client.HTTPSConnection(
            host, port, timeout=timeout_ms / 1000, context=context)
        connection.request(method, target, body=body_bytes, headers=header_pairs)
        response = connection.getresponse()
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            return _err(
                "response_too_large",
                "net: the response body from " + host + " exceeded max_bytes",
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return _err(
                "response_not_utf8",
                "net: the response body from " + host + " is not UTF-8 text",
            )
        headers = [
            {"name": name, "value": value} for name, value in response.getheaders()
        ]
        return _ok(response.status, headers, text)
    except (socket.timeout, TimeoutError):
        return _err(
            "timeout",
            "net: the request to " + host + " timed out after "
            + str(timeout_ms) + " ms",
        )
    except ssl.SSLCertVerificationError:
        return _err(
            "tls_verification",
            "net: the certificate presented by " + host + " did not verify",
        )
    except ssl.SSLError:
        return _err("tls_error", "net: TLS failed for " + host)
    except (ConnectionError, OSError):
        return _err("network_error", "net: could not reach " + host)
    except http.client.HTTPException:
        return _err(
            "protocol_error",
            "net: the response from " + host + " was not valid HTTP",
        )
    finally:
        if connection is not None:
            connection.close()
