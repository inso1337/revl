"""The one answer envelope the machine-readable surfaces share (issue #2858).

An answer says four things before it says what it found:

  ok          did the call answer at all (already on every answer)
  verdict     passed | failed | refused
  payloadKey  the key in the same object that carries the verb's substance
  refusal     present when verdict is refused: code, category, message,
              location, repair

`ok` was already universal, so the envelope adds `verdict`, `payloadKey` and
the optional `refusal` object. It is additive by design: a converted verb keeps
every key it answered with before, at the same place, so existing consumers
keep working while a new consumer can branch on the envelope alone. The first
slice converts `revl_check` and `revl_test_run` (`PAYLOAD_KEYS`); a later verb
joins by adding its payload key here and stamping the answer in the server.

`verdict` separates two `ok: false` answers that used to need prose to tell
apart:

  failed      the call ran and the composition (or the test suite) failed:
              read `diagnostics`, and for a check the `selfCheck` rows
  refused     the call did not run: `refusal` says why, by code

The stable contract is the refusal `code`. `message` is prose and stays free to
improve; scripts and agents branch on `code` and `category`, never on the
sentence. `REFUSALS` is the one registry every converted handler draws a code
from: a code is added here before a handler may emit it, and `define` raises
`UnknownRefusalCode` for one that is not, so an unregistered code cannot reach
an answer.

`CATEGORY_CODES` closes the other direction: the refusal sites that run before
a verb's handler (the path jail, the authoring gate, an unknown argument) know
their category but not a verb-specific code, and each category maps to one
registered fallback code. A category absent from the map is a programming
error, not a silently generic answer.
"""

from __future__ import annotations

from typing import NamedTuple

#: One refusal code's stable classification. `category` is the coarse family a
#: consumer branches on; `meaning` says when the code is emitted, in enough
#: detail that a reader never needs the message to identify it.
class Entry(NamedTuple):
    category: str
    meaning: str


#: code -> category and meaning. The codes the converted handlers emit, then
#: one fallback per category for a refusal made before a handler ran.
REFUSALS: dict[str, Entry] = {
    # `revl_check` (the held-session form included)
    "SESSION_TAKES_NO_CANDIDATE": Entry(
        "usage",
        "`session: true` asks to check the composition the session holds, so "
        "the call may not carry a `source`/`files` candidate of its own"),
    "DRAFT_HELD": Entry(
        "usage",
        "a candidate was sent while an edit draft is held; checking it would "
        "replace the working set the draft is filling"),
    "MODULES_WITHOUT_CANDIDATE": Entry(
        "usage",
        "`modules` are the in-memory `use` imports of a candidate and do not "
        "name a candidate by themselves"),
    "NO_CANDIDATE": Entry(
        "usage",
        "the call named no candidate and the session held no composition to "
        "check instead"),
    "NOTHING_TO_CHECK": Entry(
        "session",
        "the held composition carries no source text that could be "
        "recompiled, so there is no check to make"),

    # `revl_test_run`
    "EMPTY_SELECTION": Entry(
        "usage",
        "the test selection was empty, so a run over nothing would read as "
        "green"),
    "BAD_SELECTION": Entry(
        "usage",
        "the test selection was not a non-empty array of test paths or node "
        "ids, or one of its entries named nothing runnable"),
    "BAD_CWD": Entry(
        "usage",
        "the working directory was missing, was not a string, or is not a "
        "directory"),
    "BAD_TIMEOUT": Entry(
        "usage",
        "the timeout was not a positive number of seconds"),
    "OUTSIDE_ROOT": Entry(
        "admission",
        "the test selection or its working directory leaves the "
        "operator-sanctioned root(s), and a selection is confined like every "
        "other path argument"),
    "TIMEOUT": Entry(
        "runtime",
        "the selected tests did not finish inside the timeout"),
    "TEST_RUN_FAILED": Entry(
        "runtime",
        "the test runner could not be started by this server's interpreter"),
    "NO_TESTS_COLLECTED": Entry(
        "usage",
        "the selection collected no test, so there is no result to report"),
    "ALL_SKIPPED": Entry(
        "usage",
        "every selected test was skipped, and a skipped run is not a passing "
        "run"),

    # fallbacks for a refusal made before a verb's handler ran
    "USAGE": Entry(
        "usage",
        "the call's arguments did not satisfy the verb's input contract"),
    "SESSION_REFUSED": Entry(
        "session",
        "the call did not fit the state the session held"),
    "ADMISSION_REFUSED": Entry(
        "admission",
        "a path argument or an import left what the operator sanctioned"),
    "AUTHORING_REFUSED": Entry(
        "admission",
        "the candidate carried host code the session's authoring trust does "
        "not admit"),
    "RUNTIME_REFUSED": Entry(
        "runtime",
        "the verb's runtime could not do what the call asked"),
    "OPERATOR_REFUSED": Entry(
        "operator",
        "the bound operator profile does not allow this management action"),
    "LEASE_REFUSED": Entry(
        "lease",
        "another operator holds a lease on a component this call would "
        "replace"),
    "INTERNAL_REFUSED": Entry(
        "internal",
        "the verb failed while answering"),
}

#: category -> the registered fallback code, for a refusal whose site names a
#: category but no verb-specific code. Closed over the categories in
#: `REFUSALS`: a missing category raises rather than inventing an answer.
CATEGORY_CODES: dict[str, str] = {
    "usage": "USAGE",
    "session": "SESSION_REFUSED",
    "admission": "ADMISSION_REFUSED",
    "runtime": "RUNTIME_REFUSED",
    "operator": "OPERATOR_REFUSED",
    "lease": "LEASE_REFUSED",
    "internal": "INTERNAL_REFUSED",
}

#: The converted verbs and the key in the same answer that carries their
#: substance. A verb joins the envelope by being named here.
PAYLOAD_KEYS: dict[str, str] = {
    "revl_check": "selfCheck",
    "revl_test_run": "tests",
}


class UnknownRefusalCode(KeyError):
    """A refusal code no `REFUSALS` entry defines.

    Raised at the moment the code is used, so an unregistered code is a bug in
    the handler rather than an answer a consumer has to guess about."""

    def __str__(self) -> str:
        return (f"refusal code {self.args[0]!r} is not in "
                f"revl.answer.REFUSALS")


def define(code: str) -> Entry:
    """The registry entry for `code`, or `UnknownRefusalCode`."""
    try:
        return REFUSALS[code]
    except KeyError:
        raise UnknownRefusalCode(code) from None


def refusal(code: str, message: str, location: str | None = None,
            repair: str | None = None) -> dict:
    """One structured refusal: the stable code and category from the registry,
    the free-to-change `message`, and where and how to repair the call."""
    entry = define(code)
    return {"code": code, "category": entry.category, "message": str(message),
            "location": location, "repair": repair}


def attach_refusal(payload: dict, code: str, message: str,
                   fix: str | None = None) -> dict:
    """Give a refusal payload its structured `refusal`, built from the code the
    caller already validated and the `location`/`repair` it put beside it.

    Called where a handler refuses, so the shared error envelope does not have
    to spell these fields itself and stay one vocabulary across transports."""
    payload["refusal"] = refusal(code, message, payload.get("location"),
                                 payload.get("repair") or fix)
    return payload


def envelope(verb: str, payload):
    """`payload` with the envelope keys for `verb`, or it unchanged when the
    verb is not converted (or the payload is not an object).

    An answer that already says `verdict` keeps it: `revl_test_run` names
    `passed`/`failed` for a completed run and `refused` for a run that never
    happened. A check verdict that failed is `failed` and carries its
    `diagnostics`/`selfCheck`, not a refusal: nothing refused the call.

    It does not raise for a carried diagnostic code: the code a `SessionError`
    forwards or a category this module does not map is not a registered refusal
    code, so a refusal is derived from the category instead. An explicit
    `refusal` object still has to name a registered code; that is a handler
    bug, not an answer to describe.
    """
    if not isinstance(payload, dict) or verb not in PAYLOAD_KEYS:
        return payload
    answer = dict(payload)
    answer.setdefault("payloadKey", PAYLOAD_KEYS[verb])
    answer["verdict"] = answer.get("verdict") or _verdict(answer)
    if answer["verdict"] == "refused":
        answer["refusal"] = _refusal_of(answer)
        answer.setdefault("code", answer["refusal"]["code"])
    elif isinstance(answer.get("refusal"), dict):
        answer["refusal"] = _normalised(answer["refusal"])
    return answer


def _verdict(payload: dict) -> str:
    if payload.get("ok"):
        return "passed"
    if "selfCheck" in payload:
        return "failed"
    return "refused"


def _refusal_of(payload: dict) -> dict:
    existing = payload.get("refusal")
    if isinstance(existing, dict):
        return _normalised(existing)
    code = payload.get("code")
    if code not in REFUSALS:
        # a carried diagnostic code (a SessionError's, a guarantee's) is not a
        # refusal code; deriving from the category keeps the answer a refusal
        code = _derived_code(payload)
    detail = (payload.get("diagnostics") or [{}])[0]
    return refusal(code,
                   detail.get("message") or payload.get("note") or "",
                   payload.get("location") or _location(detail),
                   payload.get("repair") or detail.get("fix"))


def _derived_code(payload: dict) -> str:
    """The registered code for a refusal whose site did not name one: the
    authoring trust and the path jail are named, everything else maps by the
    diagnostic's category. A category outside the map (a compiler category
    reaching a re-raised failure) is still answered, as the internal fallback,
    because an exception here would be a transport fault rather than a
    refusal."""
    if payload.get("authoringTrust"):
        return "AUTHORING_REFUSED"
    detail = (payload.get("diagnostics") or [{}])[0]
    return CATEGORY_CODES.get(detail.get("category") or "internal",
                              "INTERNAL_REFUSED")


def _location(detail: dict) -> str | None:
    path = detail.get("file")
    if not path:
        return None
    line = detail.get("line")
    return f"{path}:{line}" if line is not None else str(path)


def _normalised(value: dict) -> dict:
    return refusal(value.get("code"), value.get("message") or "",
                   value.get("location"), value.get("repair"))
