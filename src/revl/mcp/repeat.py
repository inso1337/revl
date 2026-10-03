"""An identical repeated refusal changes shape (issue #1694).

In an agent benchmark the same failing `revl_edit` was sent ten times and got
the same refusal ten times. A caller that retries unchanged is misreading the
refusal, and a byte-identical answer gives it nothing new to read. So the
server remembers the last refused call, by verb and a digest of its
arguments. When the very next call is that same call and it is refused again,
the refusal says so: which attempt this is, the reason it failed, restated
beside what the session holds now, and the `next` call when there is one.
From the third identical attempt on, the refusal also carries a distinct
diagnostic code, `REPEATED_REFUSAL`, that a caller can branch on.

A different verb, different arguments or a call that succeeds resets the
count. Only refusals are rewritten; a success is never touched.
"""

from __future__ import annotations

import hashlib
import json
import threading

from .ambient import render

#: The attempt from which a repeated refusal carries `REPEATED_REFUSAL`.
BOUND = 3
CODE = "REPEATED_REFUSAL"

_LOCK = threading.Lock()
_LAST: dict = {"key": None, "attempt": 0}


def digest(verb: str, arguments: dict) -> str:
    """A stable digest of one call: the verb and its arguments, key-sorted."""
    blob = json.dumps({"verb": verb, "arguments": arguments}, sort_keys=True,
                      default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def forget() -> None:
    """Drop the remembered refusal (a fresh server, or a test case)."""
    with _LOCK:
        _LAST.update(key=None, attempt=0)


def observe(verb: str, arguments: dict, payload: dict, state: dict) -> dict:
    """`payload` as the caller should see it: unchanged unless it is a
    refusal of the same call as the refusal just before it. `state` is the
    session footer (`ambient.footer`) for the restatement."""
    attempt = _count(digest(verb, arguments), refused=not payload.get("ok", False))
    if attempt < 2:
        return payload
    return _reshaped(payload, attempt, state)


def _count(key: str, *, refused: bool) -> int:
    """The attempt number of this call among consecutive identical refusals,
    or 0 when it was not refused."""
    with _LOCK:
        if not refused:
            _LAST.update(key=None, attempt=0)
            return 0
        attempt = _LAST["attempt"] + 1 if _LAST["key"] == key else 1
        _LAST.update(key=key, attempt=attempt)
        return attempt


def _reshaped(payload: dict, attempt: int, state: dict) -> dict:
    diagnostics = [dict(d) for d in payload.get("diagnostics") or []]
    reason = diagnostics[0]["message"] if diagnostics else "the call was refused"
    restated = (f"attempt {attempt} of this same call, refused again for the "
                f"same reason: {reason}. The session now: {render(state)}")
    if diagnostics:
        diagnostics[0]["message"] = restated
    else:
        diagnostics = [_diagnostic("REVL", restated)]
    if attempt >= BOUND:
        diagnostics.insert(0, _diagnostic(CODE, _stop_message(attempt, payload)))
    return {**payload, "diagnostics": diagnostics,
            "repeat": {"attempt": attempt, "bound": BOUND}}


def _stop_message(attempt: int, payload: dict) -> str:
    remedy = ("send `next` instead" if payload.get("next") is not None
              else "change the arguments, or the session state the refusal names")
    return (f"this exact call has now been refused {attempt} times in a row, "
            f"and sending it unchanged will be refused again: {remedy}")


def _diagnostic(code: str, message: str) -> dict:
    return {"severity": "error", "code": code, "category": "session",
            "message": message}
