"""The one HTTP client revl uses to reach a model endpoint (issue #1461).

Every request to a model provider in this tree goes through `request_json`:
the runtime adapters in this package, and the `bench/` tools that used to carry
three private copies of the same urllib code. One client means one redirect
policy, one error shape and one place where a credential is kept out of a
message.

Standard library only. revl has no required runtime dependency, and a model
call is a POST with a JSON body, which `urllib` does without help.

THE THREE RULES THIS FILE ENFORCES
----------------------------------
1. **No redirect is followed.** urllib follows a 301/302/303 by default and
   re-issues the POST as a GET, body dropped, at whatever host `Location`
   names. The endpoint is part of what the operator bound a model role to, and
   the placement check in `revl.providers.placement` judged THAT host. A
   redirect would move the prompt somewhere the check never saw, so it is a
   refusal, the same policy `revl.crossing_redirect` gives the emitted
   crossings.
2. **A credential never reaches a message.** The caller passes every secret it
   put in a header as `secrets`, and every string this module raises is passed
   through `redact` first, including the provider's own error body, which is
   where a key most often comes back: several providers echo the rejected key
   in a 401. The urllib exception is not chained (`from None`), so a traceback
   does not carry the request object either.
3. **Only a JSON object is a response.** Anything else is a named error rather
   than a value the caller has to guess about.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request

#: The text a redacted secret is replaced with.
REDACTED = "[redacted]"

#: How much of a provider's error body a message quotes. Enough to read the
#: provider's reason, not so much that a message becomes a dump of the reply.
ERROR_BODY_LIMIT = 400


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
                 secrets=(), label: str = "model endpoint") -> dict:
    """POST `body` as JSON to `url` (GET when `body` is None) and return the
    decoded JSON object.

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
    opener = urllib.request.build_opener(_NoRedirect)

    def fail(message: str, status: int | None = None) -> ProviderError:
        return ProviderError(redact(f"{label}: {message}", secrets),
                             status=status)

    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            raw = response.read()
    except _RedirectRefused as exc:
        raise fail(f"HTTP {exc.code} redirect refused: {url} is the endpoint "
                   f"this role is bound to, and a redirect would send the "
                   f"prompt to a host the placement check never judged",
                   exc.code) from None
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read()[:ERROR_BODY_LIMIT].decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - the body is best-effort context
            detail = ""
        raise fail(f"HTTP {exc.code} from {url}: {detail}", exc.code) from None
    except urllib.error.URLError as exc:
        raise fail(f"cannot reach {url}: {exc.reason}") from None
    except TimeoutError:
        raise fail(f"timed out after {timeout}s waiting for {url}") from None
    except OSError as exc:
        raise fail(f"connection to {url} failed: {exc}") from None
    except http.client.HTTPException as exc:
        # not an OSError: a body shorter than its Content-Length
        # (IncompleteRead), a status line that is not HTTP (BadStatusLine)
        raise fail(f"malformed HTTP response from {url}: "
                   f"{type(exc).__name__}") from None

    if status != 200:
        raise fail(f"HTTP {status} from {url}: "
                   f"{raw[:ERROR_BODY_LIMIT].decode('utf-8', 'replace')}",
                   status)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise fail(f"unparseable JSON from {url}: {exc}") from None
    if not isinstance(parsed, dict):
        raise fail(f"{url} answered with a JSON {type(parsed).__name__}, "
                   f"not an object")
    return parsed
