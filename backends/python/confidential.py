"""The confidentiality choke point: what a `Secret[T]` value may look like once
it leaves the process (roadmap item 256 Slice 3, §7b).

A `Secret[T]` declaration authorises disclosure **to the declared receiver**. It
does not authorise the recorder to keep a copy: an argument that legitimately
crosses at a declared `Secret[T]` receiver was still being written verbatim into
the durable write-ahead log, replayed out of `revl_timeline` / `revl_fork`, and
printed by `:bisect` — and a `Secret[Str]` config field was echoed into the run
log and into the `revl_load` MCP response. Those sinks are exactly the ones §7b
fences: a plaintext file at rest, and a model's context window.

The fix is placed **at capture, not at each printer**. `Timeline.record_emission`
and the `WriteAheadLog.record_*` builders put :data:`REDACTED` into the record
they construct, so the raw value never enters a `Step`, never reaches the WAL
writer, and is not there for any downstream renderer to find. A new externalis-
ation point added tomorrow reads the same already-redacted record and therefore
cannot leak; no printer has to remember to redact.

Two independent markings drive it, because the runtime cannot re-derive
confidentiality from a value:

1. **Declared position** — `taint.extract_and_normalize` strips `Secret[T]` off
   the declared types before lowering, so the compiler stamps the surviving
   index set into the IR (`params[i]["secret"]`). :class:`SecretIndex` reads it
   back and answers "is argument `i` of this crossing a declared disclosure
   receiver".
2. **Declared config field** — a config field's declared type reaches the
   emitted `ConfigSchema` intact, so `Secret[...]` is read straight off it.

Whenever either marking fires, the value is also remembered in a process-wide
set (:func:`register_secret_value`) and scrubbed by `_describe` wherever it turns
up again — belt and braces for a capture point that has no positional marking of
its own (a witness on a discharge descriptor, say). That is an exact-value match,
never a heuristic, so a non-secret argument is still recorded verbatim.

This module deliberately imports nothing: an emitted program runs against the
backend tree with no `revl` package on the path, and `replay.py` must stay
cordis-free.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

# Must equal `revl.taint.REDACTED_SECRET` — the placeholder is part of the WAL's
# on-disk contract and `revl recover` reads it back to refuse a replay it cannot
# honestly perform. `tests/test_secret_externalization.py` pins the two.
REDACTED = "<redacted:secret>"

_QUALIFIER = "Secret["

# A remembered confidential value has to be long enough that an exact match means
# something. Below this a value is a coin flip against ordinary timeline data
# ("", "1", "ok"), and blanket-erasing those would gut the timeline for no
# confidentiality gain — the positional marking still covers the declared
# receiver, which is where a short secret actually crosses.
_MIN_MARKABLE = 4

# The placeholder a CALLER'S OWN ARGUMENT is rendered as when it turns up inside
# free-form host text that is about to cross a trust boundary (item 421 F5). It
# is distinct from `REDACTED` on purpose: `REDACTED` says "a declared `Secret[T]`
# was here", this says "a value the caller passed in was here", and a reader of a
# seam error reply should be able to tell the two apart.
REDACTED_ARG = "<redacted:arg>"

# An argument value has to be this long before it is matched as a substring of
# host text. Below it, a match is a coin flip against ordinary English ("id",
# "on", "a") and blanket-replacing it would shred the diagnostic for no
# confidentiality gain. Deliberately lower than `_MIN_MARKABLE`: an argument is
# the caller's data by construction, whereas a remembered secret value is matched
# everywhere and needs the wider margin.
_MIN_MATCHABLE_ARG = 3

_secret_values: set = set()


# ---------------------------------------------------------------------------
# declared markings
# ---------------------------------------------------------------------------


def is_secret_type(type_name: Any) -> bool:
    """Whether a declared type carries the `Secret[...]` qualifier.

    Used for config fields, whose declared type reaches the runtime unstripped."""
    return isinstance(type_name, str) and type_name.lstrip().startswith(_QUALIFIER)


class SecretIndex:
    """Which argument positions and config fields a composition declared secret.

    Built once from the IR document the recorder already holds, so it costs one
    pass over `services` / `externs` / `components` and nothing per crossing.
    An IR with no `Secret[T]` anywhere yields an index that answers "nothing is
    secret" to every question, and the recorder behaves exactly as before.
    """

    __slots__ = ("_by_service", "_by_method", "_by_extern", "_config", "_requires")

    def __init__(self, ir: Optional[dict] = None) -> None:
        self._by_service: dict = {}    # (service, method) -> frozenset[int]
        self._by_method: dict = {}     # method            -> frozenset[int]
        self._by_extern: dict = {}     # extern name       -> frozenset[int]
        self._config: dict = {}        # component         -> frozenset[str]
        self._requires: dict = {}      # component -> {key: service}
        self._build(ir or {})

    # -- construction ------------------------------------------------------

    @staticmethod
    def _secret_indices(params: Any) -> frozenset:
        if not isinstance(params, (list, tuple)):
            return frozenset()
        return frozenset(
            index for index, param in enumerate(params)
            if isinstance(param, dict) and param.get("secret"))

    def _build(self, ir: dict) -> None:
        for service, spec in (ir.get("services") or {}).items():
            for method, method_spec in ((spec or {}).get("methods") or {}).items():
                indices = self._secret_indices((method_spec or {}).get("params"))
                if not indices:
                    continue
                self._by_service[(service, method)] = indices
                # The checker keys `secret_receivers` by OPERATION NAME alone, so
                # the fallback used when a capture point knows only the method
                # keeps exactly the checker's own granularity.
                self._by_method[method] = self._by_method.get(
                    method, frozenset()) | indices
        for ext in (ir.get("externs") or []):
            indices = self._secret_indices((ext or {}).get("params"))
            if indices:
                name = (ext or {}).get("name")
                self._by_extern[name] = indices
                self._by_method[name] = self._by_method.get(
                    name, frozenset()) | indices
        for comp in (ir.get("components") or []):
            name = (comp or {}).get("name")
            requires = (comp or {}).get("requires") or {}
            if isinstance(requires, dict) and requires:
                self._requires[name] = dict(requires)
            fields = frozenset(
                field.get("name") for field in ((comp or {}).get("config") or [])
                if isinstance(field, dict) and is_secret_type(field.get("type")))
            if fields:
                self._config[name] = fields

    # -- queries -----------------------------------------------------------

    @property
    def engaged(self) -> bool:
        """Whether this composition declares any `Secret[T]` surface at all."""
        return bool(self._by_method or self._by_extern or self._config)

    def crossing(self, *, service: Optional[str] = None,
                 method: Optional[str] = None,
                 key: Optional[str] = None,
                 component: Optional[str] = None) -> frozenset:
        """The argument indices of one boundary crossing that are declared
        `Secret[T]`.

        Resolution is most-specific-first: the exact `(service, method)` pair
        when the service is known, else the service the component requires under
        `key`, else the operation name on its own. The last is the checker's own
        keying, so it can only over-redact (two services sharing an operation
        name where one declares the receiver), never under-redact."""
        if method is None:
            return frozenset()
        if service is not None:
            found = self._by_service.get((service, method))
            if found is not None:
                return found
        if service is None and key is not None:
            resolved = (self._requires.get(component) or {}).get(key)
            if resolved is not None:
                found = self._by_service.get((resolved, method))
                if found is not None:
                    return found
        return self._by_method.get(method, frozenset())

    def config_fields(self, component: Optional[str]) -> frozenset:
        """The config field names `component` declared `Secret[T]`."""
        return self._config.get(component, frozenset())


# ---------------------------------------------------------------------------
# value marking
# ---------------------------------------------------------------------------


def register_secret_value(value: Any) -> None:
    """Remember a value a declared marking just identified as confidential.

    Exact-value membership, never a pattern: this scrubs the SAME value turning
    up at a capture point that carries no positional marking of its own. It
    cannot promote an unrelated value, and it is per-process — nothing is
    written down."""
    if isinstance(value, (str, bytes)) and len(value) >= _MIN_MARKABLE:
        _secret_values.add(value)


def register_secret_tree(value: Any) -> None:
    """Remember every string leaf of a value a declared marking identified as
    confidential, containers included.

    A `Secret[T]` where T is a record or a list is confidential WHOLE, so each
    leaf that could later be interpolated into a trace line or a host error is
    registered. Scalars go straight to :func:`register_secret_value`, which keeps
    the same minimum-length rule; nothing else changes."""
    if isinstance(value, (list, tuple)):
        for item in value:
            register_secret_tree(item)
        return
    if isinstance(value, dict):
        # Values only: a record's KEYS are field names the author wrote, and
        # registering them would redact the field name out of every later trace.
        for item in value.values():
            register_secret_tree(item)
        return
    register_secret_value(value)


def is_secret_value(value: Any) -> bool:
    """Whether `value` is a value some declared marking already redacted."""
    if not _secret_values or not isinstance(value, (str, bytes)):
        return False
    return value in _secret_values


def forget_secret_values() -> None:
    """Drop every remembered value. For tests, and for a host that wants the
    marking not to outlive one composition."""
    _secret_values.clear()


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------


def redact_args(args: Iterable, secret_indices=frozenset(), describe=None) -> list:
    """Render a crossing's arguments for a record, with the declared
    `Secret[T]` positions replaced by :data:`REDACTED`.

    The record keeps its shape — same list, same arity, same position — so
    replay, the timeline and every consumer of `detail.args` keep working; only
    the confidential bytes are gone. Each redacted value is registered, so the
    same value is scrubbed if it surfaces again somewhere with no marking."""
    describe = describe or (lambda value: value)
    out = []
    for index, value in enumerate(args):
        if index in secret_indices:
            register_secret_value(value)
            out.append(REDACTED)
        else:
            out.append(describe(value))
    return out


def is_redacted(value: Any) -> bool:
    """Whether a value read back out of a record is the placeholder rather than
    the thing itself — what `revl recover` checks before it tries to re-issue a
    call from a durable descriptor."""
    return value == REDACTED


def redact_value(value: Any) -> Any:
    """Render one value with every already-registered secret scrubbed, nested
    containers included — the single funnel a capture point with no positional
    marking of its own (item 256 Slice 3, §7b) passes a value through before it
    is kept anywhere durable or handed back to a caller: a WAL record's `repr`
    detail, a discharge descriptor's witness, and — item 416c — a validation
    fault raised over an untrusted model response, which may carry a secret the
    program fed into the prompt back out verbatim.

    Exact-value match against :func:`is_secret_value`, never a heuristic: an
    ordinary value, including one nested deep in a container, is still rendered
    verbatim. A value this funnel has never seen registered is not redacted —
    this is belt-and-braces on top of the positional marking, not a substitute
    for it."""
    if is_secret_value(value):
        return REDACTED
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [redact_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): redact_value(v) for k, v in value.items()}
    return repr(value)


# ---------------------------------------------------------------------------
# redaction of ALREADY-RENDERED text (item 421 F5/F6)
# ---------------------------------------------------------------------------
#
# `redact_value` funnels a value before it is rendered. Two sinks cannot use it,
# because by the time revl sees them the value is already inside a string it did
# not build:
#
#   * the host trace (`runtime.py` `_record`), whose events are f-strings the
#     runtime interpolates a key / sql / item into. Redacting at each `_record`
#     call site would be "at each printer", the discipline this module exists to
#     avoid, and would miss the next site added;
#   * a host exception's message crossing the seam (`bridge.py`), which is free
#     text produced by code revl did not write. A plain `data[key]` lookup raises
#     `KeyError: '<the key>'` with no author interpolation at all.
#
# So both go through a text funnel instead, and the match stays EXACT: a
# registered secret value, or one of this call's own argument values. Nothing is
# pattern-matched, so ordinary trace and ordinary diagnostics are untouched.


def _needles(value: Any, into: set, minimum: int) -> None:
    """Collect the string forms `value` can take inside host text.

    Bools and None are skipped: their renderings ("True", "None") are ordinary
    English in a diagnostic, and replacing them would corrupt messages that have
    nothing to do with the caller's data."""
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        if len(value) >= minimum:
            into.add(value)
        return
    if isinstance(value, bytes):
        for form in (value.decode("utf-8", "replace"), repr(value)):
            if len(form) >= minimum:
                into.add(form)
        return
    if isinstance(value, (int, float)):
        form = str(value)
        if len(form) >= minimum:
            into.add(form)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _needles(item, into, minimum)
        return
    if isinstance(value, dict):
        # Values only. A record's KEYS are field names the author wrote, not the
        # caller's data, and redacting them would erase the diagnostic's shape.
        for item in value.values():
            _needles(item, into, minimum)
        return
    members = getattr(value, "__dict__", None)
    if isinstance(members, dict):
        for item in members.values():
            _needles(item, into, minimum)


def _replace_all(text: str, needles: Iterable, placeholder: str) -> str:
    """Replace each needle with `placeholder`, LONGEST FIRST so a needle that
    contains another does not leave the shorter one's tail behind."""
    for needle in sorted(set(needles), key=len, reverse=True):
        if needle and needle in text:
            text = text.replace(needle, placeholder)
    return text


def redact_text(text: Any) -> Any:
    """Scrub every already-registered secret value out of a rendered string.

    The funnel for a sink that receives TEXT rather than a value: the host
    trace the operator console prints (item 421 F6). Costs nothing when the
    composition registered no secret: the common case returns immediately."""
    if not _secret_values or not isinstance(text, str) or not text:
        return text
    needles: set = set()
    for secret in _secret_values:
        _needles(secret, needles, _MIN_MARKABLE)
    return _replace_all(text, needles, REDACTED)


def redact_call_text(text: Any, args: Any = ()) -> Any:
    """Scrub one call's own argument values, then every registered secret, out of
    free-form host text (item 421 F5).

    A failure crossing a seam must not hand the consumer back the values it was
    called with: the consumer is on the other side of a trust boundary, and the
    forward crossing into a declared `Secret[T]` receiver authorises disclosure
    TO THE RECEIVER, never a reverse crossing on the error channel. The message's
    SHAPE survives (the exception type, the sentence, the surrounding text), so
    the reply is still worth reading; only the caller's own bytes are gone.

    Bounded honestly: a value shorter than `_MIN_MATCHABLE_ARG` is left alone
    (see the constant), and a value the host reformats before printing it (a
    truncation, a case fold, a `%.2f`) is not matched, because the match is exact
    rather than a pattern."""
    if not isinstance(text, str) or not text:
        return text
    needles: set = set()
    _needles(list(args) if isinstance(args, (list, tuple)) else args,
             needles, _MIN_MATCHABLE_ARG)
    if needles:
        text = _replace_all(text, needles, REDACTED_ARG)
    return redact_text(text)
