"""Host-runtime adapter and stub stdlib for the revl cordis-py backend.

This module is everything an emitted component imports.  It has two halves:

* **Adapter** — :class:`Frame`, the per-activation accumulator that maps the
  IR's component-level LIFO recovery (R1) onto cordis-py's effect protocol.
  cordis-py unloads a fiber's *top-level* effects concurrently (its
  `asyncio.gather` mirrors the upstream `Promise.all`) and only guarantees
  LIFO order *within* a single effect's yielded disposers.  The emitter
  therefore compiles a component body to exactly one `ctx.effect(generator)`
  whose yields are the accumulated inverses, and `Frame` adopts any effects
  registered later by provide-method calls so they are drained — newest
  first — ahead of the activation-time inverses.

* **Host stdlib** — the `Pool` / `Map` / `Job` host builtins required by
  docs/backend-ir.md.  `Map` is a real in-memory map; `Pool` is a real
  bounded connection pool over a deterministic fake database; `Job` is a
  real cancellable asynchronous unit of work.  All three record every
  operation so demos and tests can assert ordering.

  This module is the **reference definition** of the Pool/Job semantics — see
  ":ref:`pool-job-semantics`" below.  The cordis (TS), cordis-rs and cordis4j
  tiers implement the same state machines and point back here; a tier that
  drifts from this text is a bug in that tier, not a local dialect.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import hmac
import inspect
import itertools
import json
import os
import re
import time
import types
import weakref
from collections import OrderedDict
from typing import Any, Callable, Optional

# The confidentiality choke point (item 256 Slice 3): `confidential.py`, the
# sibling module this runtime and the recorder both read, so a `Secret[T]` value
# is redacted in ONE place rather than at each printer. It ships next to this
# file and must travel with it.
#
# The fallback covers this module being loaded BY PATH with its own directory off
# `sys.path` (`spec_from_file_location(".../runtime.py")`, which several suites
# do). A bare import cannot survive that, and continuing without the module would
# mean continuing to leak, so this raises rather than degrades.
try:
    import confidential
except ModuleNotFoundError:  # pragma: no cover — path-loaded copy of this module
    import importlib.util as _importlib_util
    import sys as _sys

    _confidential_spec = _importlib_util.spec_from_file_location(
        "confidential",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "confidential.py"))
    confidential = _importlib_util.module_from_spec(_confidential_spec)
    _confidential_spec.loader.exec_module(confidential)
    _sys.modules.setdefault("confidential", confidential)

# item 247 second-pass (F5): a process-monotonic registration index stamped on
# every transactional/compensation entry at construction, so the escrow can be
# replayed LIFO (reverse registration) even when no WAL is attached and every
# `seq` is None. Reverse-`seq` alone collapses to a stable-sort no-op there,
# leaving a data-losing FIFO replay of overlapping idempotent-total inverses
# (the item-369 hazard). See `SessionOwner.finalize_abort`.
_ENTRY_STAMP = itertools.count()

__all__ = [
    "Clock", "ConfigError", "ConfigSchema", "FaultProbe", "Frame", "Job", "JobCancelled",
    "JobHandle", "LeaseHandle", "LeaseRefused", "Map", "Pool", "PoolError",
    "EstopHalted", "EstopRefused",
    "SessionCommitError", "SessionOwner",
    "SpawnHandle", "StateIncompatible",
    "EventContract",
    "Stream", "StreamFaulted", "StreamSource", "Subscription", "STREAM_CLOSED",
    "TimerHandle", "TransientError", "add_trace", "arm_fault_probe", "clear_session_owner",
    "disarm_fault_probe",
    "fmt", "live_instances", "plug", "realm_label", "remove_trace", "resolved_config",
    "retry_idempotent", "schedule_after", "schedule_every", "session_owner",
    "set_session_owner", "set_trace", "spawn",
    "trace_observers",
    # item 443: the operator E-Stop (docs/design/443-estop.md)
    "arm_estop_latch", "clear_estop", "estop", "estop_engaged",
    "estop_from_latch", "estop_latch_path", "estop_residue", "estop_state",
]


# ---------------------------------------------------------------------------
# Delivery semantics (docs/delivery-semantics.md, roadmap item 44)
# ---------------------------------------------------------------------------
#
# The IR promotes idempotency to a checked property: `emission idempotent fn`.
# Only that promise earns the runtime a new right — to *auto-retry* the
# emission on a transient failure, because re-delivering it is defined to have
# the same effect as delivering it once (`f(f(x)) == f(x)` on the server).
#
# A host signals "this failure was transient — the emission did not durably
# land, so re-issuing it is well-defined" by raising `TransientError`. Any
# other exception is a real error and is never retried. And a `TransientError`
# from a *non*-idempotent emission is still never retried: without the checked
# property, a second delivery could double the effect, so the runtime has no
# right to it. This is the whole point of making idempotency a checked flag.

class TransientError(RuntimeError):
    """A host-signalled transient delivery failure of an emission.

    Raising this from an emission's host body tells the runtime the emission
    did not durably land (a dropped connection, a 503, a timeout before the
    server committed). The runtime retries it *iff* the emission is declared
    `idempotent`; for any other emission it propagates unchanged.
    """


async def retry_idempotent(call, *, idempotent: bool = False,
                           attempts: int = 3, where: str = ""):
    """Invoke ``call`` and, for an idempotent emission, auto-retry a transient
    failure up to ``attempts`` times.

    ``call`` is a zero-argument callable that performs one delivery; its result
    is awaited if awaitable. A :class:`TransientError` is retried only when
    ``idempotent`` is true — a non-idempotent emission gets exactly one attempt
    and the transient failure propagates. Every non-transient exception
    propagates immediately, retries or not.
    """
    budget = attempts if idempotent else 1
    last: TransientError | None = None
    for _ in range(budget):
        try:
            result = call()
            if inspect.isawaitable(result):
                result = await result
            return result
        except TransientError as exc:  # noqa: PERF203 — retry is the point
            last = exc
    # budget exhausted (or a single non-idempotent attempt): re-raise the
    # transient failure so the caller still sees a failed delivery
    raise last


class ResponseValidationError(RuntimeError):
    """Item 257: a `validated` emission's response failed to validate against
    the schema derived from its return type.

    This is a TYPED fault at the boundary, not a stringly parse error a body
    forgets to handle. The malformed response is DISCARDED before the body ever
    sees it (validation runs at the forward crossing, before the value binds and
    before the body resumes), so nothing downstream consumed it and no `emit`
    step fired on it. That is what makes a completion safe-to-retry ("a read with
    a cost", §5.1), but the retry BUDGET and loop are Slice 2; Slice 1 raises
    this typed fault without re-issuing. `retryable` records the classification
    so the audit surface and the Slice-2 loop can read it.
    """

    retryable = True

    def __init__(self, message: str, *, where: str = "", schema=None, value=None):
        super().__init__(message)
        self.where = where
        self.schema = schema
        # item 416c: `value` is the raw model response — untrusted, not itself
        # secret, but it may carry a secret the program's OWN prompt fed the
        # model back out verbatim. A host that logs this exception (a bare
        # `logger.exception`, a crash reporter serializing `__dict__`) reads
        # `.value` the same as it reads `str(exc)`, so the SAME scrub applies
        # here as to the message: exact-match only, never a heuristic, so an
        # ordinary malformed response — the common case a retry loop or a
        # developer needs to see in full — is retained verbatim.
        self.value = confidential.redact_value(value)


def _json_type_ok(value, json_type: str) -> bool:
    if json_type == "object":
        return isinstance(value, dict)
    if json_type == "array":
        return isinstance(value, list)
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if json_type == "boolean":
        return isinstance(value, bool)
    if json_type == "null":
        return value is None
    return True


def _json_schema_error(value, schema, path: str = "$"):
    """Validate ``value`` against the derived JSON-Schema subset the revl mapping
    emits (item 257, §3). Returns an error string for the FIRST violation, or
    ``None`` when the value conforms. Covers exactly the constructs
    `json_schema_for` produces: primitive `type`, `const`, `enum`, `nullable`,
    `properties`/`required`/`additionalProperties` (bool or schema), `items`, and
    a discriminated `oneOf` (exactly one arm matches). No `$ref` (Slice 1 refuses
    cyclic types), so the walk is finite by construction."""
    if not isinstance(schema, dict):
        return None

    if "const" in schema:
        if value != schema["const"]:
            # item 416c: `value` is the raw, UNTRUSTED model response — never a
            # declared secret itself, but a secret the program fed into the
            # prompt can come back out of the model verbatim (§7b's model-
            # context sink again, from the return side). `schema["const"]` is
            # the program's OWN declared literal, never response content, so
            # it is left as-is; only the observed value goes through the same
            # exact-value scrub the WAL and the timeline already apply.
            return (f"{path}: expected const {schema['const']!r}, got "
                    f"{confidential.redact_value(value)!r}")
        return None

    if "enum" in schema:
        if value not in schema["enum"]:
            return (f"{path}: {confidential.redact_value(value)!r} is not one "
                    f"of {schema['enum']!r}")
        return None

    if "oneOf" in schema:
        matches = [arm for arm in schema["oneOf"]
                   if _json_schema_error(value, arm, path) is None]
        if len(matches) == 1:
            return None
        if not matches:
            return (f"{path}: value matches no arm of the union "
                    f"(a well-formed value names exactly one constructor)")
        return f"{path}: value is ambiguous, matching {len(matches)} union arms"

    if schema.get("nullable") and value is None:
        return None

    json_type = schema.get("type")
    if json_type is not None and not _json_type_ok(value, json_type):
        return f"{path}: expected type {json_type!r}, got {type(value).__name__}"

    if json_type == "object" or isinstance(value, dict):
        if not isinstance(value, dict):
            return None
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                return f"{path}: missing required property {name!r}"
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                err = _json_schema_error(item, props[key], f"{path}.{key}")
                if err is not None:
                    return err
            elif extra is False:
                return f"{path}: unexpected property {key!r}"
            elif isinstance(extra, dict):
                err = _json_schema_error(item, extra, f"{path}.{key}")
                if err is not None:
                    return err

    if json_type == "array" and isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(value):
                err = _json_schema_error(item, items, f"{path}[{i}]")
                if err is not None:
                    return err

    return None


_VALIDATE_MISSING = object()


def validate_response(value, schema, where: str = "", constructors=None):
    """Item 257 (§4): the validate-on-response seam. Check ``value`` against the
    derived ``schema`` REGARDLESS of what the provider did, and on success
    construct the revl ADT value from the validated tag/value so the tagged wire
    shape never leaks into the matched-over value (§3.2). On failure raise the
    typed :class:`ResponseValidationError` (the malformed value is discarded
    before the body sees it).

    ``constructors`` maps each case tag to its emitted ADT case class. Absent
    (a non-ADT validated return, e.g. a record or a primitive), the validated
    value is returned as-is."""
    err = _json_schema_error(value, schema, "$")
    if err is not None:
        raise ResponseValidationError(
            f"{where}: response failed validation: {err}"
            if where else f"response failed validation: {err}",
            where=where, schema=schema, value=value)
    if constructors and isinstance(value, dict) and "tag" in value:
        tag = value["tag"]
        ctor = constructors.get(tag)
        if ctor is None:
            raise ResponseValidationError(
                f"{where}: validated response tag {tag!r} names no constructor"
                if where else f"validated response tag {tag!r} names no constructor",
                where=where, schema=schema, value=value)
        payload = value.get("value", _VALIDATE_MISSING)
        return ctor() if payload is _VALIDATE_MISSING else ctor(payload)
    return value


def validate_retry(make_call, budget: int, schema, where: str = "",
                   constructors=None, site: "Optional[str]" = None):
    """Item 257 (Slice 2, §5.2): the read-with-a-cost validation-retry loop.

    Fire ``make_call`` — the model completion call, and ONLY it — and validate its
    response with :func:`validate_response`. On a
    :class:`ResponseValidationError` (the malformed value is discarded before the
    body ever sees it, §5.3) re-issue the completion up to ``budget`` times, then
    surface the fault. Total attempts are ``budget + 1`` (the first plus ``budget``
    retries), a hard ceiling: exhaustion is the SAME terminal typed fault the body
    observes under `retry 0`, never an unbounded loop (§8, attack 4).

    The seam sits at the forward crossing, so a retry re-crosses the one-way model
    boundary again and re-incurs ONLY that crossing (another token charge); no
    downstream `emit` fired on the malformed value and no teardown entry was
    registered from it, so a re-issue doubles nothing the system executes (§5.3).
    This is keyed on ONE fault kind — the validation fault; a `TransientError` or
    any other host error is NOT retried here (a completion is not idempotent, §5.1)
    and propagates immediately.

    `site` (item 121 Slice 2) is the emitter's static identity for THIS
    completion call. Present, a validated response mints the fiber-local
    value-flow token under it (`revl_note_validated_completion`); absent — every
    call site the emitter did not analyse — nothing is minted and `producedSeq`
    honest-degrades to absent."""
    attempt = 0
    started = time.monotonic()
    while True:
        value = make_call()
        try:
            validated = validate_response(value, schema, where, constructors)
        except ResponseValidationError:  # noqa: PERF203 — retry is the point
            if attempt >= budget:
                _revl_record_model_call(started, attempt + 1, budget + 1, value,
                                        validated=False)
                raise
            attempt += 1
            continue
        # item 121: a validated model completion. Record the revl-measured
        # bracket, the attempt count against the static N+1 ceiling, and the
        # host-reported usage from the return, bound to the crossing `make_call`
        # just recorded, for the driver to read when it records THAT crossing's
        # `emit` event (§2.1; item 242 for why the binding is by crossing rather
        # than by fiber). Off-path for a non-model retry loop only in that
        # nothing reads the entry there.
        _revl_record_model_call(started, attempt + 1, budget + 1, value)
        # item 121 Slice 2: and mint the value-flow token for this static site,
        # naming the crossing the recorder just made for the winning attempt.
        revl_note_validated_completion(site)
        return validated


async def validate_retry_async(make_call, budget: int, schema, where: str = "",
                               constructors=None, site: "Optional[str]" = None):
    """Item 257 (Slice 2, §5.2): the async colour of :func:`validate_retry`.

    ``make_call`` returns a FRESH coroutine per attempt (the emitter passes the
    un-awaited completion call as the thunk), so awaiting it re-issues the one-way
    crossing each retry. Identical bound and fault semantics to the sync form: up
    to ``budget + 1`` attempts, only a :class:`ResponseValidationError` is
    retried, and exhaustion surfaces the terminal typed fault."""
    attempt = 0
    started = time.monotonic()
    while True:
        result = make_call()
        if inspect.isawaitable(result):
            result = await result
        try:
            validated = validate_response(result, schema, where, constructors)
        except ResponseValidationError:  # noqa: PERF203 — retry is the point
            if attempt >= budget:
                _revl_record_model_call(started, attempt + 1, budget + 1, result,
                                        validated=False)
                raise
            attempt += 1
            continue
        _revl_record_model_call(started, attempt + 1, budget + 1, result)
        revl_note_validated_completion(site)
        return validated


# ---------------------------------------------------------------------------
# item 121: `revl trace` — the model hop as a first-class span
# (docs/design/121-revl-trace.md). The completion fires at exactly one runtime
# seam, `validate_retry` above, the only scope that sees the attempt count, the
# wall-clock bracket, the static N+1 ceiling, and the host usage metadata in one
# place (§2.1). These helpers assemble the `llm` payload the driver attaches to
# the crossing's `emit` event, bind that payload to the crossing it was measured
# at (item 242), mint the fiber-local value-flow token that gates `producedSeq`,
# and compute the salted, suppressible prompt digest.
# ---------------------------------------------------------------------------

# The origin markers item 256/249 attribute to a value. Spelled here as bare
# literals — matching `revl.taint.SECRET_ORIGIN`/`CONFIDENTIAL_ORIGIN` — so the
# emitted-code runtime stays free of a compiler-package import while the digest
# gate keeps the SAME meaning as the taint checker (§4 attack 1).
_REVL_SECRET_ORIGIN = "secret"
_REVL_CONFIDENTIAL_ORIGIN = "confidential"

# Per-run secret nonce keying the salted prompt digest (§4 attack 4b). Minted
# lazily once per process run and NEVER written to the trace or any span; salting
# defeats the cross-run confirmation oracle while preserving within-run "same
# prompt twice" equality (identical args under one run hash identically).
_revl_digest_nonce: "Optional[bytes]" = None


def revl_digest_nonce() -> bytes:
    """The per-run HMAC key for the prompt digest, minted on first use. Never
    serialised into the trace or a span (§4 attack 4b)."""
    global _revl_digest_nonce
    if _revl_digest_nonce is None:
        _revl_digest_nonce = os.urandom(32)
    return _revl_digest_nonce


def revl_reset_run_trace_state() -> None:
    """Reset the per-run trace state (mint a fresh digest nonce, clear the
    fiber-local model-hop registers: the crossing-keyed observation store, the
    recorded-crossing marker, and the value-flow token).

    Called by the driver at every generation boundary (`run._Driver._emit_module`,
    which is gen 1 for a plain run and gen N+1 for each `--watch` reload), and
    also the test seam. Item 416d: until that call was wired the contract line
    "called at run start" was false, and the state survived for the whole
    process."""
    global _revl_digest_nonce
    _revl_digest_nonce = None
    _revl_model_calls.set(())
    _revl_recorded_crossing.set(None)
    _revl_model_decision_sink.set(None)
    _revl_validated_completions.set(None)
    _revl_last_emission_index.set(None)
    _revl_pending_produced_by.set(None)


# item 242: the model-hop observations live in THIS fiber, KEYED BY THE CROSSING
# they were measured at — `(component, stepIndex)`, the identity the recorder
# mints for the `emit` step. Entries are `((crossing, observation), ...)`, oldest
# first, where an observation is `(latencySeconds, attempts, attemptCeiling,
# rawReturn)`. Written by `validate_retry` at the seam, read by the driver when
# it records that crossing.
#
# Why keyed and not a single register. The driver walks a step-back's
# `emissionsCrossed` NEWEST FIRST (`replay.Timeline.step_back` iterates
# `reversed(tail)`), so a bare "did this fiber observe a completion" register is
# consumed by whichever crossing the walk reaches first: a model completion
# followed by a later filesystem write got its model/token/cost/latency numbers
# attributed to the write. Fiber-locality alone cannot fix that — it isolates
# concurrent ACTIVATIONS, and both crossings are in one fiber. The crossing key
# isolates completions WITHIN one body, the same finer-than-fiber property the
# `producedSeq` token gets from the activation id: two completions in one body
# are two crossings, so they are two entries, and the walk order stops mattering.
#
# The value is REBUILT on every write, never mutated in place: a child Task
# copies the context by reference, so an in-place mutation would be visible to
# every fiber and would give back exactly the cross-attribution this keying
# exists to prevent.
_revl_model_calls: "contextvars.ContextVar[tuple]" = \
    contextvars.ContextVar("_revl_model_calls", default=())

# The crossing this fiber recorded most recently: `(component, stepIndex)`, or
# None when nothing has been recorded (recording off, or a hand-built timeline).
# Published by `replay.Timeline.record_emission` at RECORD time — which is inside
# `validate_retry`'s `make_call`, so when the seam returns, this names the
# completion's OWN crossing and nothing else's.
_revl_recorded_crossing: "contextvars.ContextVar[Optional[tuple]]" = \
    contextvars.ContextVar("_revl_recorded_crossing", default=None)

# item 250 Slice 3a: the durable sink for the crossing recorded most recently in
# this fiber, or None. The recorder publishes it beside the crossing key when a
# WAL is attached (`replay.Timeline.record_emission`), so the seam below can
# append a `model-decision` record for the completion it just measured WITHOUT
# the runtime holding a WAL handle. Consumed on write, exactly like the keyed
# observation: a later crossing that carried no completion never inherits it.
# None whenever recording is off or no WAL is attached, in which case the
# decision lives only on the trace (item 121) as before.
_revl_model_decision_sink: "contextvars.ContextVar[Optional[Callable]]" = \
    contextvars.ContextVar("_revl_model_decision_sink", default=None)

# ---------------------------------------------------------------------------
# Slice 2: the value-flow token that gates `producedSeq` (§2.2, the NEW
# CRITICAL), and the identity bridge it rides on.
#
# The token can hold neither a trace seq nor an activation id, because neither
# exists on this side of the boundary: `_Driver._seq` is private to `run.py` and
# the activation id is synthesised at step-back time. The one identity that
# ALREADY crosses is `replay.Step.index` — assigned by the recorder during
# forward execution and handed to the driver at step-back as `entry["index"]`.
# So the token holds the completion emission's STEP INDEX, and the driver maps
# step index -> trace seq as it records the crossings (`run._Driver._replay`).
#
# Three fiber-local registers make that work:
#
#   `_revl_last_emission_index`   the recorder publishes each crossing's step
#                                 index here as it records it, so the seam can
#                                 name the completion crossing it just made;
#   `_revl_validated_completions` site id -> the step index of the completion
#                                 that STATIC site last validated in this fiber.
#                                 Keyed by SITE, not "last completion": with two
#                                 completions live in one body, "last" would
#                                 attribute the wrong one;
#   `_revl_pending_produced_by`   the marker `revl_produced_emit` sets around a
#                                 downstream crossing the EMITTER proved reads
#                                 the completion's binding; the recorder consumes
#                                 it and stamps `detail["producedBy"]`.
#
# All three are contextvars, so a child Task copies rather than shares them and
# two live activations of one component never cross-attribute. The activation
# check itself is the driver's: a `producedBy` naming a crossing outside the
# activation being recorded resolves to nothing and the edge is OMITTED.
# ---------------------------------------------------------------------------

_revl_last_emission_index: "contextvars.ContextVar[Optional[int]]" = \
    contextvars.ContextVar("_revl_last_emission_index", default=None)

_revl_validated_completions: "contextvars.ContextVar[Optional[dict]]" = \
    contextvars.ContextVar("_revl_validated_completions", default=None)

_revl_pending_produced_by: "contextvars.ContextVar[Optional[int]]" = \
    contextvars.ContextVar("_revl_pending_produced_by", default=None)


def revl_note_emission_index(component: "Optional[str]", index: "Optional[int]",
                             sink: "Optional[Callable]" = None) -> None:
    """Item 242: publish the crossing being recorded RIGHT NOW in this fiber.

    Called by `replay.Timeline.record_emission` as it mints the `emit` step, so
    the completion seam below can bind its observation to the crossing that
    carried it instead of leaving the driver to infer one at read time. Pure
    bookkeeping: it records nothing, redacts nothing, and a timeline built
    without a runtime (a hand-written test) simply never calls it.

    Item 121 Slice 2 rides the same publication: the value-flow token holds the
    completion crossing's STEP INDEX, so the bare index is published alongside
    the `(component, index)` crossing key for `revl_note_validated_completion`
    to name the crossing the recorder just made.

    Item 250 Slice 3a rides it too: `sink`, when the recorder has a WAL
    attached, is the callable that appends THIS crossing's `model-decision`
    record (`sink(llm, outcome)`). It is published beside the key rather than
    looked up later because the seam that writes it runs after `make_call`
    returns, in the same fiber, with no WAL handle of its own. Absent (the
    default) means no durable sink: the decision stays trace-only."""
    _revl_recorded_crossing.set((component, index))
    _revl_last_emission_index.set(index)
    _revl_model_decision_sink.set(sink)


def revl_recorded_crossing() -> "Optional[tuple]":
    """The crossing this fiber recorded most recently, or None."""
    return _revl_recorded_crossing.get()


def _revl_record_model_call(started: float, attempts: int, attempt_ceiling: int,
                            raw_return, validated: bool = True) -> None:
    """Stash this fiber's model-completion observation, KEYED BY THE CROSSING it
    was measured at, for the driver to read when it records that crossing's
    `emit` event. `latencySeconds` is the revl-measured BRACKET (§2.2): honest
    about what revl timed, silent about what the host `@py` body did inside it.

    Item 250 Slice 3a: the same observation is then written DURABLY through the
    fiber's model-decision sink when the recorder published one
    (`_revl_write_model_decision`); `validated` names the outcome on that
    record, and defaults to True for the seam's own unit tests.

    The key is `revl_recorded_crossing()` — the crossing `make_call` just
    recorded, which under a validation retry is the LAST attempt, the one whose
    return validated and is being measured here. A completion with no recorded
    crossing (recording off) keys on None and is never matched by a keyed take,
    which is right: with nothing recorded there is no crossing to attribute it
    to (item 242)."""
    latency = max(0.0, time.monotonic() - started)
    crossing = _revl_recorded_crossing.get()
    obs = (latency, attempts, attempt_ceiling, raw_return)
    entries = tuple(e for e in _revl_model_calls.get() if e[0] != crossing)
    _revl_model_calls.set((*entries, (crossing, obs)))
    _revl_write_model_decision(obs, validated)


def revl_host_usage(raw) -> tuple:
    """The HOST-REPORTED model/tokens/cost, best-effort read off a model
    completion's return value (item 121 §2.1). Byte-identical in behaviour to
    `revl.run._host_usage`, the driver's copy the trace uses; item 250 Slice 3a
    needs the same read at the crossing, on this side of the boundary, so the
    WAL record and the trace hop agree on what the host said. Pinned together by
    the Slice 3a tests.

    Recognized ONLY when the return is a mapping carrying the conventional keys
    (top-level, or a nested ``usage`` object for the token counts); anything
    else honest-degrades to ``None`` for every field. Returns
    ``(model, tokens_in, tokens_out, cost)``."""
    if not isinstance(raw, dict):
        return None, None, None, None
    usage = raw.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    tokens_in = raw.get("tokensIn", usage.get("tokensIn"))
    tokens_out = raw.get("tokensOut", usage.get("tokensOut"))
    cost = raw.get("cost")
    cost = cost if isinstance(cost, dict) else None
    return raw.get("model"), tokens_in, tokens_out, cost


def _revl_write_model_decision(obs: tuple, validated: bool) -> None:
    """Item 250 Slice 3a: make the completion the seam just measured DURABLE.

    Consumes this fiber's model-decision sink (published by the recorder beside
    the crossing key, see `revl_note_emission_index`) and hands it the SAME
    `llm` payload the trace hop carries (`revl_model_hop`), assembled from the
    revl-owned bracket and attempt count plus the host-reported usage. With no
    sink (recording off, no WAL attached, a hand-built timeline) this is a
    no-op and the decision lives only on the trace, exactly as before.

    Two trace fields are deliberately ABSENT here, stated rather than faked:
    `promptDigest` needs the compile-side taint certificate only the driver
    holds (item 444), and a digest written without that gate would be the
    CRITICAL item 121 §4 closes; `producedSeq` is a trace seq that does not
    exist on the WAL. Neither the prompt nor the response text is ever written.
    `outcome` says whether the response VALIDATED or the retry budget was
    EXHAUSTED (item 257): the crossing happened and cost tokens either way, so
    the record is written either way."""
    sink = _revl_model_decision_sink.get()
    if sink is None:
        return
    _revl_model_decision_sink.set(None)
    latency, attempts, ceiling, raw = obs
    model, tokens_in, tokens_out, cost = revl_host_usage(raw)
    llm = revl_model_hop(
        model=model, tokens_in=tokens_in, tokens_out=tokens_out, cost=cost,
        latency_seconds=latency, attempts=attempts, attempt_ceiling=ceiling,
        verified_by=[])
    sink(llm, "validated" if validated else "exhausted")


_REVL_ANY_CROSSING = object()


def revl_take_model_call(crossing=_REVL_ANY_CROSSING) -> "Optional[tuple]":
    """Consume and return a model-call observation from this fiber, or None.

    With a `crossing` — `(component, stepIndex)`, what the driver holds while
    recording one `emissionsCrossed` entry — this returns the observation
    measured AT THAT CROSSING and no other, so the newest-first walk order
    cannot hand a completion's numbers to a later non-model crossing (item 242).
    A crossing that carried no completion gets None and its record stays
    byte-identical to a pre-121 v2 emit.

    Called with no argument it consumes the most recent observation whatever its
    crossing — the pre-242 unkeyed behaviour, kept for the seam's own unit tests
    and for a caller that holds no crossing identity. Consuming always clears the
    entry, so a later emit cannot inherit a stale bracket either way."""
    entries = _revl_model_calls.get()
    if not entries:
        return None
    if crossing is _REVL_ANY_CROSSING:
        keep, (_, obs) = entries[:-1], entries[-1]
    else:
        hit = next((i for i, e in enumerate(entries) if e[0] == crossing), None)
        if hit is None:
            return None
        keep = entries[:hit] + entries[hit + 1:]
        obs = entries[hit][1]
    _revl_model_calls.set(keep)
    return obs


def revl_note_validated_completion(site: "Optional[str]",
                                   step_index: "Optional[int]" = None) -> None:
    """Mint the fiber-local value-flow token for completion `site`: the STEP
    INDEX of the model completion that site just validated in this fiber.

    `site` is the emitter's static identity for one `validated ... retry`
    completion call; keying on it (rather than on "the last completion in this
    fiber") is what keeps two completions in one body from cross-attributing.
    `step_index` defaults to the crossing the recorder last published; `None`
    (recording off, so no step index exists) CLEARS the site's token rather than
    minting a bogus one — honest-degrade, §2.2."""
    if site is None:
        return
    if step_index is None:
        step_index = _revl_last_emission_index.get()
    register = dict(_revl_validated_completions.get() or {})
    if step_index is None:
        register.pop(site, None)
    else:
        register[site] = step_index
    _revl_validated_completions.set(register)


def revl_produced_by(site: "Optional[str]") -> "Optional[int]":
    """The step index of the completion that `site` last validated in this
    fiber, or None (no token: recording off, a `Str`-returning non-validated
    completion, or a sibling fiber's token that this context never inherited)."""
    if site is None:
        return None
    return (_revl_validated_completions.get() or {}).get(site)


def revl_produced_emit(site: "Optional[str]", fn, *args, **kwargs):
    """Fire one boundary crossing whose ARGUMENTS the emitter proved read the
    binding produced by validated-completion `site` (§2.2, the static
    value-flow fact only the emitter can see).

    `fn` and `args` are already evaluated by the time this is called, so a
    crossing nested inside the arguments records BEFORE the marker is set and
    cannot consume it. The marker is fiber-local, consumed by the recorder at
    `record_emission`, and cleared here either way: with no token for `site`
    (recording off, or that site has not validated in this fiber) nothing is
    marked and the crossing is byte-identical to an unmarked one."""
    _revl_transparent_frame = True   # the recorder skips this frame for the site
    token = revl_produced_by(site)
    if token is None:
        return fn(*args, **kwargs)
    restore = _revl_pending_produced_by.set(token)
    try:
        return fn(*args, **kwargs)
    finally:
        _revl_pending_produced_by.reset(restore)


def revl_take_produced_by() -> "Optional[int]":
    """Consume this fiber's pending `producedBy` marker (or None). Consuming
    clears it, so exactly ONE crossing is stamped per marked call."""
    token = _revl_pending_produced_by.get()
    if token is not None:
        _revl_pending_produced_by.set(None)
    return token


def _revl_canonical_args_bytes(args) -> bytes:
    """A stable byte encoding of the revl-typed emission arguments (§2.3): the
    program-passed values the taint checker can see, NEVER the host-materialised
    request string (§4 attack 1, the CRITICAL)."""
    return json.dumps(args, separators=(",", ":"), sort_keys=True,
                      default=str, ensure_ascii=False).encode("utf-8")


def _revl_bytes_bucket(length: int) -> str:
    """A COARSE size bucket instead of an exact byte length, so the digest
    cannot serve as a length-narrowed confirmation oracle (§4 attack 4b)."""
    for hi, label in ((64, "0-64"), (256, "64-256"), (1024, "256-1k"),
                      (4096, "1k-4k"), (16384, "4k-16k"), (65536, "16k-64k")):
        if length < hi:
            return label
    return "64k+"


def revl_prompt_digest(args, arg_origins, taint_engaged: bool) -> "Optional[dict]":
    """The salted, suppressible prompt digest over the revl-typed args (§2.3, §4).

    `arg_origins` is the set of taint origins the checker attributes to the args
    (item 249/256), or None when the analysis is unavailable/disengaged.

    FAIL-CLOSED (§4 attack 1): a digest is emitted ONLY when taint analysis is
    engaged AND proves the args carry NEITHER a `secret` NOR a `confidential`
    origin. A secret/confidential arg SUPPRESSES the digest (returns None — the
    caller still records the hop, just without a digest); it NEVER refuses, so a
    `Secret[T]`-receiving model op stays a legal, compilable program (HIGH 2).
    Disengaged/unavailable analysis suppresses too (treated as unproven)."""
    if not taint_engaged or arg_origins is None:
        return None
    origins = set(arg_origins)
    if _REVL_SECRET_ORIGIN in origins or _REVL_CONFIDENTIAL_ORIGIN in origins:
        return None
    payload = _revl_canonical_args_bytes(args)
    mac = hmac.new(revl_digest_nonce(), payload, hashlib.sha256).hexdigest()
    return {"salted": "hmac-sha256:" + mac,
            "bytesBucket": _revl_bytes_bucket(len(payload)),
            "provenance": "revl-side-args"}


def revl_model_hop(*, model, tokens_in, tokens_out, cost, latency_seconds,
                   attempts, attempt_ceiling, verified_by,
                   produced_seq=None, prompt_digest=None,
                   model_id_cap: int = 256) -> dict:
    """Assemble the `llm` payload for a model completion crossing (§2.2), with
    every field's provenance fixed and not host-negotiable.

    `model`/`tokens`/`cost` are HOST-REPORTED and unverifiable (§4 attack 2);
    `latency` is a REVL-MEASURED BRACKET; `attempts`/`attemptCeiling` are
    REVL-CONTROLLED (the one cross-checkable number, §3.2). `model` is a
    length-capped opaque string, never parsed or trusted (§4 attack 4a).
    `produced_seq`/`prompt_digest` are attached only when present (both
    honest-degrade to absent)."""
    payload: dict = {
        "model": (str(model)[:model_id_cap] if model is not None else None),
        "modelProvenance": "host-reported",
        "tokensIn": tokens_in,
        "tokensOut": tokens_out,
        "usageProvenance": "host-reported",
        "latencySeconds": latency_seconds,
        "latencyProvenance": "revl-measured-bracket",
        "attempts": attempts,
        "attemptCeiling": attempt_ceiling,
        "attemptsProvenance": "revl-controlled",
        "verifiedBy": list(verified_by) if verified_by is not None else [],
    }
    if cost is not None:
        payload["cost"] = {"amount": cost.get("amount"),
                           "currency": cost.get("currency"),
                           "provenance": "host-reported"}
    if produced_seq:
        payload["producedSeq"] = list(produced_seq)
    if prompt_digest is not None:
        payload["promptDigest"] = prompt_digest
    return payload


# ---------------------------------------------------------------------------
# v2: realm placement (docs/design-v2-realms.md)
# ---------------------------------------------------------------------------

class _RealmLabel:
    """A realm identity. cordis compares isolate labels by object identity,
    so same-string sharing must go through one object — never rely on
    string interning."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<realm {self.name}>"


_REALM_LABELS: dict = {}


def realm_label(name: str) -> "_RealmLabel":
    """Process-wide string -> label-object registry: equal strings share a
    realm (the paper §5.2.1 global-realm convention)."""
    label = _REALM_LABELS.get(name)
    if label is None:
        label = _REALM_LABELS[name] = _RealmLabel(name)
    return label


def plug(ctx, component: dict, config=None):
    """Load an emitted component honoring its realm placements: apply
    ctx.isolate(key, label) per entry BEFORE ctx.plugin — the fiber's
    context chain is fixed at plugin time, so isolation cannot happen
    inside apply."""
    # item 443: an activation is a fresh batch of boundary crossings, so an
    # engaged E-Stop refuses to start one. Placed here rather than deeper
    # because refusing BEFORE `ctx.plugin` means no frame, no accumulator and
    # nothing to strand — the cheapest possible halt.
    _estop_check(f"plug {component.get('name') or '<component>'}")
    scoped = ctx
    for key, realm in (component.get("isolate") or {}).items():
        scoped = scoped.isolate(key, realm_label(realm))
    return scoped.plugin(component, config)


# ---------------------------------------------------------------------------
# instance-parametric components (docs/design-v2-instances.md)
# ---------------------------------------------------------------------------

# -- live-instance state migration (roadmap item 10, hot-swap with instances) -
#
# Hot-swapping a template `T` to `T'` while instances of `T` are live must
# reconcile each live instance's *state* onto `T'` (docs/design-v2-instances.md
# "Not in phase 1", question 6). The swap engine (src/revl/mcp/session.py) drives
# the reconciliation; this section is the runtime substrate it stands on:
#
#   * a **live-instance registry** so the engine can enumerate the live
#     instances of a swapped template (`live_instances(name)`);
#   * per-activation **resource tracking** so an instance's migratable state —
#     the stateful host resources it acquired (its `Map`, …) — can be captured
#     and restored (`SpawnHandle.capture_state` / `.restore_state`).
#
# All of it is inert unless something spawns: no spawn ⇒ no registry entries, and
# the activation hook only records resources while a body is actually running.

#: fibers whose body is *currently* activating — a stack, innermost last. A host
#: resource created inside a body (`Map.new()`) registers onto the top frame, so
#: it is attributed to the activation (hence the instance) that acquired it.
_ACTIVATING: list["Frame"] = []

#: ctx -> the Frame of the activation on that context. Weak-keyed so a torn-down
#: instance's frame is collected with its context. A `SpawnHandle` finds its
#: instance's frame here, via the fiber context it already holds.
_FRAME_BY_CTX: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

#: component name -> the live `SpawnHandle`s of that template, in spawn order.
#: A list (not a set): the swap engine correlates old instances to the new ones
#: the successor template spawns *positionally*, and spawn order is deterministic.
_LIVE_INSTANCES: "dict[str, list[SpawnHandle]]" = {}


def _register_resource(resource: Any) -> None:
    """Attribute a freshly-created host resource to the activation acquiring it.

    Called from `_Closable.__init__`; a no-op when nothing is activating (a
    resource made outside any component body — e.g. a bare test — is not
    instance state and is simply not tracked)."""
    if _ACTIVATING:
        _ACTIVATING[-1]._resources.append(resource)


def _frame_for_ctx(ctx: Any) -> "Optional[Frame]":
    try:
        return _FRAME_BY_CTX.get(ctx)
    except TypeError:  # pragma: no cover — non-weakrefable ctx
        return None


def live_instances(component: str) -> "list[SpawnHandle]":
    """The live instances of a template, in spawn order (the swap engine's
    enumeration point). A copy, so disposal during iteration is safe."""
    return list(_LIVE_INSTANCES.get(component, ()))


def _remember_instance(handle: "SpawnHandle") -> None:
    _LIVE_INSTANCES.setdefault(handle.component, []).append(handle)


def _forget_instance(handle: "SpawnHandle") -> None:
    bucket = _LIVE_INSTANCES.get(handle.component)
    if bucket is not None:
        try:
            bucket.remove(handle)
        except ValueError:
            pass
        if not bucket:
            _LIVE_INSTANCES.pop(handle.component, None)


class StateIncompatible(RuntimeError):
    """A live instance's captured state cannot migrate onto the successor
    template — the successor dropped or retyped a resource the instance held.
    Raised by `SpawnHandle.restore_state`; the swap engine turns it into an
    admission-style rejection and rolls back rather than dropping the state."""


class SpawnHandle:
    """The value a `spawn` acquisition binds: a live component instance, torn
    down by its own `.dispose()`.

    The instance is a child fiber of its spawner — its own nested teardown
    scope (item zero). `dispose()` unloads that fiber, running the instance's
    LIFO teardown *now*, independent of the spawner: a request-scoped instance
    is reclaimed when the request ends, never deferred to the component's
    teardown. Disposal is idempotent, so the spawner's own inverse
    (`yield lambda: s.dispose()`, or the frame-adopted safety net for a
    method-body spawn) is a harmless no-op once the instance is already gone."""

    __slots__ = ("_fiber", "component", "_disposed", "__weakref__")

    def __init__(self, fiber, component: str) -> None:
        self._fiber = fiber
        self.component = component
        self._disposed = False

    def dispose(self):
        """Unload the instance's fiber (its LIFO teardown). Returns the
        runtime's awaitable so a caller in an async context can `await` the
        reclamation; the emitted inverse is drained through the effect
        protocol, which already awaits an awaitable disposer."""
        if self._disposed:
            return None
        self._disposed = True
        _forget_instance(self)
        return self._fiber.dispose()

    # -- state migration (hot-swap with live instances) --------------------

    def _frame(self) -> "Optional[Frame]":
        """The instance's activation frame, which tracks the stateful host
        resources it acquired. `None` if the instance never activated (its
        provisions never came up) — nothing to migrate."""
        return _frame_for_ctx(self._fiber.ctx)

    def capture_state(self) -> list:
        """Snapshot this live instance's migratable state: the ordered vector
        of `(resource_type, state)` for each stateful host resource the
        instance acquired during activation. `state` is `None` for a resource
        with no `__revl_state__` (e.g. a `Pool`, whose checkout bookkeeping is
        transient) — its *type* is still pinned so the compat gate can require
        the successor to acquire an equivalent one.

        The vector's order is acquisition order (source order of the
        instance's `let-effect` steps), which the successor reproduces
        deterministically — that is what makes the positional restore sound."""
        frame = self._frame()
        resources = list(frame._resources) if frame is not None else []
        captured = []
        for res in resources:
            snap = res.__revl_state__() if hasattr(res, "__revl_state__") else None
            captured.append((type(res), snap))
        return captured

    def check_state(self, captured: list) -> None:
        """The state-compat gate, without mutating anything: raise
        `StateIncompatible` if a `capture_state()` vector cannot migrate onto
        this (fresh) instance, else return.

        The successor must have acquired a resource vector of the *same length*
        and, at each position, of the *same type*. Any divergence — a dropped
        resource, a retyped one, a changed count — means the successor cannot
        hold the predecessor's state, and silently losing it would be residue.
        Kept separate from `restore_state` so a whole cohort can be *checked*
        before any of it is *applied* — that is what makes a rejected migration
        roll back with nothing half-written."""
        resources = self._resource_vector()
        if len(resources) != len(captured):
            raise StateIncompatible(
                f"instance of {self.component!r} held {len(captured)} stateful "
                f"resource(s); the successor acquires {len(resources)} — state "
                f"cannot migrate without dropping or inventing a resource")
        for pos, (res, (old_type, _snap)) in enumerate(zip(resources, captured)):
            if type(res) is not old_type:
                raise StateIncompatible(
                    f"instance of {self.component!r}: resource #{pos} was "
                    f"{old_type.__name__}, the successor acquires "
                    f"{type(res).__name__} — retyped state cannot migrate")

    def restore_state(self, captured: list) -> None:
        """Migrate a checked `capture_state()` vector onto this instance: write
        each captured state back through its resource's `__revl_restore__`.
        Re-runs `check_state` so a direct caller is still gated."""
        self.check_state(captured)
        resources = self._resource_vector()
        for res, (_old_type, snap) in zip(resources, captured):
            if snap is not None:
                res.__revl_restore__(snap)

    def _resource_vector(self) -> list:
        frame = self._frame()
        return list(frame._resources) if frame is not None else []

    def get(self, key: str):
        """Read a provision the instance published, in *its* local realm.
        Only the spawner (which holds this handle) can reach it — a sibling
        instance, isolated into a different local realm, cannot (supervision-
        tree addressing, decision 1/2)."""
        return self._fiber.ctx.get(key)


def spawn(ctx, component: dict, config, realms):
    """Instantiate `component` at runtime as a child of the spawner, each key
    it provides isolated into a *fresh* LOCAL realm (unlabeled `ctx.isolate`,
    which mints a distinct identity per call). Two instances of one component
    therefore never collide on a provision — disjoint by construction, no
    config value known at link time (decision 3/5). Returns a
    :class:`SpawnHandle`."""
    scoped = ctx
    for key in realms or ():
        scoped = scoped.isolate(key)  # no label -> a fresh local realm per spawn
    fiber = scoped.plugin(component, dict(config or {}))
    handle = SpawnHandle(fiber, component.get("name"))
    _remember_instance(handle)  # so a hot-swap can enumerate live instances
    # If a recording context is threaded through the spawn, give it the chance to
    # wrap the handle so an emission reached off it (`emit s.inner.method()`) is
    # recorded in the WAL, the same as a required-service emission (replay.py's
    # `_SpawnRecorder`). The hook lives only on the recorder's context wrapper;
    # the real cordis `Context` returns None for a `_`-prefixed unknown name, so a
    # normal (un-recorded) activation is untouched. The registry above still holds
    # the REAL handle, so teardown and live-instance enumeration are unchanged.
    record = getattr(ctx, "_revl_record_spawn", None)
    if callable(record):
        return record(handle)
    return handle


# ---------------------------------------------------------------------------
# tracing (test/demo observability for the stub stdlib)
# ---------------------------------------------------------------------------

_trace: Optional[Callable[[str], None]] = None
_observers: list = []


def set_trace(callback: Optional[Callable[[str], None]]) -> None:
    """Install *the primary* trace callback (the original single-observer API).

    Replaces whatever a previous ``set_trace`` installed; ``set_trace(None)``
    clears it.  Observers registered through :func:`add_trace` live in a
    separate list and are **not** disturbed by this call, so a demo can hold
    its own subscription across a driver's install/teardown of the primary
    callback.
    """
    global _trace
    _trace = callback


def add_trace(callback: Callable[[str], None]) -> Callable[[], None]:
    """Subscribe an *additional* trace observer; returns its unsubscriber.

    Any number of observers may coexist.  Each event goes to the primary
    ``set_trace`` callback first (if any), then to every observer in
    subscription order.  The returned function is idempotent.
    """
    _observers.append(callback)
    state = {"live": True}

    def unsubscribe() -> None:
        if state["live"]:
            state["live"] = False
            try:
                _observers.remove(callback)
            except ValueError:  # already removed via remove_trace
                pass

    return unsubscribe


def remove_trace(callback: Callable[[str], None]) -> bool:
    """Unsubscribe an observer added with :func:`add_trace`."""
    try:
        _observers.remove(callback)
    except ValueError:
        return False
    return True


def trace_observers() -> int:
    """How many callbacks are currently receiving events (primary included)."""
    return len(_observers) + (1 if _trace is not None else 0)


# item 259 slice 2 (docs/design/259-checked-parallel-emissions.md §4.1, HIGH-1):
# a contextvar-backed record-sink STACK. `_record` reads MODULE GLOBALS, so three
# interleaving parallel branches could not each hold a distinct sink. The sink is
# BUILT here: each parallel branch pushes a task-local buffer onto this stack for
# the duration of its host call, so a mid-call `_record` lands in the branch
# buffer instead of the real observers; the join pops and REPLAYS each buffer to
# the real sink IN PLAN ORDER, so the observable trace is the sequential
# concatenation even though the host calls overlapped (C1). Each branch is a
# distinct asyncio Task with its own copied context, so the buffers never collide,
# and the stack composes under nesting (a parallel group inside a parallel group).
_revl_record_sinks: "contextvars.ContextVar[tuple]" = contextvars.ContextVar(
    "_revl_record_sinks", default=())


def _record(event: str) -> None:
    # The confidentiality funnel for the host trace (item 421 F6). A trace event
    # is an f-string this file already interpolated a key / sql / stream item
    # into, so `confidential.redact_value`, which funnels a VALUE, cannot be
    # applied at the sink; and redacting at each `_record` CALL SITE would be
    # exactly the "at each printer" discipline `confidential.py` exists to
    # replace, leaving whatever site is added next open. So the scrub happens
    # here, at the one choke point every event passes through, before the event
    # can reach a branch buffer, the `set_trace` sink (the operator console,
    # forwarded to the conductor's stdout) or any observer.
    #
    # The match is EXACT against the values a declared `Secret[T]` marking
    # registered, never a pattern, so ordinary trace is byte-identical and a
    # composition that declares no secret pays a single empty-set test.
    event = confidential.redact_text(event)
    sinks = _revl_record_sinks.get()
    if sinks:
        # innermost installed branch sink wins: buffer for a plan-order replay at
        # the parallel group's join rather than emitting to the real sink now.
        sinks[-1].append(event)
        return
    if _trace is not None:
        _trace(event)
    for observer in list(_observers):
        observer(event)


def mark_secret(*values) -> None:
    """Remember that these values crossed at a declared `Secret[T]` receiver
    (item 421 F6). Emitted at the head of every provide method implementing an
    operation whose IR params carry `secret: true`.

    Why here and not only in the recorder: `replay.redact_args` already
    registers a declared receiver's argument, but the recorder is engaged only
    under `revl run --record` / `--wal` / an MCP `revl_load(record=True)`. A
    plain `revl run` prints the SAME host trace with no recorder attached, which
    is exactly the console the audit read the secret off, so the positional
    marking has to fire from the emitted program itself. Registration is
    idempotent and exact-valued, so doing it in both places costs one set
    insert."""
    for value in values:
        confidential.register_secret_tree(value)


def secret_result(fn):
    """Decorator on an extern whose DECLARED return was `Secret[T]`: item 256
    §7a's origin, where a confidential value enters the value world.

    `mark_secret` covers the RECEIVER end of a crossing, which is enough when the
    value crosses one; it is not enough for the body that PRODUCES it and then
    uses it locally (`let t = emit mint(); effect store.insert(t, v)`, the shape
    the audit read off the console). Marking at the origin covers both, and is
    the narrowest place to do it: one wrapper per declared extern, not one per
    call site, and the verbatim `@py` body is untouched.

    Registration is the ONLY effect: the value is returned unchanged, so the
    program's semantics are byte-identical and only what a trace or a seam error
    may SAY about it changes."""
    if inspect.iscoroutinefunction(fn):
        async def _secret_result_async(*args, **kwargs):
            value = await fn(*args, **kwargs)
            confidential.register_secret_tree(value)
            return value
        _secret_result_async.__name__ = fn.__name__
        _secret_result_async.__qualname__ = fn.__qualname__
        _secret_result_async.__doc__ = fn.__doc__
        return _secret_result_async

    def _secret_result_sync(*args, **kwargs):
        value = fn(*args, **kwargs)
        # A sync extern that hands back an awaitable is a colour error the ref
        # thunk already refuses; here we simply do not touch it, so the wrapper
        # never awaits on a caller's behalf.
        if not inspect.isawaitable(value):
            confidential.register_secret_tree(value)
        return value
    _secret_result_sync.__name__ = fn.__name__
    _secret_result_sync.__qualname__ = fn.__qualname__
    _secret_result_sync.__doc__ = fn.__doc__
    return _secret_result_sync


# ---------------------------------------------------------------------------
# item 259 slice 2: checked parallel emissions - the py runtime fan-out
# ---------------------------------------------------------------------------
#
# A parallel GROUP is a run of emissions the checker proved independent (disjoint
# declared capabilities, or same-key `commutative`). The emitter renders a group
# of size > 1 as `_revl_parallel([...])` followed by a PLAN-ORDER join. Three
# obligations shape the helpers below (docs/design/259-checked-parallel-
# emissions.md §3, §4.1):
#
#   * Concurrency. `_revl_parallel` fires every branch under the ONE cordis event
#     loop via `asyncio.gather` - no threads (schedule.py decision 3), so
#     "concurrent" means the branches' `await` points interleave cooperatively:
#     three host round trips are in flight at once, ~max(latencies) not sum.
#
#   * Byte-identical audit (C1). Host extern bodies call `_record` mid-fire, so a
#     naive gather would interleave those records nondeterministically. Each
#     branch instead runs under a branch-local record sink (the contextvar stack
#     above); its mid-fire records buffer, and the join replays them in PLAN
#     ORDER. Records emitted OFF the awaiting task (Clock.fire / Job completion /
#     spawn) would escape the buffer, which is why the emitter admits only
#     emissions whose records are produced synchronously on-task (§3.2).
#
#   * Teardown-EFFECT equivalence, NOT byte-identical `accumulated` (§3.3, the
#     CRITICAL). A branch that faults or is diverted changes the fired-and-
#     registered SET versus a sequential early exit. `_revl_parallel` always
#     drives the WHOLE group to quiescence (every branch captured, none left
#     in-flight to race teardown); the emitted join then registers each
#     SUCCESSFUL branch's compensation in plan order and `_revl_raise_first`
#     re-raises the first fault. The emitter forms a group only from members with
#     idempotent forward delivery and idempotent-or-absent compensation, so
#     over-firing a member under a fault or an A1 divert and then compensating it
#     leaves the same world state a skipped sequential tail would. Byte-identical
#     `accumulated` is neither claimed nor needed for the fault/divert path.


class _RevlBranchResult:
    """One parallel branch's captured outcome, replayed at the join in plan order.

    `records` is the branch-local audit buffer; `ok`/`value` carry a clean result
    and `error` a fault. A divert (a deadline / sibling-fault / cancel that lands
    at the branch's `await`) arrives as a ``CancelledError`` and rides the same
    `error` slot, so the join treats a diverted branch exactly like a faulted one:
    it does not register that member's compensation, and re-raises to unwind."""

    __slots__ = ("ok", "value", "error", "records")

    def __init__(self, ok: bool, value: Any, error: Optional[BaseException],
                 records: list) -> None:
        self.ok = ok
        self.value = value
        self.error = error
        self.records = records


async def _revl_branch(thunk: Callable[[], Any]) -> _RevlBranchResult:
    """Fire one group member under a branch-local record sink and CAPTURE its
    outcome - never raise. A fault or a divert (``CancelledError``) is caught and
    surfaced at the join in plan order, so `gather` always drives every branch to
    quiescence and no in-flight branch races the activation's teardown.

    The sink is pushed inside this coroutine, which `gather` runs as its own Task
    with a copied context, so the push is task-local and sibling branches never
    see each other's buffer."""
    buffer: list = []
    token = _revl_record_sinks.set(_revl_record_sinks.get() + (buffer,))
    try:
        value = thunk()
        if inspect.isawaitable(value):
            value = await value
        return _RevlBranchResult(True, value, None, buffer)
    except asyncio.CancelledError as exc:  # a divert landing at this branch's await
        return _RevlBranchResult(False, None, exc, buffer)
    except Exception as exc:  # noqa: BLE001 - re-raised in plan order at the join
        return _RevlBranchResult(False, None, exc, buffer)
    finally:
        _revl_record_sinks.reset(token)


async def _revl_parallel(thunks) -> list:
    """Fire a group's branches concurrently under the single cordis loop and
    rejoin. Returns the branch outcomes in the SAME order as `thunks` (plan
    order), regardless of completion order, so the emitted join is single-threaded
    and in plan order.

    Every branch captures its own outcome (`_revl_branch` never raises), so the
    gather always completes: the whole group is driven to quiescence even when a
    member faults or is diverted (§3.3, §5)."""
    thunks = list(thunks)
    if not thunks:
        return []
    return list(await asyncio.gather(*(_revl_branch(t) for t in thunks)))


def _revl_flush(records) -> None:
    """Replay one branch's buffered audit records to the sink, in order. Routing
    back through `_record` nests correctly under an OUTER branch sink (a group
    inside a group), because this branch's own sink is already popped by now."""
    for event in records:
        _record(event)


def _revl_raise_first(outcomes) -> None:
    """Re-raise the first faulted-or-diverted branch's error in PLAN order, after
    every successful branch's compensation has been registered at the join. A
    clean group is a no-op. Raising here (not inside `_revl_parallel`) is what
    lets the join register the fired members' compensations FIRST, so the
    subsequent L-Raise teardown unwinds a correctly-ordered stack (P/G7)."""
    for outcome in outcomes:
        if not outcome.ok:
            raise outcome.error


# ---------------------------------------------------------------------------
# fault probe: instrumentation for `fault test` (docs/fault-tests.md)
# ---------------------------------------------------------------------------


class FaultProbe:
    """Records one component activation's inverse accumulation and replay.

    A `fault test` needs two orders to compare: the order in which the
    activation *accumulated* its inverses, and the order in which the runtime
    actually *ran* them while unwinding.  Both are observed here rather than
    reconstructed from the host trace, because a host trace only sees effects
    that touch a stub builtin — a `provide` withdrawal or a pure closure undo
    leaves no trace event at all, and those are exactly the inverses an
    L-Raise regression would drop.

    The probe wraps the component body generator installed by
    :meth:`Frame.install` and tags every yielded disposer with its
    accumulation index; the wrapper appends that index to :attr:`ran` when the
    runtime disposes it.  R1/A8 hold iff ``ran == list(reversed(accumulated))``.

    Only the component named at arming time is instrumented, so siblings in
    the same composition run exactly as they normally would.
    """

    __slots__ = ("component", "accumulated", "ran", "_n", "frame")

    def __init__(self, component: str) -> None:
        self.component = component
        self.accumulated: list = []   # accumulation indices, in order
        self.ran: list = []           # the same indices, in disposal order
        self._n = 0
        # the activation frame this probe instrumented, so the harness can
        # read the merged residue (`compensation_residue`) the teardown
        # recorded. A Phase-1 inverse that RAISED is residue the probe's own
        # `ran`/`never_ran` counters cannot see: continue-and-record means the
        # inverse DID run, it just did not land (docs/design/teardown-
        # contract.md). Without this the judge would call such a teardown
        # clean — strictly less honest than the pre-guard skip it replaces.
        self.frame: Optional["Frame"] = None

    # -- instrumentation ---------------------------------------------------

    def _tag(self, value: Any, frame: "Frame") -> Any:
        """Wrap one yielded disposer; pass anything else through untouched."""
        if not callable(value):
            return value            # `yield None` — an A1 iteration boundary
        if getattr(value, "__self__", None) is frame:
            return value            # `yield frame.drain` — the accumulator itself
        self._n += 1
        index = self._n
        self.accumulated.append(index)

        def _instrumented(_index=index, _undo=value):
            self.ran.append(_index)
            return _undo()

        # keep the underlying entry reachable through the wrapper: `Frame`'s
        # Phase-1 guard (which sits OUTSIDE this instrumentation) reads it to
        # pick the residue severity and name the inverse.
        _instrumented._revl_entry = value
        return _instrumented

    def instrument(self, body: Callable, frame: "Frame") -> Callable:
        """Return a body generator function equivalent to *body*, tagged.

        cordis iterates an effect body with a plain ``for``/``async for`` and
        never ``send``s or ``throw``s into it (see cordis fiber ``_execute``),
        so a re-yielding wrapper is protocol-faithful.
        """
        self.frame = frame
        if inspect.isasyncgenfunction(body):
            async def _async_wrapper():
                async for value in body():
                    yield self._tag(value, frame)
            return _async_wrapper

        def _wrapper():
            for value in body():
                yield self._tag(value, frame)
        return _wrapper

    # -- results -----------------------------------------------------------

    def lifo_violation(self) -> Optional[tuple]:
        """``(position, ran_index, expected_index)`` for the first inverse that
        broke LIFO, or ``None`` when the replay was exact.

        ``ran_index`` is ``None`` when the replay simply stopped early — an
        inverse that never ran is a LIFO violation too, and the more serious
        one, so it is reported here rather than silently passing.
        """
        expected = list(reversed(self.accumulated))
        for position, index in enumerate(self.ran):
            if position >= len(expected):
                return (position + 1, index, None)
            if expected[position] != index:
                return (position + 1, index, expected[position])
        if len(self.ran) != len(expected):
            return (len(self.ran) + 1, None, expected[len(self.ran)])
        return None

    def never_ran(self) -> list:
        """Accumulation indices whose inverse was never disposed."""
        ran = set(self.ran)
        return [index for index in self.accumulated if index not in ran]

    def residue(self) -> list:
        """The merged residue records the instrumented activation's teardown
        left behind — a Phase-1 inverse that raised (`bracket-fault` /
        `restore-residue`) or a Phase-2 offset that did not land
        (`compensation-residue`). Empty for a teardown that landed clean."""
        frame = self.frame
        return list(frame.compensation_residue) if frame is not None else []


_fault_probe: Optional[FaultProbe] = None


def arm_fault_probe(component: str) -> FaultProbe:
    """Instrument the next activation of *component*; returns the probe.

    Process-global and single-slot on purpose: a fault test drives exactly one
    activation at a time, and a leftover probe from a crashed run is visible
    rather than silently accumulating.
    """
    global _fault_probe
    _fault_probe = FaultProbe(component)
    return _fault_probe


def disarm_fault_probe() -> None:
    global _fault_probe
    _fault_probe = None


# ---------------------------------------------------------------------------
# adapter: the component accumulator
# ---------------------------------------------------------------------------


def _named_call_method(undo: Callable[[Any], Any]) -> str:
    """Best-effort inverse name for a WAL discharge-descriptor's `call.method`.

    243's grammar declares `undo` as an ordinary pure-expression call (docs/
    design/243-witnessed-externs.md, note 1): `_witnessed_step` in emit.py
    compiles the call site to `lambda result: <declared undo>(...)`, so the
    lambda's own bytecode references the declared inverse's name as a global
    lookup — `co_names[0]` recovers it without needing the call-site metadata
    the (separate, parallel) lower.py enablement slice will eventually thread
    through. Falls back to the callable's own `__name__` (or a fixed label)
    for anything that is not a plain generated lambda, e.g. a hand-built test
    fixture — never raises, this is best-effort naming for the descriptor,
    not the replay path itself."""
    code = getattr(undo, "__code__", None)
    if code is not None and code.co_names:
        return code.co_names[0]
    return getattr(undo, "__name__", None) or "undo"


class _Transactional:
    """The disposer for one witnessed (transactional) effect (item 243).

    A bracket disposer replays its inverse on every teardown — clean unload and
    abort alike — because releasing an acquired handle is always right. A
    transactional disposer is different: the mutation IS the deliverable, so on
    a clean COMMIT (the activation completed and is now unloading cleanly) the
    inverse must NOT run and the witness is dropped; only an ABORT (a
    mid-activation failure that unwinds before the activation ever committed)
    replays it. Both branches drop the inverse and witness references so a
    discharged transaction leaves no rollback state alive (witness GC).

    The commit signal is read from the owning frame at disposal time, not
    captured at registration, because whether the activation commits is not yet
    known when the effect runs — it depends on whether a LATER step aborts."""

    # `_estop_stranded` (item 443): this entry is already on the halt
    # inventory. The inventory is built in two halves — named at the halt, then
    # completed at `Frame._guard` as the unwind hands disposers over — so the
    # flag is what keeps an entry from being counted twice.
    __slots__ = ("frame", "witness", "_undo", "discharged", "replayed", "seq",
                 "_escrowed", "stamp", "undo_idempotent", "component", "method",
                 "_estop_stranded")

    def __init__(self, frame: "Frame", undo: Callable[[Any], Any], witness: Any,
                 undo_idempotent: bool = False) -> None:
        self.frame = frame
        self._undo = undo
        self.witness = witness
        # the crossing's identity, captured HERE at registration and never
        # re-read at teardown (teardown-contract.md, "No data hazard") — the
        # compensation entry's own `component`/`method` pair, so a Phase-1
        # `restore-residue` record can NAME the restore that failed even
        # though `__call__` has already dropped `_undo` by then.
        self.component: Optional[str] = frame.name
        self.method: Optional[str] = _named_call_method(undo)
        # item 309: whether the author declared `undo idempotent`. A declared
        # inverse replays freely and needs NO fence; an undeclared one is fenced
        # before its Phase-1 apply so recover cannot double-apply it after an
        # abort-then-crash (option a, §3a). Absent (`False`) is every pre-309
        # witnessed extern, so the abort path is byte-identical for them.
        self.undo_idempotent = undo_idempotent
        self.discharged = False   # committed: inverse skipped, mutation persists
        self.replayed = False     # aborted: inverse ran, mutation reverted
        # item 247 second-pass (F5): process-monotonic registration index, so an
        # escrow with no WAL (every seq is None) still replays LIFO.
        self.stamp = next(_ENTRY_STAMP)
        # the WAL discharge-descriptor's `seq` this entry was registered under
        # (bridge slice: connects Slice 2a to the WAL/recover foundation), or
        # `None` when no WriteAheadLog is attached (a plain run) — see
        # `Frame._wal`/`Frame.transactional`.
        self.seq: Optional[int] = None
        # item 245: escrowed under a session owner whose verdict is still
        # pending (a mid-session withdrawal). Held once — neither discharged nor
        # replayed — until the owner settles the verdict and disposes it.
        self._escrowed = False
        self._estop_stranded = False   # item 443

    def __call__(self) -> Any:
        if _hold_for_session(self):
            return None
        if self.frame._committed:
            # commit: discharge. The mutation stays; drop the inverse + witness
            # so no rollback state survives a committed transaction (GC).
            self.discharged = True
            self._undo = None
            self.witness = None
            return None
        # abort: replay the declared inverse against the captured witness, then
        # drop both references (idempotent single-shot, so a re-entrant unwind
        # or a `revl recover` pass does not run it twice).
        self.replayed = True
        undo, self._undo = self._undo, None
        witness, self.witness = self.witness, None
        if undo is None:  # pragma: no cover — single-flight guard
            return None
        # item 309 §3a, option (a): fence an UNDECLARED inverse's own Phase-1
        # apply. The fence is fsync-appended to the WAL BEFORE the inverse runs,
        # so if the process dies here (abort-then-crash), `revl recover` finds
        # the fence, does NOT re-apply, and reports the honest fenced residue —
        # at-most-once holds across abort-then-crash. A DECLARED-idempotent
        # inverse replays freely and writes no fence (a payoff of declaring).
        if not self.undo_idempotent and self.seq is not None:
            wal = self.frame._wal()
            if wal is not None:
                wal.record_fence(self.seq)
        return undo(witness)


#: Phase-2 bound config surface (docs/design/teardown-contract.md, "the
#: bound rule"): two environment variables in the existing `REVL_*`
#: convention, identical spelling on every tier, read once per activation
#: (`Frame.__init__`) so a change mid-run cannot move an already-aborting
#: activation's deadline. Provisional defaults pinned by the contract
#: (open question 1): budget 5000 ms, per-call 1000 ms.
_COMPENSATION_BUDGET_ENV = "REVL_COMPENSATION_BUDGET_MS"
_COMPENSATION_PER_CALL_ENV = "REVL_COMPENSATION_PER_CALL_MS"
_COMPENSATION_BUDGET_DEFAULT_MS = 5000
_COMPENSATION_PER_CALL_DEFAULT_MS = 1000


def _read_bound_seconds(env_name: str, default_ms: int) -> Optional[float]:
    """Read one `REVL_*_MS` bound: unset -> the tier-uniform default; `0` ->
    no bound (`None`, teardown-contract.md: "the between-compensation check
    still runs and records nothing expired"); an unparsable value -> the
    default, same as unset (never crash an activation over a malformed
    host env var). Returns seconds, `time.monotonic`'s unit."""
    raw = os.environ.get(env_name)
    if raw is None:
        ms = default_ms
    else:
        try:
            ms = int(raw)
        except ValueError:
            ms = default_ms
    if ms == 0:
        return None
    return ms / 1000.0


#: The merged residue schema's `kind` discriminator (docs/design/teardown-
#: contract.md, "The merged residue schema"), one spelling per tier. Phase 1
#: has two severities — a failed BRACKET inverse is contract-grade
#: (`bracket-fault`: the inverse claimed G5 infallibility and lied), a failed
#: WITNESSED restore is the anticipated case (`restore-residue`, 243 rule 6).
#: Phase 2's best-effort offset is `compensation-residue`.
_BRACKET_FAULT = "bracket-fault"
_RESTORE_RESIDUE = "restore-residue"
_COMPENSATION_RESIDUE = "compensation-residue"


def _residue_record(entry: Any, *, outcome: str,
                    attempted_flag: bool, attempted: Optional[dict],
                    error: dict, kind: str = _COMPENSATION_RESIDUE,
                    component: Optional[str] = None,
                    method: Optional[str] = None) -> dict:
    """One merged-residue fact (item 247 gap 2, design Decision 2).

    A best-effort offset that raised (`failed`) or never got to run under the
    Phase-2 budget (`not-attempted`) is residue: a crossing whose compensation
    was OWED but did not land. So is a Phase-1 inverse that RAISED — the
    bracket/witnessed arm, `kind` `bracket-fault`/`restore-residue`. Every
    such record carries `state: "unresolved"` — the third audit state joining
    `bare`/`compensated` — and NAMES the crossing it was offsetting or
    reverting (`component`, `method`, WAL `seq`), so the audit surface
    (`revl.query`/`revl.erase_report`) and the 246 session-boundary report can
    enumerate it rather than let an in-memory list silently grow.

    `component`/`method` default to the entry's own identity; a Phase-1
    BRACKET disposer is a bare closure with no entry object behind it, so the
    frame passes its own name and a best-effort inverse label instead."""
    return {
        "kind": kind,
        "state": "unresolved",
        "component": getattr(entry, "component", None) if component is None else component,
        "method": getattr(entry, "method", None) if method is None else method,
        "seq": getattr(entry, "seq", None),
        "attemptedFlag": attempted_flag,
        "attempted": attempted,
        "outcome": outcome,
        "error": error,
    }


def _inverse_label(disposer: Any) -> Optional[str]:
    """A best-effort name for the inverse a Phase-1 disposer runs, for the
    residue record's `method` field.

    An emitted bracket disposer is a bare `lambda: <undo>` (backends/python/
    emit.py), so there is no entry object carrying an identity. The lambda's
    own code object does carry one: the undo is a call, and the callee's name
    is the last global/attribute the closure loads (`lambda: a.close()` ->
    `close`, `lambda: blow('x')` -> `blow`). Best-effort by construction —
    `None` when nothing is readable — but it is what lets the record NAME the
    inverse that faulted instead of reporting an anonymous failure."""
    entry = getattr(disposer, "_revl_entry", disposer)
    method = getattr(entry, "_revl_method", None) or getattr(entry, "method", None)
    if method is not None:
        return method
    code = getattr(entry, "__code__", None)
    names = getattr(code, "co_names", ()) if code is not None else ()
    return names[-1] if names else None


def _phase1_kind(disposer: Any) -> str:
    """The residue severity for a Phase-1 disposer that raised. A witnessed
    (transactional) restore is the ANTICIPATED failure (`restore-residue`,
    243 rule 6); anything else on the Phase-1 stack is a bracket inverse that
    claimed G5 infallibility, so its raise is contract-grade
    (`bracket-fault`) — teardown-contract.md, "the two severities"."""
    entry = getattr(disposer, "_revl_entry", disposer)
    if isinstance(entry, _Transactional):
        return _RESTORE_RESIDUE
    if isinstance(entry, _Compensation):
        return _COMPENSATION_RESIDUE
    return _BRACKET_FAULT


class _Compensation:
    """The disposer for one compensation entry (item 247, docs/design/
    teardown-contract.md 'the three entry kinds, one stack'). Registered at
    an `emit ... compensate ...` step, it joins the SAME per-activation LIFO
    disposer stack as bracket and transactional entries — there is no second
    list (the contract's decision 1: the distinction lives in the entry, not
    in a separate structure).

    A compensation offsets an EMISSION that already crossed the boundary
    (mail sent, message published); it cannot be reversed, only offset. Like
    a transactional entry, a clean COMMIT discharges it — it never runs,
    because the emission itself is the deliverable and best-effort cleanup
    on success would be wrong. Unlike a transactional entry, an ABORT does
    NOT run it inline during the Phase-1 unwind: interleaved with the proof
    inverses in one pass, a raising compensation could leave a LATER (older)
    bracket/transactional inverse un-run, which is strictly worse residue
    (teardown-contract.md, "why two phases", reason 1). So `__call__` — the
    Phase-1 encounter, invoked by cordis's own LIFO disposal — never invokes
    `fn`. It only ENQUEUES this entry on the owning frame's
    `_pending_compensations`, in the exact order cordis's reverse-registration
    unwind encounters it (already the correct Phase-2 LIFO order — newest
    compensation first), and returns immediately, a Phase-1 no-op.

    `Frame.install`'s post-unwind hook drains that queue — actually invoking
    `fn` — only after the WHOLE Phase-1 pass has finished, via
    `_run_phase2`. This is the "compensation disposer enqueues itself ...
    and drains the queue in a post-unwind hook" mechanism teardown-
    contract.md names as natural on a cordis tier, where the runtime unwinds
    every disposer in one synchronous stack-position pass."""

    # `_estop_stranded`: see `_Transactional.__slots__` (item 443).
    __slots__ = ("frame", "fn", "discharged", "ran", "failed", "error", "seq",
                 "_escrowed", "component", "method", "stamp", "_estop_stranded")

    def __init__(self, frame: "Frame", fn: Callable[[], Any],
                 method: Optional[str] = None) -> None:
        self.frame = frame
        self.fn = fn
        self.discharged = False   # committed: never runs, deliverable persists
        self.ran = False          # Phase 2: actually invoked (any outcome)
        self.failed = False       # Phase 2: invoked and raised (continue-and-record)
        self.error: Optional[BaseException] = None
        # item 247 gap 2: the offset emission's identity, so a residue record
        # NAMES the crossing it was offsetting on the audit surface (design
        # Decision 2: "the record names the original emission it was offsetting").
        # `component` is the activation the compensation belongs to; `method` is
        # the offsetting call the emitter wrote (from `_named_call_method`).
        self.component: Optional[str] = frame.name
        self.method: Optional[str] = method
        # the WAL discharge-descriptor's `seq` this entry was registered
        # under (mirrors `_Transactional.seq`), or `None` when no
        # WriteAheadLog is attached.
        self.seq: Optional[int] = None
        # item 245: escrowed under a session owner with a pending verdict.
        self._escrowed = False
        self._estop_stranded = False   # item 443
        # item 247 second-pass (F5): process-monotonic registration index, so an
        # escrow with no WAL (every seq is None) still replays LIFO.
        self.stamp = next(_ENTRY_STAMP)

    def __call__(self) -> Any:
        if _hold_for_session(self):
            return None
        if self.frame._committed:
            # commit: discharge. The emission was the deliverable; a
            # best-effort offset on success would be wrong to run.
            self.discharged = True
            self.fn = None
            return None
        # abort, Phase-1 encounter: defer to Phase 2 — do not invoke `fn`
        # here. Enqueueing (rather than running immediately) is what keeps a
        # failing compensation from ever interrupting proof replay.
        self.frame._pending_compensations.append(self)
        return None

    def _run_phase2(self) -> None:
        """Phase 2 only: actually invoke the compensation. Best-effort — it
        catches and records rather than raising, so one failed offset never
        fails the abort and never blocks the remaining ones (teardown-
        contract.md's continue-and-record rule, `compensation-residue`
        severity)."""
        self.ran = True
        fn, self.fn = self.fn, None
        if fn is None:  # pragma: no cover — single-flight guard
            return
        try:
            fn()
        except BaseException as error:  # noqa: BLE001 — anticipated, best-effort
            self.failed = True
            self.error = error


# ---------------------------------------------------------------------------
# item 245: the session commit protocol (docs/design/245-session-commit.md)
# ---------------------------------------------------------------------------
#
# A session is one driver lifetime = one WAL. The driver registers a
# `SessionOwner` around the session (`set_session_owner`), and every `Frame`
# built while one is registered joins its live-frame registry. The owner holds
# the three session-scoped structures the commit verb derives its GATE TARGET
# from — the deferral queue, the discharge escrow, the live-frame registry —
# never the runtime's per-call current frame (`_FRAME_BY_CTX`).
#
# The owner is process-global and single-slot, exactly like `_trace`: one driver
# runs a session at a time. When NO owner is registered (`_SESSION_OWNER is
# None`), every `Frame` behaves byte-identically to the pre-245 world — a clean
# drain discharges at unload (implicit commit), the compatibility clause.

_SESSION_OWNER: "Optional[SessionOwner]" = None


def set_session_owner(owner: "SessionOwner") -> None:
    """Register the session's commit-state owner for the frames built next.

    The driver calls this once at session start, before loading the
    composition, so each activation `Frame.__init__` joins the owner's registry.
    Idempotent-ish: replacing an owner mid-session is a caller error, not
    guarded here (one driver owns one session)."""
    global _SESSION_OWNER
    _SESSION_OWNER = owner


def clear_session_owner() -> None:
    """Unregister the session owner (the driver calls this at commit/abort/unload
    end). Frames built afterwards get the pre-245 implicit-commit semantics."""
    global _SESSION_OWNER
    _SESSION_OWNER = None


def session_owner() -> "Optional[SessionOwner]":
    return _SESSION_OWNER


def _hold_for_session(entry: Any) -> bool:
    """Whether a transactional/compensation entry must be HELD now rather than
    discharged or replayed (item 245's escrow). It holds iff its frame has a
    session owner whose verdict is still pending AND the frame is not aborting —
    i.e. this is a MID-SESSION withdrawal (a swap/undo), whose entries wait for
    the session verdict rather than committing at the withdrawal. A held entry
    escrows itself once so the owner disposes it when the verdict settles.

    A terminal teardown (the owner's own commit/abort/unload) sets the verdict
    BEFORE disposing, so nothing holds there — the per-frame `_committed` /
    `_aborting` bits govern discharge-vs-replay exactly as before. An aborting
    frame never holds: its inverses replay immediately."""
    frame = entry.frame
    owner = frame._owner
    if owner is None or owner._verdict is not None or frame._aborting:
        return False
    # `_holding` is the discriminator between the two ways a frame's inverses
    # can run with the verdict still pending. It is set ONLY by `drain`'s
    # mid-session-withdrawal branch (the activation completed and is being torn
    # down while the session continues), never by a mid-ACTIVATION failure
    # (the body raised before `drain` — the classic 243 abort, whose inverses
    # replay immediately). Without this a failed activation under a session
    # owner would wrongly escrow its inverse instead of reverting.
    if not frame._holding:
        return False
    if not entry._escrowed:
        entry._escrowed = True
        owner.escrow(entry)
    return True


# ---------------------------------------------------------------------------
# item 443: the operator E-Stop (docs/design/443-estop.md)
# ---------------------------------------------------------------------------
#
# Every other stop revl has is COOPERATIVE: a teardown replays inverses LIFO
# under the activation's verdict (`commit`/`abort`), faults route through
# residue records, withdrawal propagates to dependents. That is right for a
# composition fault and wrong for an operator emergency: when a human hits the
# button during a runaway loop with irreversible effects in flight, unwinding
# two hundred brackets first is not a safe answer, it is a long one.
#
# So the E-Stop is a THIRD verdict, `halted` (`formal/RevL/Semantics.lean`,
# `Verdict.halted`), and its whole content is what it does NOT do:
#
#   * it replays no inverse             (`RevL.G7.estop_replays_nothing`)
#   * it discharges no entry            (`RevL.G7.estop_discharges_nothing`)
#   * it STRANDS every registered entry (`RevL.G7.estop_strands_everything`)
#
# Stranded is the third disposition: registered, not run, and NOT dropped —
# the inverse descriptor and the witness are KEPT, which is the exact opposite
# of a discharge, because `revl recover` is what reads them back. An E-Stop is
# deliberately shaped to look like a CRASH to the recovery path, since revl
# already has a proven answer for a crash.
#
# The halt violates G7's LIFO-completeness and R4's no-residue BY DESIGN, so
# the system records what it did not unwind instead of pretending it did.

_ESTOP_LATCH_ENV = "REVL_ESTOP_LATCH"

#: The merged residue schema's two E-Stop `kind` discriminators (docs/design/
#: teardown-contract.md, "The merged residue schema"). `estop-stranded` is a
#: registered entry whose inverse or compensation was NEVER ATTEMPTED;
#: `estop-ambiguous` is the at-most-one crossing that was already dispatched
#: when the latch was read and whose outcome is therefore UNKNOWN — item 440's
#: ambiguous tier, reached deliberately rather than by accident.
_ESTOP_STRANDED = "estop-stranded"
_ESTOP_AMBIGUOUS = "estop-ambiguous"

#: The engaged halt, or None. Process-global by construction: an E-Stop that
#: only stopped one activation would not be a stop.
_ESTOP: Optional[dict] = None

#: An explicit cross-process latch path (`revl run --estop-latch`), or None to
#: fall back to `REVL_ESTOP_LATCH`. `revl estop` creates the file; every
#: crossing seam stats it, so an operator in another terminal can halt a
#: running composition without a control socket.
_ESTOP_LATCH: Optional[str] = None

#: The crossings currently DISPATCHED and unconfirmed, innermost last. At most
#: one is unconfirmed per activation at any instant, which is what bounds the
#: halt's ambiguity to a single record (`RevL.G7.halt_ambiguity_is_at_most_one`).
_INFLIGHT: list = []

#: Every live activation frame, weakly held so a torn-down one falls out. The
#: halt walks this to build its inventory.
_LIVE_FRAMES: "weakref.WeakSet" = weakref.WeakSet()


class EstopRefused(RuntimeError):
    """`estop()` was called without operator authority.

    The whole point of the E-Stop is that a composition (or an agent driving
    one) may not invoke it on itself: it is held as an OPERATOR authority
    (item 55, `docs/operator-capabilities.md`, the `estop` verb). There is
    deliberately no in-language surface for it — no extern, no stdlib binding
    — and this refusal is the defensive twin for an embedding that reaches the
    module function directly."""


class EstopHalted(RuntimeError):
    """Raised at a boundary-crossing seam once an E-Stop is engaged.

    This is the "stop dispatching NEW crossings immediately" half. It is NOT a
    fault the composition may catch and route through the normal teardown: the
    halt is already engaged when this raises, so every disposer the unwind
    reaches is stranded rather than replayed."""

    def __init__(self, where: str) -> None:
        halt = _ESTOP or {}
        super().__init__(
            f"E-STOP engaged — `{where}` was refused. "
            f"reason: {halt.get('reason', 'operator halt')}; "
            f"operator: {halt.get('operator', 'unknown')}. "
            "The instance is dead: reconcile with `revl recover --wal <file>` "
            "(item 443); there is no resume.")
        self.where = where
        self.halt = dict(halt)


def arm_estop_latch(path: Optional[str]) -> None:
    """Point the crossing seams at a cross-process E-Stop latch file.

    `revl run --estop-latch PATH` calls this; `REVL_ESTOP_LATCH` is the
    ambient equivalent for a host this flag does not reach (`revl mcp serve`,
    an embedder). Passing None disarms the explicit path and falls back to the
    environment."""
    global _ESTOP_LATCH
    _ESTOP_LATCH = path


def estop_latch_path() -> Optional[str]:
    """The latch file this process watches, or None."""
    return _ESTOP_LATCH or (os.environ.get(_ESTOP_LATCH_ENV) or None)


def _latch_record() -> Optional[dict]:
    """The halt an operator wrote to the latch file, or None when the latch is
    absent. A latch that exists but does not parse still HALTS: a malformed
    emergency stop is still an emergency stop, and failing open here would be
    the one failure mode this feature exists to prevent."""
    path = estop_latch_path()
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    except (ValueError, TypeError):
        return {"reason": "operator halt (unreadable latch file)",
                "operator": "unknown"}
    if not isinstance(record, dict):
        return {"reason": "operator halt (unreadable latch file)",
                "operator": "unknown"}
    return record


def estop_engaged() -> bool:
    """Whether a halt is in force — in this process, or on the latch file an
    operator in another terminal just wrote."""
    if _ESTOP is not None:
        return True
    return _latch_record() is not None


def estop_state() -> Optional[dict]:
    """The engaged halt record, or None. The in-flight inventory lives here."""
    return None if _ESTOP is None else dict(_ESTOP)


def clear_estop() -> None:
    """Drop the halt. For test isolation and for a host that has finished
    reconciling and is starting a FRESH process-level session — never a
    resume: the halted instance stays dead (item 443, open question 3)."""
    global _ESTOP
    _ESTOP = None
    _INFLIGHT.clear()


def _estop_check(where: str) -> None:
    """The crossing seam. Refuse to dispatch anything new once the halt is
    engaged, and engage it here when the latch file says an operator did.

    Cost is one `open()` on the latch path per crossing, and nothing at all
    when no latch is armed — which is the default, so a composition that never
    arms one is byte-identical to the pre-443 runtime."""
    if _ESTOP is None:
        record = _latch_record()
        if record is None:
            return
        estop(record.get("reason") or "operator halt",
              operator=record.get("operator") or "unknown", _from_latch=True)
    raise EstopHalted(where)


class _InFlight:
    """Marks one dispatched, unconfirmed crossing for the duration of the call.

    If the halt lands inside the `with`, this descriptor becomes the halt's
    single `estop-ambiguous` record: the call MAY have landed and the runtime
    says so rather than guessing (item 440's ambiguous tier, item 309's spent
    at-most-once attempt). It never becomes a stranded record — stranding
    means "never attempted", and this one was."""

    __slots__ = ("descriptor",)

    def __init__(self, **descriptor: Any) -> None:
        self.descriptor = descriptor

    def __enter__(self) -> "_InFlight":
        _INFLIGHT.append(self.descriptor)
        return self

    def __exit__(self, *_exc: Any) -> None:
        try:
            _INFLIGHT.remove(self.descriptor)
        except ValueError:  # pragma: no cover — a halt already drained it
            pass


def _estop_record(kind: str, *, component: Optional[str], method: Optional[str],
                  seq: Any, reason: str, entry_kind: str,
                  referent: Optional[str] = None) -> dict:
    """One E-Stop residue fact, in the merged residue schema (docs/design/
    teardown-contract.md). `estop-stranded` is `not-attempted` with a null
    `attempted.call`; `estop-ambiguous` is attempted with outcome `unknown`."""
    ambiguous = kind == _ESTOP_AMBIGUOUS
    return {
        "kind": kind,
        "state": "unresolved",
        "component": component,
        "method": method,
        "seq": seq,
        "entry": entry_kind,
        "attemptedFlag": ambiguous,
        "attempted": {"call": method, "phase": 0} if ambiguous else None,
        "outcome": "unknown" if ambiguous else "not-attempted",
        "referent": referent,
        "error": {
            "type": "estop",
            "message": (f"operator halt: {reason} — this crossing was in "
                        "flight when the latch was read, so it MAY have landed")
            if ambiguous else
            (f"operator halt: {reason} — registered and never unwound, "
             "the obligation is still owed"),
        },
        "hint": ("re-issue only if the crossing declared an idempotency key "
                 "(item 309); otherwise finish it by hand — a second attempt "
                 "cannot be proven safe")
        if ambiguous else
        "replayed by `revl recover --wal <file>` from its WAL descriptor",
    }


def estop(reason: str = "operator halt", *, operator: Optional[str] = None,
          _from_latch: bool = False) -> dict:
    """Engage the E-Stop. Idempotent: a second call returns the first halt.

    What this DOES: flips the process-global latch (so every crossing seam
    refuses from the next instruction onward), marks every live frame halted
    (so every disposer the unwind reaches is stranded, not replayed), snapshots
    the crossings that were already dispatched as `estop-ambiguous`, and
    enumerates every entry it can name as `estop-stranded`.

    What this does NOT do, and must not: run an inverse, run a compensation,
    flush a deferred emission, write a discharge record, or tear anything down.
    Its cost is one latch flip plus the walk of the live frames — never the
    cost of a teardown, which is the entire reason the verb exists."""
    global _ESTOP
    if _ESTOP is not None:
        return dict(_ESTOP)
    if not _from_latch and not (isinstance(operator, str) and operator.strip()):
        raise EstopRefused(
            "an E-Stop is an operator authority: it needs an operator token "
            "(item 55's `estop` verb). A composition may not halt itself — "
            "that is the whole point (item 443)")
    now = time.time()
    reason = reason or "operator halt"
    # Snapshot BEFORE marking anything: these calls are already out.
    ambiguous = [
        _estop_record(_ESTOP_AMBIGUOUS, component=d.get("component"),
                      method=d.get("method"), seq=d.get("seq"), reason=reason,
                      entry_kind=d.get("entry") or "crossing",
                      referent=d.get("referent"))
        for d in list(_INFLIGHT)]
    stranded: list = []
    activations: list = []
    for frame in list(_LIVE_FRAMES):
        if getattr(frame, "_halted", False):
            continue
        frame._halted = True
        records = frame._strand_registered(reason)
        stranded.extend(records)
        activations.append({"component": frame.name, "stranded": len(records)})
    _ESTOP = {
        "halted": True,
        "verdict": "halted",
        "reason": reason,
        "operator": operator or "unknown",
        "at": now,
        "activations": activations,
        "inFlight": ambiguous,
        "stranded": stranded,
        # item 443, open question 3: the instance is DEAD. The body was cut
        # mid-step and the runtime cannot know whether the in-flight crossing
        # landed, so re-entering it would be exactly the "pretend it did not
        # happen" the honest semantics forbids.
        "resumable": False,
        "reconcile": "revl recover --wal <file>",
    }
    _record(f"estop {reason}")
    return dict(_ESTOP)


def estop_from_latch() -> Optional[dict]:
    """Engage the halt IF an operator has armed the latch; return the halt
    record, or None when no latch is armed.

    `_estop_check` engages the latch LAZILY, at the next crossing, which is
    exactly right for a busy process: it costs nothing until something is
    about to cross. But a process parked on its stop event crosses nothing, so
    it would notice the emergency stop only when work next arrived — which is
    the moment it is too late. A watcher (`revl._process_runner`) calls this on
    a timer so an idle process halts on the BUTTON rather than on its next
    request. It is the same halt either way: idempotent, and still refusing to
    run without the operator authority the latch carries."""
    if _ESTOP is not None:
        return dict(_ESTOP)
    record = _latch_record()
    if record is None:
        return None
    return estop(record.get("reason") or "operator halt",
                 operator=record.get("operator") or "unknown", _from_latch=True)


def estop_residue() -> list:
    """Every E-Stop residue record accumulated so far: the entries named at the
    halt, plus the ones the unwind stranded afterwards at `Frame._guard`.

    The two halves are both real. A witnessed mutation, a compensation and an
    acquired resource are NAMEABLE from the frame at halt time; an emitted
    bracket disposer is a bare `lambda: <undo>` living in the cordis disposable
    list, reachable only as the unwind hands it to the frame's guard. So the
    inventory is built at the halt and COMPLETED as the process unwinds, and
    this is the merged view."""
    if _ESTOP is None:
        return []
    out = list(_ESTOP["inFlight"]) + list(_ESTOP["stranded"])
    for frame in list(_LIVE_FRAMES):
        out.extend(getattr(frame, "estop_residue", []))
    return out


class Frame:
    """One component activation's effect accumulator.

    The emitted ``apply`` creates a fresh ``Frame`` per activation and
    installs the component body as a single ``ctx.effect`` generator.  The
    generator's final ``yield frame.drain`` places the drain at the *top* of
    the runtime's LIFO disposer stack, so inverses accumulated after
    activation (provide-method ``effect`` steps, adopted below) are undone
    first, newest first, before the activation-time inverses run — exact
    component-level LIFO on a runtime that is only per-effect LIFO.

    Every inverse still lives in a genuine ``ctx.effect``; disposal is the
    runtime's single-flight ``FiberEffect``, so drain and the fiber's own
    unload pass can both reach a wrapper without ever double-freeing it.

    Empirically the fiber's unload already starts disposals newest-first
    (``DisposableList.clear()`` reverses), so with synchronous undos the
    drain usually finds the adopted effects disposed and no-ops.  That
    ordering is an implementation detail the cordis README explicitly
    disclaims ("LIFO ordering is preserved only within a single effect's
    yielded disposers"); the drain turns R1 from a property of that detail
    into a property of the documented per-effect contract.  See REPORT.md.
    """

    def __init__(self, ctx: Any, name: str) -> None:
        self.ctx = ctx
        self.name = name
        self._adopted: list = []
        # item 243 (docs/design/243-witnessed-externs.md): the transactional
        # entries this activation registered, in registration order. A witnessed
        # effect is NOT a bracket — its inverse replays on ABORT ONLY and is
        # DISCHARGED (skipped, witness GC'd) on a clean successful unload, where
        # the mutation itself is the deliverable and must persist. `_committed`
        # is the abort-vs-commit discriminator: it flips True the moment `drain`
        # runs, and `drain` runs iff the body reached its final `yield` (a clean
        # unload). A mid-activation abort never yields `drain`, so cordis unwinds
        # the already-collected disposers with `_committed` still False and the
        # transactional inverse replays exactly like a bracket. Kept for
        # introspection (fault/residue proofs); the disposer reads `_committed`.
        self._transactional: list = []
        self._committed: bool = False
        # item 318 (docs/design/243-witnessed-externs.md): the transactional
        # entries a PROVIDE-METHOD registered (a per-tool-call witnessed fs
        # mutation), as opposed to the activation body's own. A method body has
        # no body generator to `yield` a disposer into, and adopting it as a
        # sibling `ctx.effect` is UNSOUND: cordis disposes an adopted effect
        # BEFORE the body effect's final `yield _revl_frame.drain` runs, so the
        # disposer would observe `_committed` still False on a CLEAN unload and
        # wrongly replay (revert) the deliverable. So a method-registered
        # transactional entry is NOT a cordis disposer; it is parked here and
        # disposed by `drain` itself, once `_committed`/`_aborting` are settled,
        # so it observes the correct commit-vs-abort bit by construction. Each
        # entry is also appended to `_transactional` above so the WAL discharge
        # record (`drain`) and fault/residue introspection cover it uniformly.
        self._deferred_transactional: list = []
        # item 318: the abort discriminator for a component that already
        # activated cleanly. `_committed` (flipped by `drain`) answers "did the
        # ACTIVATION body complete"; a per-tool-call mutation runs AFTER
        # activation, so on any later teardown `drain` runs and would always
        # commit it. A session-level reject (item 245's explicit commit/abort
        # UX drives this) sets `_aborting` BEFORE teardown; `drain` then leaves
        # `_committed` False, so both the activation-body transactional entries
        # (cordis disposers reading `_committed`) and the method-registered
        # deferred entries replay and the mutations revert, residue-free.
        self._aborting: bool = False
        # item 247 (docs/design/teardown-contract.md): the compensation
        # entries this activation registered, in registration order — kept
        # for introspection, same role as `_transactional` above. The actual
        # Phase-2 queue is `_pending_compensations`, populated as `_Compensation`
        # disposers are encountered during an abort's Phase-1 unwind (see
        # `_Compensation.__call__`) and drained by `_drain_phase2` after
        # `install`'s `ctx.effect(...)` call re-raises the body's failure.
        self._compensations: list = []
        # item 247 (method-body compensate remainder) (docs/design/teardown-contract.md): the compensation entries
        # a PROVIDE-METHOD registered (`emit ... compensate ...` in a method
        # body), the compensation analog of `_deferred_transactional` above. Like
        # a method-registered transactional inverse, a method-body compensation
        # has NO body generator to yield its `_Compensation` disposer into, and
        # adopting it as a sibling `ctx.effect` is UNSOUND: cordis disposes an
        # adopted effect BEFORE the body's `drain`, so the bare disposer would
        # run on a CLEAN unload — firing the offset after a successful commit and
        # destroying the deliverable the emission was (the item 247 bug this
        # closes, left in place for the method-body site). So the entry is parked
        # here and disposed by `drain` once `_committed`/`_aborting` is settled:
        # DISCHARGED on commit, ENQUEUED for Phase 2 on abort (drained after every
        # proof inverse, exactly as the activation-body compensation is). Each
        # entry also joins `_compensations` above so the WAL discharge record and
        # residue introspection cover it uniformly.
        self._deferred_compensations: list = []
        self._pending_compensations: list = []
        # Phase-2 residue (teardown-contract.md's `compensation-residue`):
        # one record per compensation that raised or was skipped past the
        # budget. Best-effort introspection for this activation; the merged
        # G8 audit surface (246) is a separate, later consumer.
        self.compensation_residue: list = []
        # the Phase-2 bound, read ONCE here at activation (teardown-
        # contract.md's config surface: "read once at activation" so a
        # change mid-run cannot move an already-aborting activation's
        # deadline). `_compensation_budget_s` is `None` for "no bound".
        # `_compensation_per_call_ms` is read for config-surface parity
        # across tiers but UNUSED here: python has no in-call preemption of
        # a synchronous host call (teardown-contract.md's per-tier table),
        # so only the between-compensation check (`_compensation_budget_s`)
        # is enforceable on this tier.
        self._compensation_budget_s = _read_bound_seconds(
            _COMPENSATION_BUDGET_ENV, _COMPENSATION_BUDGET_DEFAULT_MS)
        self._compensation_per_call_ms = os.environ.get(_COMPENSATION_PER_CALL_ENV)
        # item 443: the operator E-Stop. `_halted` is the THIRD verdict
        # (`RevL.Semantics.Verdict.halted`), and unlike `_committed`/`_aborting`
        # it does not select which entries replay — it selects that NONE do.
        # Every disposer the unwind reaches while this is set is STRANDED:
        # skipped without running and WITHOUT discharging, so the inverse
        # descriptor and the witness survive for `revl recover` to read back.
        self._halted: bool = False
        # the E-Stop records this frame's unwind produced AFTER the halt, i.e.
        # the disposers that were not nameable at halt time (an emitted bracket
        # is a bare `lambda: <undo>` in the cordis disposable list). The
        # halt-time half lives on the halt record itself; `runtime.estop_residue()`
        # merges the two.
        self.estop_residue: list = []
        _LIVE_FRAMES.add(self)
        # stateful host resources this activation acquires (`Map.new()`, …), in
        # acquisition order — the instance's migratable state for a hot-swap
        # (see the state-migration section above). Populated by
        # `_register_resource` while `_body` runs, under `install`'s hook.
        self._resources: list = []
        # item 245: the session commit-state owner (docs/design/245-session-commit.md),
        # or None. When set (a driver registered one via `set_session_owner`)
        # this frame joins the owner's live-frame registry — the commit verb's
        # gate target — and its transactional/compensation discharge defers to
        # the session verdict instead of the activation's own clean unload. None
        # is the compatibility clause: drain discharges at unload, byte-identical
        # to the pre-245 world.
        self._owner: "Optional[SessionOwner]" = _SESSION_OWNER
        if self._owner is not None:
            self._owner.register_frame(self)
        # item 245: set by `drain`'s mid-session-withdrawal branch, so the
        # activation-body inverses that unwind after `drain` escrow themselves
        # (see `_hold_for_session`). Never set by a mid-activation failure.
        self._holding = False
        try:
            _FRAME_BY_CTX[ctx] = self  # so a SpawnHandle can find this frame
        except TypeError:  # pragma: no cover — non-weakrefable ctx
            pass
        # item 334 (instance migration under recording): the emitted body is
        # handed a `_RecordingContext` wrapper when the WAL is on, so the frame
        # is keyed above under the WRAPPER — but a `SpawnHandle` holds the RAW
        # fiber context (`runtime.spawn`: `SpawnHandle(fiber, ...)`) and looks the
        # frame up by it (`_frame`/`_frame_for_ctx`). Without also keying under
        # the underlying raw context, `capture_state` finds no frame under a
        # recording gate and silently reports an empty resource vector, so a
        # generational hot-swap through `Gate.propose` (which records under a
        # policy) drops the live instance's state instead of migrating it. Key
        # the frame under the underlying context too; a bare (un-recorded)
        # activation has no `_revl_ctx` and is untouched.
        underlying = getattr(ctx, "_revl_ctx", None)
        if underlying is not None and underlying is not ctx:
            try:
                _FRAME_BY_CTX[underlying] = self
            except TypeError:  # pragma: no cover — non-weakrefable ctx
                pass
        # An emitted `apply` resolves its config immediately before building
        # the Frame, so this is where the resolution finally learns which
        # component it belongs to (ConfigSchema itself is name-less in
        # emitted output, and emitted output must stay byte-identical).
        self.config = _flush_config_trace(name)

    def install(self, body: Callable) -> Any:
        """Install the component body (a generator function) as one effect.

        The body is wrapped so this frame is the *current activation* while its
        steps run — that is how a `Map.new()` in the body attributes itself to
        this instance's migratable state. The wrapper re-yields every value
        untouched (including `frame.drain` and any probe-tagged disposer), so
        the LIFO/R1 contract and fault-probe instrumentation are unchanged; it
        only brackets each step with a push/pop of the activation stack.

        item 247: this is also the Phase-2 POST-UNWIND HOOK (teardown-
        contract.md's "the phase split ... by having the compensation disposer
        enqueue itself when invoked during an abort unwind and draining the
        queue in a post-unwind hook"). A synchronous mid-body failure runs the
        WHOLE Phase-1 pass — every bracket/transactional inverse AND every
        `_Compensation.__call__` Phase-1 encounter (which only enqueues, never
        invokes) — inside cordis's own `_fail_setup`, before it re-raises out
        of `self.ctx.effect(...)`. So catching here is exactly "after Phase 1
        has fully completed": `_drain_phase2` now runs every enqueued
        compensation, best-effort, before the original failure propagates.
        Known limitation, honestly not hidden: an ASYNC body's mid-activation
        failure unwinds through cordis's asynchronous single-flight dispose
        path instead of this synchronous one, so this hook does not fire for
        it and any compensations it registered stay enqueued, undrained — py
        preemption is "none" (teardown-contract.md's per-tier table); the
        sync activation path this hook covers is the one every existing
        witnessed/compensate test and the exit-test proof drive."""
        probe = _fault_probe
        if probe is not None and probe.component == self.name:
            body = probe.instrument(body, self)
        try:
            return self.ctx.effect(self._tracked(body), f"{self.name}/body")
        except BaseException:
            self._drain_phase2()
            raise

    def _drain_phase2(self) -> None:
        """Phase 2 of the abort algorithm (teardown-contract.md): run every
        compensation `_Compensation.__call__` enqueued during Phase 1, in the
        order they were enqueued — which is already Phase-2 LIFO (newest
        compensation first), because Phase 1 itself visited the stack newest-
        first and every compensation appended itself to
        `_pending_compensations` in that same encounter order.

        Bounded and best-effort: the deadline (`self._compensation_budget_s`,
        read once at activation from `REVL_COMPENSATION_BUDGET_MS`, `None`
        for no bound) is checked BEFORE each compensation — never mid-call,
        python cannot safely preempt a synchronous host call (teardown-
        contract.md's per-tier table) — and a failure never stops the
        remaining ones: continue-and-record, same rule as Phase 1's bracket/
        transactional failures, `compensation-residue` severity. Every skip
        or failure is recorded, never silently dropped."""
        pending, self._pending_compensations = self._pending_compensations, []
        if not pending:
            return
        # item 443: an E-Stop runs no compensation either. Phase 2 is the
        # best-effort offset pass; "best effort" under an operator halt is zero
        # effort, because every compensation is itself a boundary CROSSING and
        # the halt's first guarantee is that no new crossing is dispatched.
        # Each owed offset is stranded instead, so the report names it.
        if self._halted:
            reason = (_ESTOP or {}).get("reason", "operator halt")
            for entry in pending:
                if getattr(entry, "_estop_stranded", False):
                    continue
                entry._estop_stranded = True
                self.estop_residue.append(_estop_record(
                    _ESTOP_STRANDED,
                    component=getattr(entry, "component", self.name),
                    method=getattr(entry, "method", None),
                    seq=getattr(entry, "seq", None), reason=reason,
                    entry_kind="compensation"))
            return
        budget = self._compensation_budget_s
        deadline = None if budget is None else time.monotonic() + budget
        for entry in pending:
            if deadline is not None and time.monotonic() >= deadline:
                self.compensation_residue.append(_residue_record(
                    entry, outcome="not-attempted", attempted_flag=False,
                    attempted=None,
                    error={"type": "deadline-expired",
                           "message": "phase-2 budget exhausted before "
                                      "this compensation started"}))
                continue
            entry._run_phase2()
            if entry.failed:
                self.compensation_residue.append(_residue_record(
                    entry, outcome="failed", attempted_flag=True,
                    attempted={"phase": 2},
                    error={"type": type(entry.error).__name__,
                           "message": str(entry.error)}))

    def _record_phase1_residue(self, disposer: Any, error: BaseException) -> None:
        """Record ONE Phase-1 inverse that raised, into the merged residue
        schema (`compensation_residue`, the per-frame half of the audit
        surface `SessionOwner.collect_compensation_residue` merges).

        The severity is the entry's (`_phase1_kind`): contract-grade
        `bracket-fault` for a bracket inverse that claimed G5 infallibility
        and lied, `restore-residue` for the anticipated witnessed-restore
        failure. Same catch, same shape, different tag — teardown-contract.md,
        "Phase-1 failure: continue-and-record, uniform, two severities"."""
        self.compensation_residue.append(_residue_record(
            getattr(disposer, "_revl_entry", disposer),
            kind=_phase1_kind(disposer),
            component=self.name,
            method=_inverse_label(disposer),
            outcome="failed", attempted_flag=True,
            attempted={"phase": 1},
            error={"type": type(error).__name__, "message": str(error)}))

    def _strand_registered(self, reason: str) -> list:
        """Enumerate every entry this frame can NAME at halt time, as
        `estop-stranded` residue (item 443).

        Nameable means: the witnessed (`transactional`) entries, the
        `compensation` entries, and the stateful host resources this activation
        acquired. An emitted BRACKET inverse is a bare `lambda: <undo>` living
        in the cordis disposable list with no entry object behind it, so it is
        not reachable from here — it is stranded instead at `_guard`, as the
        unwind hands it over. Both halves carry the same `kind`, and
        `runtime.estop_residue()` is the merged view.

        Each entry is flagged so the later `_guard` pass does not record it
        twice."""
        records: list = []
        seen: list = []
        for entry, entry_kind in ([(e, "transactional") for e in self._transactional]
                                  + [(e, "compensation") for e in self._compensations]):
            if getattr(entry, "_estop_stranded", False):
                continue
            entry._estop_stranded = True
            seen.append(entry)
            records.append(_estop_record(
                _ESTOP_STRANDED, component=getattr(entry, "component", self.name),
                method=getattr(entry, "method", None),
                seq=getattr(entry, "seq", None), reason=reason,
                entry_kind=entry_kind))
        for resource in self._resources:
            records.append(_estop_record(
                _ESTOP_STRANDED, component=self.name,
                method=getattr(resource, "_tag", lambda: type(resource).__name__)(),
                seq=None, reason=reason, entry_kind="bracket",
                referent=repr(resource)))
        return records

    def _record_estop_stranded(self, disposer: Any) -> None:
        """Record ONE disposer the unwind reached while the halt was engaged.

        This is the `_guard` half of the inventory: the inverse is NOT run and
        NOT discharged, and the record says which. Deduplicated against the
        halt-time pass by the entry's own flag, so an entry named at the halt
        is not counted twice when cordis later hands its disposer over."""
        entry = getattr(disposer, "_revl_entry", disposer)
        if getattr(entry, "_estop_stranded", False):
            return
        try:
            entry._estop_stranded = True
        except AttributeError:  # pragma: no cover — a builtin/bound method
            pass
        halt = _ESTOP or {}
        self.estop_residue.append(_estop_record(
            _ESTOP_STRANDED, component=self.name,
            method=_inverse_label(disposer), seq=getattr(entry, "seq", None),
            reason=halt.get("reason", "operator halt"),
            entry_kind=("transactional" if isinstance(entry, _Transactional)
                        else "compensation" if isinstance(entry, _Compensation)
                        else "bracket")))

    def _guard(self, value: Any) -> Any:
        """Wrap one disposer the activation body yielded so a raise out of it
        is CAUGHT, RECORDED and does not abort the rest of Phase 1.

        This is the contract's uniform Phase-1 rule (docs/design/teardown-
        contract.md): "A failed inverse never skips the remaining Phase-1
        inverses. Skipping strictly increases residue ... catch, record into
        the merged residue schema, continue." cordis disposes the entries of
        one effect strictly sequentially, so an uncaught raise out of ONE
        disposer starves every earlier-registered (later-disposed) entry —
        G7 (LIFO completeness) and R4 (no unreported residue) both break, and
        the fiber still lands DISPOSED, silently.

        The guard lives HERE rather than in the emitted `lambda: <undo>`
        because `_tracked` is the single chokepoint every emitted shape
        already passes through — plain brackets, result-guarded CAS inverses,
        witnessed `_Transactional` entries, `_Compensation` entries, and any
        future one — so a later emitter change cannot forget it.

        ONLY an author's own inverse is wrapped — a plain function (the
        emitted `lambda: <undo>`, a timer's cancel closure, a method-body
        `acquire` inverse, the fault probe's tag around any of them) or a
        `_Transactional`/`_Compensation` entry. Everything else passes through
        BY IDENTITY, which is load-bearing, not cosmetic:

        * `yield _revl_ctx.provide(...)` yields a cordis `FiberEffect`, and
          cordis's own unwind branches on `isinstance(d, FiberEffect)` to join
          an in-flight cleanup (`d._join`) instead of re-entering it. Wrapping
          it in a plain function hides that type and silently changes the
          withdrawal ordering (R3: a dependent must fully deactivate before
          its provider's own inverses run).
        * the frame's sentinels (`begin`/`drain`) are runtime code, not an
          author's inverse; they guard their own internal per-entry loops
          (see `drain`).
        * a non-callable yield (an A1 iteration boundary) is not a disposer.
        """
        if getattr(value, "__self__", None) is self:
            return value
        if not (isinstance(value, types.FunctionType)
                or isinstance(value, (_Transactional, _Compensation))):
            return value
        frame = self

        def _guarded(_disposer=value):
            # item 443: under an E-Stop the inverse does NOT run. This is the
            # runtime half of `RevL.G7.estop_replays_nothing` — the halt's
            # replay set is empty by construction, at the one chokepoint every
            # emitted disposer shape already passes through — and the entry is
            # recorded as owed rather than dropped.
            if frame._halted:
                frame._record_estop_stranded(_disposer)
                return None
            with _InFlight(component=frame.name,
                           method=_inverse_label(_disposer),
                           seq=getattr(getattr(_disposer, "_revl_entry", _disposer),
                                       "seq", None),
                           entry="inverse"):
                try:
                    return _disposer()
                except BaseException as error:  # noqa: BLE001 — recorded, never re-raised
                    frame._record_phase1_residue(_disposer, error)
                    return None

        _guarded._revl_entry = getattr(value, "_revl_entry", value)
        return _guarded

    def _tracked(self, body: Callable) -> Callable:
        """Wrap `body` so `self` is the top activation frame while its code
        runs between yields. Preserves sync-vs-async-generator identity, which
        cordis's effect dispatch switches on.

        Also the Phase-1 continue-and-record seam: every disposer the body
        yields is routed through `_guard` on its way to cordis (see there)."""
        frame = self

        if inspect.isasyncgenfunction(body):
            async def _async_tracked():
                iterator = body().__aiter__()
                while True:
                    _ACTIVATING.append(frame)
                    try:
                        value = await iterator.__anext__()
                    except StopAsyncIteration:
                        break
                    finally:
                        _ACTIVATING.pop()
                    yield frame._guard(value)
            return _async_tracked

        def _tracked_gen():
            iterator = iter(body())
            while True:
                _ACTIVATING.append(frame)
                try:
                    value = next(iterator)
                except StopIteration:
                    break
                finally:
                    _ACTIVATING.pop()
                yield frame._guard(value)
        return _tracked_gen

    def adopt(self, effect: Any) -> Any:
        """Join an effect created while ACTIVE to this component's accumulator."""
        self._adopted.append(effect)
        return effect

    def acquire(self, label: str, get: Callable[[], Any], undo: Callable[[Any], Any]) -> Any:
        """A ``let-effect`` step inside a provide-method body: run the
        acquisition through the effect protocol, adopt it, return the value."""
        _estop_check(f"{self.name}.{label}")   # item 443
        holder: list = []

        frame = self

        def _setup():
            holder.append(get())

            def _inverse():
                return undo(holder[0])

            # the residue record names the effect this inverse belongs to;
            # the closure itself references only free variables, so there is
            # no callee name in its code object to recover.
            _inverse._revl_method = label
            # same Phase-1 continue-and-record guard the activation body's
            # yields get (`_guard`): a method-registered bracket inverse that
            # raises must not break `_dispose_adopted`'s sequential unwind and
            # starve every earlier-adopted effect.
            yield frame._guard(_inverse)

        self.adopt(self.ctx.effect(_setup, label))
        return holder[0]

    def _wal(self) -> Optional[Any]:
        """The active `WriteAheadLog` reachable through this frame's `ctx`, or
        `None` (bridge slice: wires the Slice-2a runtime to the WAL/recover
        foundation that `backends/python/replay.py`/`src/revl/recovery.py`
        already implement).

        Under `revl run --record`/`--wal`, `replay.Recorder._wrap_apply` calls
        the emitted `apply` with a `_RecordingContext` proxy as `ctx`, carrying
        a `_revl_timeline` attribute set through `object.__setattr__` — a
        genuine instance attribute, not one routed through the proxy's
        `__getattr__` — so a plain `getattr` finds it directly, with no
        threading needed through `emit.py`'s `Frame(_revl_ctx, name)` call
        site. A plain run's `ctx` is the real cordis context with no such
        attribute, so this is a no-op there — every WAL write below becomes a
        no-op too, exactly as an un-recorded run behaved before this slice."""
        timeline = getattr(self.ctx, "_revl_timeline", None)
        if timeline is None:
            return None
        return getattr(timeline, "_wal", None)

    def transactional(self, undo: Callable[[Any], Any], witness: Any, *,
                      undo_idempotent: bool = False,
                      register: Optional[str] = None,
                      idempotency: Optional[str] = None) -> "_Transactional":
        """Register a witnessed effect's declared inverse as a TRANSACTIONAL
        entry, carrying its `witness` (item 243). Returns the disposer the
        emitted body yields into the accumulator, so it sits in the same LIFO
        disposer stack as every bracket inverse and unwinds in exact reverse
        order alongside them.

        The distinction from `acquire` lives entirely in the returned disposer
        (:class:`_Transactional`): on teardown it replays `undo(witness)` iff
        this activation did NOT commit (an abort — `drain` never ran), and on a
        clean committed unload it DISCHARGES — the inverse is skipped and the
        witness dropped, because the mutation is the deliverable and must
        persist. Registration itself is unconditional here; the emitted call
        site yields this only on the `Ok` branch (Ok-conditional), so a failed
        mutation that touched nothing schedules no rollback.

        item 443: registration IS the crossing here (the emitted call site
        yields this on the `Ok` branch, i.e. after the mutation landed), so an
        engaged E-Stop refuses before a new one is accepted.

        Bridge slice: when a WriteAheadLog is attached, this ALSO writes the
        WAL discharge-descriptor for the entry at registration time (docs/
        design/teardown-contract.md, "WAL descriptor") — durably ahead of
        whether this activation ever commits, so a crash before commit still
        lets `revl recover` reconstruct and replay the inverse. `entry.seq`
        carries the assigned seq so `drain` can name it in the discharge
        record on a clean commit; it is `None` when no WAL is active."""
        _estop_check(f"{self.name}.{_named_call_method(undo)}")   # item 443
        entry = _Transactional(self, undo, witness,
                               undo_idempotent=undo_idempotent)
        self._transactional.append(entry)
        wal = self._wal()
        if wal is not None:
            record = wal.record_discharge_descriptor(
                "transactional",
                receiver=self.name,
                method=_named_call_method(undo),
                args=[witness],
                origin={"phase": "activation", "key": self.name},
                witness=witness,
                # item 309: carry the register into the WAL so a fresh-process
                # recover reads free-vs-fenced replay from the descriptor.
                undo_idempotent=undo_idempotent,
                register=register,
                idempotency=idempotency,
            )
            entry.seq = record["seq"]
        return entry

    def transactional_method(self, undo: Callable[[Any], Any], witness: Any) -> "_Transactional":
        """Register a PROVIDE-METHOD witnessed effect's declared inverse as a
        transactional entry on THIS component's activation frame (item 318,
        docs/design/243-witnessed-externs.md). This is the per-tool-call H1
        seam: an agent's fs mutation fires from a provide-method (per request),
        and its inverse must outlive the method call — the method returns, but
        the mutation's rollback must survive until the component/session
        commits or aborts. The enclosing component's activation frame is that
        accumulator: it is component-long, and its commit/abort already drives
        `_Transactional` discharge/replay (Slice 2a).

        Unlike `transactional` (yielded by the activation body's generator into
        cordis's LIFO disposer stack), a method body has no generator to yield
        into. Adopting the entry as a sibling `ctx.effect` is unsound — cordis
        disposes an adopted effect BEFORE the body's `drain`, so on a clean
        unload the disposer would see `_committed` still False and wrongly
        revert the deliverable. So the entry is parked in
        `_deferred_transactional` and disposed by `drain` (commit) or the
        aborting `drain` pass (abort), where `_committed` is already settled.
        The entry still joins `_transactional` so the WAL discharge record and
        residue introspection cover it exactly like an activation-body one.

        Registration is unconditional here; the emitted call site calls this
        only on the `Ok` branch (Ok-conditional), so a failed mutation that
        touched nothing schedules no rollback. Bridge slice: the WAL
        discharge-descriptor is written at registration, durably ahead of the
        commit-vs-abort decision, so a crash before it lets `revl recover`
        reconstruct and replay the inverse (item 243 rule 4)."""
        _estop_check(f"{self.name}.{_named_call_method(undo)}")   # item 443
        entry = _Transactional(self, undo, witness)
        self._transactional.append(entry)
        self._deferred_transactional.append(entry)
        wal = self._wal()
        if wal is not None:
            record = wal.record_discharge_descriptor(
                "transactional",
                receiver=self.name,
                method=_named_call_method(undo),
                args=[witness],
                origin={"phase": "call", "key": self.name},
                witness=witness,
            )
            entry.seq = record["seq"]
        return entry

    def abort(self) -> None:
        """Mark this activation as ABORTING so its next teardown reverts rather
        than commits (item 318). A component that activated cleanly reaches its
        final `yield _revl_frame.drain`, so any later `fiber.dispose()` runs
        `drain` and would implicitly commit every accumulated transactional
        entry. A session-level reject of the work (item 245's explicit
        commit/abort UX is the eventual driver) calls this first: `drain` then
        leaves `_committed` False, so the activation-body transactional entries
        AND the per-tool-call deferred entries all replay their inverses and the
        mutations revert, residue-free. Idempotent."""
        self._aborting = True

    def compensation(self, fn: Callable[[], Any]) -> "_Compensation":
        """Register an `emit ... compensate ...` step's offsetting call as a
        COMPENSATION entry on the SAME per-activation LIFO disposer stack as
        every bracket and transactional entry (item 247, docs/design/
        teardown-contract.md). Returns the disposer the emitted body yields
        into the accumulator, so it unwinds in exact reverse-registration
        order alongside them.

        Distinct from both `acquire` and `transactional` (:class:`_Compensation`):
        on a clean committed unload it DISCHARGES — `fn` never runs, because the
        emission it offsets was already the deliverable. On an abort it does
        NOT run inline during Phase 1; it runs in PHASE 2, best-effort, only
        after every bracket/transactional inverse in this activation has
        completed (`Frame.install`'s post-unwind hook drives `_drain_phase2`).
        Registration is unconditional — the call site yields this right after
        the emission, unconditionally on whether the emission itself is
        checked-fallible, matching the existing `emit ... compensate ...`
        surface.

        Bridge slice: when a WriteAheadLog is attached, this ALSO writes the
        WAL discharge-descriptor for the entry at registration time — same
        mechanism as `transactional` (teardown-contract.md, "WAL descriptor"),
        `entry="compensation"` — so a crash before this activation's fate is
        decided still lets `revl recover` reconstruct and re-issue it through
        recovery.py's dedicated `apply_compensation` path (never the generic
        `apply_inverse`, whose `_REMOVE` verb match would wrongly pop the
        forward referent — teardown-contract.md's "WAL descriptor" section).
        `entry.seq` carries the assigned seq so `drain` can name it in the
        discharge record on a clean commit; it is `None` when no WAL is
        active."""
        _estop_check(f"{self.name}.{_named_call_method(fn)}")   # item 443
        entry = _Compensation(self, fn, method=_named_call_method(fn))
        self._compensations.append(entry)
        wal = self._wal()
        if wal is not None:
            record = wal.record_discharge_descriptor(
                "compensation",
                receiver=self.name,
                method=_named_call_method(fn),
                args=[],
                origin={"phase": "activation", "key": self.name},
                witness=None,
            )
            entry.seq = record["seq"]
        return entry

    def compensation_method(self, fn: Callable[[], Any]) -> "_Compensation":
        """Register a PROVIDE-METHOD `emit ... compensate ...` step's offsetting
        call as a COMPENSATION entry on THIS component's activation frame (the
        item-247 method-body compensate remainder, docs/design/teardown-contract.md).
        This is the compensation analog
        of `transactional_method` (item 318): a per-tool-call emission fires from
        a provide-method, and its offset must outlive the method call — the method
        returns, but the offset is owed only if the component/session ABORTS, and
        must never fire on a clean commit (the emission was the deliverable).

        Unlike `compensation` (yielded by the activation body's generator into
        cordis's LIFO disposer stack), a method body has no generator to yield
        into. Adopting the `_Compensation` as a sibling `ctx.effect` is UNSOUND —
        cordis disposes an adopted effect BEFORE the body's `drain`, so the
        disposer would run on a CLEAN unload and fire the offset after a
        successful commit (the exact placeholder-lowering bug this closes). So
        the entry is parked in `_deferred_compensations` and disposed by `drain`
        (commit → discharge; abort → enqueue for Phase 2), where the commit-vs-
        abort bit is already settled. The entry still joins `_compensations` so
        the WAL discharge record and residue introspection cover it exactly like
        an activation-body one.

        Registration is unconditional here, matching the activation-body
        `compensation` and the existing `emit ... compensate ...` surface: the
        call site yields this right after the emission, unconditionally. Bridge
        slice: the WAL discharge-descriptor is written at registration, durably
        ahead of the fate decision, `entry="compensation"`, `origin.phase="call"`
        (a per-tool-call crossing, as opposed to the activation-body's
        `"activation"`), so `revl recover` reconstructs and re-issues it through
        recovery's `apply_compensation` path (teardown-contract.md's "WAL
        descriptor").

        Recorder seam: under `--record`, the offset must still surface as a
        `compensation` step in the step-back timeline. The pre-fix placeholder
        lowering got that for free — it `yield`ed the bare offset lambda into a
        recorded effect generator, where `Timeline.record_yield` classified it by
        SOURCE ADJACENCY to the just-recorded emission. Routing through the frame
        bypasses that generator, so this records the step explicitly (same
        classifier) and adopts the ONCE-wrapped disposer `record_yield` returns as
        the entry's `fn` — so a step-back run and the live Phase-2 drain share one
        guard and never double-fire the offset. A plain run carries no timeline
        (its `ctx` has no `_revl_timeline`), so this is a no-op there, byte-inert.
        The discharge-descriptor's `method` is read from the ORIGINAL `fn` before
        wrapping, so the WAL names the real offsetting call, not the wrapper."""
        _estop_check(f"{self.name}.{_named_call_method(fn)}")   # item 443
        method_name = _named_call_method(fn)
        timeline = getattr(self.ctx, "_revl_timeline", None)
        if timeline is not None:
            _step, fn = timeline.record_yield(fn, f"{self.name}/compensate")
        entry = _Compensation(self, fn, method=method_name)
        self._compensations.append(entry)
        self._deferred_compensations.append(entry)
        wal = self._wal()
        if wal is not None:
            record = wal.record_discharge_descriptor(
                "compensation",
                receiver=self.name,
                method=method_name,
                args=[],
                origin={"phase": "call", "key": self.name},
                witness=None,
            )
            entry.seq = record["seq"]
        return entry

    def enqueue_deferred(self, receiver: str, method: str, args: list,
                         fire: Callable[[], Any], *,
                         register: Optional[str] = None,
                         idempotency: Optional[Any] = None) -> None:
        """Enqueue a class-(b) deferred emission onto the session's deferral
        queue instead of firing it (item 245, docs/design/245-session-commit.md,
        Decision 3). The host body does NOT run here; it runs exactly once at the
        session commit's flush, or never (on abort). `fire` is the zero-arg thunk
        the flush invokes; `receiver`/`method`/`args` are the serializable
        named-call descriptor for the WAL and the commit manifest.

        Requires a session owner: the deferral queue, the escrow and the commit
        verb that flushes or drops it all live on the owner (the py driver). A
        deferred emission reaching a runtime with no owner is the exact failure
        the five ownerless tiers refuse at emit (Decision 2's tier gate); on py
        the driver is always the owner, so this refusal is a guard against a
        misconfigured embedding, never a normal path."""
        _estop_check(f"{receiver}.{method}")   # item 443
        if self._owner is None:
            raise RuntimeError(
                "a deferred emission needs a session owner runtime (the deferral "
                "queue and the commit verb), which this composition has none — "
                "load it under a driver that registers one (item 245)")
        self._owner.enqueue(receiver, method, list(args), fire,
                            register=register, idempotency=idempotency)

    def request_approval(self, capability: str, fields: dict) -> dict:
        """`let a = await approval[C] { fields }` at runtime (item 246). Resolve a
        standing `Approval[C]` for THIS component from the owner ledger and return
        a handle threaded to the crossing by `with`. Fails closed when the policy
        is enforced and no valid approval covers it (silence never approves). When
        the policy is off, or no owner is registered, returns a passthrough handle
        so a `with a` crossing fires normally (byte-identity)."""
        _estop_check(f"approval[{capability}]")   # item 443
        owner = self._owner
        if owner is None or not owner.approval_enforced:
            return {"capability": capability, "requestId": None,
                    "passthrough": True}
        entry = owner.find_approval(capability, self.name)
        if entry is None:
            raise ApprovalCrossingRefused(
                capability,
                f"no granted, unexpired, unconsumed Approval[{capability}] for "
                f"component `{self.name}` in this generation and session — a "
                f"human must grant it via revl_approve (item 246, "
                f"unreachable-without)")
        return {"capability": capability, "requestId": entry.get("requestId"),
                "passthrough": False}

    def acquire_lease(self, capability: str, ttl_ms: Any, uses: Any) -> Any:
        """`let l = effect lease <cap> …` at runtime (item 294 Slice 2). The
        standing grant was already minted from the operator's approved ticket by
        the load-time lease gate; this resolves the live lease grant for THIS
        component and capability cone and returns the handle whose `.revoke()`
        retires it on teardown. Fails closed when no session owner backs the lease
        (an ungated raw run) — the load gate refuses that upstream, and this is the
        defensive twin so no self-mint ever happens with no ticket behind it."""
        owner = self._owner
        acquire = getattr(owner, "lease_acquire", None) if owner is not None \
            else None
        if acquire is None:
            raise LeaseRefused(
                capability,
                "no session grant ledger backs this lease — an unenforceable "
                "lease is not a lease (item 294, honest sentence 2). A lease "
                "must be acquired under an approval policy that raised and "
                "approved its ticket")
        request_id = acquire(self.name, capability, ttl_ms, uses)
        if request_id is None:
            raise LeaseRefused(
                capability,
                "no live standing grant was minted for this lease — the "
                "operator ticket for the lease was not approved (fail-closed: "
                "a lease never self-mints)")
        return LeaseHandle(capability, request_id, owner.lease_revoke)

    def approval_crossing(self, handle: Any, capability: str,
                          fire: Callable[[], Any]) -> Any:
        """`emit <call> with a` at runtime (item 246, Decision 3). The frame
        checks the token BEFORE the host body runs and consumes it DURABLY first
        (consume-before-fire): the `approval-consumed` WAL record is flushed, then
        the body fires, then the `approval-emission` record names the same
        `requestId`. A crash between spend and fire leaves consumed-but-unfired —
        an owed action needing a FRESH approval, fail-closed. When the policy is
        off (passthrough handle / no owner), the body fires unchanged."""
        # item 443: this is THE class-(c) crossing seam, and a class-(c) storm
        # is the scenario the E-Stop exists for. Refused before the token is
        # spent, so a halt never leaves an approval consumed-but-unfired.
        _estop_check(f"{self.name}.{capability}")
        owner = self._owner
        if owner is None or not owner.approval_enforced \
                or (isinstance(handle, dict) and handle.get("passthrough")):
            return fire()
        # re-resolve at the crossing: the token must still be valid HERE (expiry
        # is checked at the crossing, not at mint — invariant 3), by requestId
        # when the handle names one, else by capability+component.
        rid = handle.get("requestId") if isinstance(handle, dict) else None
        entry = None
        if rid is not None:
            entry = next((e for e in owner.approval_ledger
                          if e.get("requestId") == rid and not e.get("consumed")),
                         None)
            if entry is not None and owner.find_approval(
                    capability, self.name) is None:
                entry = None  # named token is stale/expired/wrong-generation
        if entry is None:
            entry = owner.find_approval(capability, self.name)
        if entry is None:
            raise ApprovalCrossingRefused(
                capability,
                f"the Approval[{capability}] threaded here is absent, consumed, "
                f"expired, or bound to another component/generation/session "
                f"(item 246, invariants 1/3/4/5)")
        owner.consume_approval(entry)          # durable spend BEFORE the fire
        # item 443: between the durable spend and the completion record is
        # exactly the window an E-Stop lands in, and the crossing is then
        # AMBIGUOUS — it may or may not have landed. Marking it in flight is
        # what lets the halt say so instead of guessing (item 440's tier).
        with _InFlight(component=self.name, method=capability,
                       seq=entry.get("requestId"), entry="crossing"):
            result = fire()                     # the host body crosses now
        wal = self._wal()
        if wal is not None:
            wal.record_approval_emission(entry.get("requestId"), capability,
                                         self.name)
        return result

    def begin(self) -> None:
        """Yielded FIRST by the emitted body -> disposed LAST (cordis LIFO), so
        it sits at the BOTTOM of this activation's unwind stack.

        item 247 second-pass (F1, data loss): this is the Phase-2 POST-UNWIND
        hook for an activation whose body ran to completion and is aborted
        LATER — a session-level reject (`Frame.abort()` + unload, or
        `Session.abort()`), not a mid-body raise. In that case `drain` (yielded
        last, disposed FIRST) runs at the TOP of the stack, and the activation-
        body `_Compensation` disposers — yielded BEFORE `drain`, so disposed
        AFTER it — enqueue themselves onto `_pending_compensations` only once
        cordis reaches them, strictly BELOW `drain`. Draining Phase 2 in
        `drain`'s tail therefore runs before those compensations exist and
        silently loses the offset. `begin`, at the bottom of the stack, is the
        one disposer guaranteed to run AFTER every earlier entry has been
        disposed — Phase 1 complete, `_pending_compensations` fully populated —
        so it is the correct place to drain Phase 2 (mirrors the go
        `runCompensationPhase` / ts `begin` post-unwind hook).

        A no-op on a clean commit (`_committed` — nothing was enqueued, the
        offsets discharged), and idempotent with `install`'s except hook: both
        call `_drain_phase2`, which single-flights `_pending_compensations` by
        swapping it to `[]`, so whichever runs first drains and the other finds
        nothing."""
        if self._committed:
            return
        self._drain_phase2()

    def drain(self) -> Any:
        """Dispose every adopted effect, newest first (yielded last by the
        emitted body, so the runtime runs it first on unload).

        item 243: reaching `drain` at teardown is the proof that the body ran to
        its final `yield` — i.e. activation completed and this unload is a clean
        one, an implicit commit. Flip `_committed` first, synchronously, before
        disposing anything: `drain` is yielded last so cordis disposes it FIRST
        (LIFO), which means every transactional inverse collected earlier runs
        AFTER this line and observes the commit. An abort never yields `drain`,
        so this never runs and the transactional inverses replay.

        Bridge slice: because `_committed` is now fixed True, every entry
        already registered in `self._transactional` will discharge (not
        replay) regardless of whether its own `_Transactional.__call__` has
        run yet — so the WAL discharge record for all of them can be, and
        must be, written HERE, before this call returns and success is
        reported to the caller (docs/design/teardown-contract.md, "Commit
        path"). That is what closes the crash window: a process that commits
        and then dies before its `activation-complete` marker still leaves a
        durable discharge record on disk, so `revl recover` skips the
        already-committed mutation instead of wrongly rolling it back.

        item 247: every registered `_Compensation` entry discharges the same
        way (its `__call__` reads the same `_committed` flag) and is owed the
        same durable discharge record — a compensation is never owed on a
        clean unload (teardown-contract.md's "Commit path"), so its seq joins
        the transactional seqs in the one discharge record written here."""
        # item 443: under an E-Stop, `drain` does NOTHING. It does not flip
        # `_committed` (the halt is a third verdict, not a commit), it writes no
        # discharge record (a discharge would DROP the very descriptors the
        # reconciliation path reads back), and it disposes no adopted effect
        # (that would be a graceful unwind, which is exactly what an emergency
        # stop is not). Every method-registered entry is stranded instead, and
        # the cordis unwind's own disposers are stranded at `_guard`. This is
        # `RevL.G7.estop_replays_nothing` and `estop_discharges_nothing`
        # together, at the commit path.
        if self._halted:
            self.estop_residue.extend(self._strand_registered(
                (_ESTOP or {}).get("reason", "operator halt")))
            self._deferred_transactional = []
            self._deferred_compensations = []
            self._pending_compensations = []
            return None
        # item 318: `_aborting` is the reject signal for an already-activated
        # component (a session-level abort of per-tool-call work). When set,
        # `drain` runs but does NOT commit: `_committed` stays False, so every
        # transactional entry — the activation body's cordis-yielded ones AND
        # the method-registered deferred ones below — replays its inverse and
        # the mutations revert. A plain unload does not set it, so this is
        # byte-identical to the previous unconditional commit for every
        # existing activation-body-only program.
        # item 245: a MID-SESSION withdrawal under a session owner whose verdict
        # is still pending (a swap/undo). This drain does NOT commit: the frame's
        # undischarged transactional/compensation entries are ESCROWED to await
        # the session verdict, and no discharge record is written. The
        # activation-body cordis disposers escrow themselves via `_hold_for_session`
        # when the fiber unwinds; the method-registered entries are escrowed here.
        # The frame leaves the live registry (it is being torn down), but its
        # entries live on in the escrow until the session commits or aborts. The
        # bracket (adopted) effects still run — releasing a handle is always right.
        if self._owner is not None and self._owner._verdict is None \
                and not self._aborting:
            self._holding = True
            self._owner.withdraw_frame(self)
            for entry in self._deferred_transactional:
                self._owner.escrow(entry)
                entry._escrowed = True
            self._deferred_transactional = []
            # item 247 (method-body compensate remainder): the method-registered compensations escrow the same way;
            # `finalize_abort` runs their Phase-2 offset (after the escrowed
            # transactional inverses), and a session commit discharges them by
            # omission — byte-for-byte the `_deferred_transactional` discipline.
            for entry in self._deferred_compensations:
                self._owner.escrow(entry)
                entry._escrowed = True
            self._deferred_compensations = []
            return self._dispose_adopted()

        if not self._aborting:
            self._committed = True
        wal = self._wal()
        # The discharge record is the COMMIT proof; it must not be written for
        # an aborting teardown, where the inverses are being replayed, not
        # committed (item 318). Under a session owner the consolidated discharge
        # record is written ONCE by the owner over (escrow + every live frame),
        # so each frame SUPPRESSES its own — otherwise the same seqs would be
        # named twice and the one-record-per-session-commit shape (Decision 1)
        # would not hold.
        if self._committed and wal is not None and self._owner is None:
            seqs = [entry.seq for entry in self._transactional if entry.seq is not None]
            seqs += [entry.seq for entry in self._compensations if entry.seq is not None]
            if seqs:
                wal.record_discharge(seqs)
        # Dispose the method-registered transactional entries HERE, now that the
        # commit-vs-abort bit is settled (item 318): on a commit each discharges
        # (mutation persists, witness GC'd); on an abort each replays (reverts).
        # They are not cordis disposers, so this is their sole disposal — no
        # double-free with the fiber's own unwind.
        #
        # item 369: replay in reverse INVOCATION order (LIFO), NOT registration
        # order. `_deferred_transactional` is appended newest-last as each
        # provide-method fires (`transactional_method`), so it must be drained
        # newest-FIRST — exactly like the activation-body path, where cordis
        # unwinds its disposer stack LIFO, and like `_dispose_adopted` below
        # (`reversed(adopted)`). On a COMMIT order is immaterial (every entry
        # no-op discharges), but on an ABORT two inverses whose paths OVERLAP
        # must undo newest-first or the guarantee 243/246 sell is violated:
        # every stdlib/fs.rvl inverse is idempotent-and-total, so a FIFO replay
        # runs the oldest inverse first, finds nothing, silently no-ops, and the
        # newer inverse then undoes into the hole — residue or DESTROYED
        # pre-session data with `noResidue: true` still reported (G7, 243 §2).
        # continue-and-record, same Phase-1 rule the cordis-yielded entries get
        # through `_guard`: a raising restore here must not skip the remaining
        # deferred entries (nor `_dispose_adopted` below).
        deferred, self._deferred_transactional = self._deferred_transactional, []
        for entry in reversed(deferred):
            try:
                entry()
            except BaseException as error:  # noqa: BLE001 — recorded, never re-raised
                self._record_phase1_residue(entry, error)
        # item 247 (method-body compensate remainder): dispose the method-registered COMPENSATION entries, now that
        # the commit-vs-abort bit is settled — the compensation analog of the
        # `_deferred_transactional` disposal above, and the method-body analog of
        # the activation-body `emit ... compensate ...` (item 247). On a COMMIT
        # each `__call__` DISCHARGES (the offset never runs — the emission was the
        # deliverable, best-effort cleanup on success would be wrong); on an ABORT
        # each ENQUEUES onto `_pending_compensations` (never fired inline during
        # Phase 1, so a raising offset can never interrupt a proof inverse).
        # Newest-first, so Phase 2 runs the newest compensation first, matching
        # the activation-body path (cordis unwinds its disposer stack LIFO).
        deferred_comp, self._deferred_compensations = self._deferred_compensations, []
        for entry in reversed(deferred_comp):
            try:
                entry()
            except BaseException as error:  # noqa: BLE001 — recorded, never re-raised
                self._record_phase1_residue(entry, error)
        # item 247 second-pass (F1): Phase 2 is NOT drained here. This `drain`
        # is disposed FIRST (yielded last), at the TOP of the unwind stack; the
        # activation-body `_Compensation` disposers are yielded BEFORE it, so
        # they are disposed AFTER it and enqueue onto `_pending_compensations`
        # only once cordis reaches them — draining here would run before they
        # exist and lose the offset (the F1 data-loss hole). The method-body
        # deferred compensations enqueued just above are in the same queue.
        # `begin` — yielded FIRST, disposed LAST, at the BOTTOM of the stack —
        # is the post-unwind hook that drains the whole queue as Phase 2,
        # strictly after every Phase-1 inverse in this activation, including the
        # async adopted disposal cordis awaits from the coroutine returned here.
        return self._dispose_adopted()

    def _dispose_adopted(self) -> Any:
        adopted, self._adopted = self._adopted, []
        if not adopted:
            return None

        async def run() -> None:
            # continue-and-record: one adopted effect whose disposal raises
            # must not starve the effects adopted BEFORE it (which unwind
            # after it — `reversed`). The inverses themselves are already
            # guarded (`acquire`/`_guard`), so anything reaching here is a
            # failure of the effect machinery, recorded at the same severity
            # rather than silently truncating the unwind.
            for effect in reversed(adopted):
                try:
                    result = effect()
                    if inspect.isawaitable(result):
                        await result
                except BaseException as error:  # noqa: BLE001 — recorded, never re-raised
                    self._record_phase1_residue(effect, error)

        return run()


# ---------------------------------------------------------------------------
# item 245: the session owner, the deferral queue, the commit/abort verbs
# ---------------------------------------------------------------------------


class SessionCommitError(RuntimeError):
    """A session commit could not proceed as asked — most often a stale manifest
    hash (the queue or the live composition changed since enumeration), which is
    refused rather than flushing a superset of what the human approved."""


class ApprovalCrossingRefused(RuntimeError):
    """A typed-approval crossing (`emit … with a`) that no valid `Approval[C]`
    covers (item 246, invariant 1 runtime half). Raised at the crossing BEFORE
    the host body runs, so a hand-built IR or a backend bug cannot cross a
    sensitive boundary silently. Carries the capability and the binding that
    failed for the why-trace."""

    def __init__(self, capability: str, reason: str) -> None:
        super().__init__(
            f"approval crossing refused for capability `{capability}`: {reason}")
        self.capability = capability
        self.reason = reason


class LeaseRefused(RuntimeError):
    """A capability lease (`effect lease …`, item 294 Slice 2) that cannot be
    acquired at runtime because no session driver / grant ledger backs it. The
    load-time gate (`Session._enforce_lease_gate`) already refuses an ungated
    lease before boot; this is the defensive twin, so a hand-built IR or a raw
    backend run cannot silently self-mint a grant with no operator ticket behind
    it (the self-mint consent bypass the design closes)."""

    def __init__(self, capability: str, reason: str) -> None:
        super().__init__(
            f"lease refused for capability `{capability}`: {reason}")
        self.capability = capability
        self.reason = reason


class LeaseHandle:
    """The value bound by `let l = effect lease …`. It names the standing grant
    the session minted from the approved ticket (by `requestId`) and carries the
    single scoped revoke exemption: `l.revoke()` retires THAT grant (its own,
    always-safe: revoking your own authority only narrows), riding the LIFO
    teardown. It names no other grant."""

    __slots__ = ("capability", "request_id", "_revoke_cb")

    def __init__(self, capability: str, request_id: str,
                 revoke_cb: "Callable[[str], Any]") -> None:
        self.capability = capability
        self.request_id = request_id
        self._revoke_cb = revoke_cb

    def revoke(self) -> Any:
        return self._revoke_cb(self.request_id)


_CLOCK_ANCHOR: list = []


def _default_now_ms() -> int:
    """Epoch-ms for approval expiry, MONOTONIC-ANCHORED (roadmap 427 F8).

    The wall clock is sampled once; every later reading is that sample plus the
    elapsed `time.monotonic_ns()`. A settimeofday, an NTP step or a DST change
    therefore cannot move this backwards, and an `expiresAt` computed from one
    reading cannot be un-passed by a later one. The MCP session overrides
    `SessionOwner.now_ms` with its own ratcheted clock; this is the standalone-run
    default, which must not be weaker."""
    import time  # noqa: PLC0415 — stdlib
    if not _CLOCK_ANCHOR:
        _CLOCK_ANCHOR.append((int(time.time() * 1000), time.monotonic_ns()))
    wall_ms, mono_ns = _CLOCK_ANCHOR[0]
    return wall_ms + (time.monotonic_ns() - mono_ns) // 1_000_000


def _approval_scope_covers(scope: Optional[str], token: str) -> bool:
    """Whether an `Approval[scope]` covers a crossing of capability `token`
    (Decision 3, `C within C'`'s scope). Exact match or a glob scope."""
    if scope is None:
        return False
    if scope == token:
        return True
    from fnmatch import fnmatchcase  # noqa: PLC0415 — stdlib, only on a crossing
    return fnmatchcase(token, scope)


def _hash_manifest(target: dict) -> str:
    """The manifest hash binding the gate target (item 245, Decision 4). Over the
    canonical JSON of the target-defining state — the deferral queue descriptors,
    the escrowed/live witnessed seqs, and the live registry's components — so any
    drift (another enqueue, a swap) recomputes to a different hash and the confirm
    is refused. Deterministic: sorted keys, no whitespace variance."""
    payload = json.dumps(target, sort_keys=True, separators=(",", ":"))
    import hashlib  # noqa: PLC0415 — stdlib, only needed on a commit
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class _Deferred:
    """One class-(b) descriptor on the deferral queue (item 245, Decision 3).
    Holds a serializable named-call descriptor for the WAL/manifest and a zero-arg
    thunk that fires the real host body at flush. The host body runs once, at
    flush, or never (on abort)."""

    __slots__ = ("receiver", "method", "args", "_fire", "seq", "fired", "error")

    def __init__(self, receiver: str, method: str, args: list,
                 fire: Callable[[], Any]) -> None:
        self.receiver = receiver
        self.method = method
        self.args = args
        self._fire = fire
        self.seq: Optional[int] = None
        self.fired = False
        self.error: Optional[BaseException] = None

    def descriptor(self) -> dict:
        return {"receiver": self.receiver, "method": self.method,
                "args": list(self.args)}

    def group(self) -> str:
        return f"{self.receiver}.{self.method}"

    def fire(self) -> None:
        """Invoke the host body once (flush). Best-effort — a raise is caught by
        the caller (continue-and-record), never here."""
        # item 443: a deferred emission is a boundary crossing that has not yet
        # happened, so an engaged E-Stop refuses it outright — nothing new
        # crosses after the button. The queue entry is left owed and enumerated,
        # never quietly dropped.
        _estop_check(f"{self.receiver}.{self.method}")
        self.fired = True
        fire, self._fire = self._fire, None
        if fire is not None:
            with _InFlight(component=self.receiver, method=self.method,
                           seq=self.seq, entry="deferred"):
                fire()


class SessionOwner:
    """The session's commit-state owner (item 245, docs/design/245-session-commit.md).

    One per driver lifetime. Owns the three session-scoped structures the commit
    verb derives its GATE TARGET from — never the runtime's per-call current
    frame (`_FRAME_BY_CTX`):

      * the DEFERRAL QUEUE — a FIFO of class-(b) descriptors, WAL-logged at
        enqueue, flushed (fired once, in program order) on commit, dropped on
        abort;
      * the DISCHARGE ESCROW — already-registered transactional/compensation
        entries of a mid-session withdrawal (a swap/undo), awaiting the verdict;
      * the LIVE-FRAME REGISTRY — every activation frame currently live.

    A session with three live components commits all three or none: the verdict
    is session-scoped, the accumulator is still the activation frame (Decision 1).
    The driver drives the two-phase commit (`enumerate` then `approve`) and the
    abort; this class holds the state and the durable WAL bookkeeping.
    """

    def __init__(self, wal_getter: Optional[Callable[[], Any]] = None) -> None:
        self._queue: list = []       # _Deferred, FIFO
        self._escrow: list = []      # _Transactional/_Compensation of withdrawn frames
        self._registry: list = []    # live Frames, registration order
        self._wal_getter = wal_getter
        self._verdict: Optional[str] = None    # None (pending) | "commit" | "abort"
        self.prompts = {"commit": 0, "perCall": 0, "residue": 0}
        # item 246 (docs/design/246-auto-approve.md, Decision 6): boundary calls
        # counted by posture at decision time. `silent` = class (a),
        # auto-approved on a checked inverse; `atCommit` = class (b), auto-approved
        # and enumerated at commit; `prompted` = class (c), a ticket surfaced to a
        # human. Class none is not a boundary call and stays out of all three
        # (and out of the percent-auto-approved denominator). The auto-approve
        # policy (Session) increments these; they are 0 for a session with no
        # policy configured, so the manifest is byte-identical there.
        self.approvals = {"silent": 0, "atCommit": 0, "prompted": 0}
        self.flush_residue: list = []
        # item 247 gap 2: the compensation residue from ESCROWED entries — the
        # entries of a mid-session-withdrawn frame whose Phase-2 offset runs in
        # `finalize_abort`, after the frame has already left `_registry`. A live
        # frame's residue stays on `frame.compensation_residue` and is merged with
        # this by `collect_compensation_residue` at the session boundary; without
        # this, an escrowed offset's failure was run but never recorded (dropped).
        self.compensation_residue: list = []
        # item 246, Decision 3: the typed-approval ledger, owned here beside the
        # deferral queue and the escrow. Each granted `Approval[C]` is one entry
        # bound to its capability, component, reach-closure candidate hash,
        # session, ttl and single-use `consumed` bit. The language-level frame
        # check (`Frame.approval_crossing`) and the operator-layer ticket path
        # (`Session.approve_ticket`) share THIS ledger — one consent mechanism,
        # two entry points. Empty and inert unless the approval policy is enabled.
        self.approval_ledger: list = []
        # whether the language-level frame check enforces: True only when the
        # session enabled the approval policy. Off = a `with a` crossing fires
        # normally (byte-identity — the edge only exists in new programs).
        self.approval_enforced: bool = False
        # the live generation's reach-closure candidate hash per component,
        # pushed by the session at load/swap. The frame check binds a token to it
        # (invariant 4, candidate-invalidates): a swap changes the hash and every
        # standing token whose closure moved fails the crossing.
        self.approval_candidates: dict = {}
        # the session identity a token is bound to (invariant 5, non-replayable):
        # a token minted in one session is refused in a later one, even over the
        # same workspace and WAL. None until the session sets it.
        self.session_id: Optional[str] = None
        # an injectable clock (ms since epoch) so expiry (invariant 3) is testable
        # without sleeping; defaults to the wall clock.
        self.now_ms: Callable[[], int] = _default_now_ms
        # item 294 Slice 2: the session's lease acquire/revoke bridge. A lease
        # `effect lease …` acquisition resolves the already-minted standing grant
        # through `lease_acquire`, and its disposer's own-requestId revoke rides
        # `lease_revoke`. Both are set by the session at owner install; None (and
        # inert) for a session with no lease-bearing program, so byte-identity
        # holds for every existing composition.
        self.lease_acquire: Optional[Callable[..., Any]] = None
        self.lease_revoke: Optional[Callable[[str], Any]] = None

    def _wal(self) -> Optional[Any]:
        return self._wal_getter() if self._wal_getter is not None else None

    # -- item 246: the typed-approval ledger -------------------------------

    def grant_approval(self, entry: dict) -> None:
        """Record a granted `Approval[C]` (item 246). Called by the session's
        `approve_ticket`/`await approval` grant; the frame check reads it back at
        the crossing. Idempotent on `requestId`."""
        rid = entry.get("requestId")
        if any(e.get("requestId") == rid for e in self.approval_ledger):
            return
        self.approval_ledger.append(dict(entry))

    def find_approval(self, capability: str, component: str,
                      candidate_hash: Optional[str] = None) -> Optional[dict]:
        """The first VALID standing approval covering this crossing (all five
        bindings, invariants 2-5): unconsumed, unexpired, same capability scope,
        same component, same reach-closure candidate hash against the live
        generation, same session. None when nothing covers it — the crossing then
        fails closed (invariant 1, runtime half)."""
        want_hash = (candidate_hash if candidate_hash is not None
                     else self.approval_candidates.get(component))
        now = self.now_ms()
        for entry in self.approval_ledger:
            if entry.get("consumed"):
                continue
            if entry.get("component") != component:
                continue                                   # invariant 5: deputy
            if not _approval_scope_covers(entry.get("capability"), capability):
                continue                                   # wrong capability
            # roadmap 427 F8: expiry LATCHES. Once this ledger entry has been
            # seen past its deadline it stays dead, so no later clock reading —
            # a stepped wall clock, an injected `now_ms` — can hand it back.
            if entry.get("expiredAt") is not None:
                continue                                   # invariant 3: expired
            exp = entry.get("expiresAt")
            if exp is not None and now > exp:
                entry["expiredAt"] = now
                continue                                   # invariant 3: expired
            if want_hash is not None and entry.get("candidateHash") != want_hash:
                continue                                   # invariant 4: swapped
            if self.session_id is not None \
                    and entry.get("session") not in (None, self.session_id):
                continue                                   # invariant 5: session
            return entry
        return None

    def consume_approval(self, entry: dict) -> None:
        """Spend the token durably BEFORE the crossing fires (Decision 3,
        consume-before-fire). The `approval-consumed` WAL record is written and
        flushed here; a crash between it and the fire leaves consumed-but-unfired
        — fail-closed, a fresh approval is demanded on recover."""
        entry["consumed"] = True
        wal = self._wal()
        if wal is not None:
            wal.record_approval_consumed(entry.get("requestId"))

    # -- registry / escrow -------------------------------------------------

    def register_frame(self, frame: "Frame") -> None:
        if frame not in self._registry:
            self._registry.append(frame)

    def withdraw_frame(self, frame: "Frame") -> None:
        try:
            self._registry.remove(frame)
        except ValueError:
            pass

    def escrow(self, entry: Any) -> None:
        if entry not in self._escrow:
            self._escrow.append(entry)

    # -- deferral queue ----------------------------------------------------

    def enqueue(self, receiver: str, method: str, args: list,
                fire: Callable[[], Any], *,
                register: Optional[str] = None,
                idempotency: Optional[Any] = None) -> "_Deferred":
        """Append a class-(b) descriptor and WAL-log it at enqueue (Decision 3).
        The intent is durable here; the outcome is a later `flushed` record.

        item 440 §(b): `register`/`idempotency` ride the descriptor into the WAL
        so a FRESH-process recover can decide whether an OWED emission is safe to
        re-issue. Both default to absent, which reads as "not re-issuable" — the
        fail-closed direction and the pre-440 behaviour."""
        descriptor = _Deferred(receiver, method, list(args), fire)
        self._queue.append(descriptor)
        wal = self._wal()
        if wal is not None:
            record = wal.record_deferred_emission(
                receiver=receiver, method=method, args=list(args),
                register=register,
                idempotency=None if idempotency is None else str(idempotency))
            descriptor.seq = record["seq"]
        return descriptor

    # -- gate target + manifest (Decision 4) -------------------------------

    def _live_entries(self) -> list:
        """Every undischarged transactional/compensation entry the commit will
        discharge — from every live registry frame plus the whole escrow. This is
        the derivation the design insists on: owner-held state, never a current
        frame."""
        entries: list = []
        for frame in self._registry:
            entries += frame._transactional
            entries += frame._compensations
        entries += self._escrow
        return entries

    def _witnessed_seqs(self) -> list:
        """The WAL seqs of every undischarged witnessed/compensation entry — the
        set the consolidated discharge record names on commit. Empty when no WAL
        is attached (seq is None); the count for the manifest uses
        :meth:`_witnessed_count` instead, which is WAL-independent."""
        return sorted({e.seq for e in self._live_entries() if e.seq is not None})

    def _witnessed_count(self) -> int:
        # `_live_entries` mixes `_Transactional` (has `.replayed`) and
        # `_Compensation` (item 247: has no `.replayed` — a compensation offsets,
        # it never replays an inverse). Read `replayed` defensively so a live
        # compensation entry counts toward the commit gate target (it discharges
        # at commit, joining the discharge record) instead of crashing the
        # manifest — the two-step commit of a session holding a compensation.
        return sum(1 for e in self._live_entries()
                   if not e.discharged and not getattr(e, "replayed", False))

    def _target(self) -> dict:
        """The gate target, hash-bound: (queue, live witnessed count, registry
        composition). Any drift — another enqueue, another witnessed call, a swap
        — changes the hash and refuses confirm. Uses a COUNT, not seqs, so the
        binding holds with or without a WAL attached."""
        return {
            "queue": [d.descriptor() for d in self._queue],
            "witnessed": self._witnessed_count(),
            "registry": sorted(f.name for f in self._registry),
        }

    def manifest(self) -> dict:
        """The commit manifest (Decision 4) — the one schema item 246 freezes
        against. `summary` is the prompt's one line; everything else is the
        evidence behind it. The hash binds the gate target."""
        summary: dict = {}
        for d in self._queue:
            summary[d.group()] = summary.get(d.group(), 0) + 1
        target = self._target()
        return {
            "deferred": [dict(d.descriptor(), site=None, group=d.group())
                         for d in self._queue],
            "summary": [{"group": g, "count": n}
                        for g, n in sorted(summary.items())],
            "fired": [],   # class-(c) crossings are logged at fire; 246 fills this
            "witnessed": {"count": target["witnessed"]},
            "residue": {"clean": True, "outstanding": []},
            "prompts": dict(self.prompts),
            # item 246: the posture tally and the headline percent, so the commit
            # manifest carries the auto-approve metrics beside the prompt counts.
            "approvals": dict(self.approvals),
            "percentAutoApproved": self.percent_auto_approved(),
            "hash": _hash_manifest(target),
        }

    def percent_auto_approved(self) -> Optional[float]:
        """`(silent + atCommit) / (silent + atCommit + prompted)` over calls that
        reached at least one crossing (item 246, Decision 6). None when no
        boundary call has been decided, so the ratio never divides by zero."""
        a = self.approvals
        denom = a["silent"] + a["atCommit"] + a["prompted"]
        if denom == 0:
            return None
        return round(100.0 * (a["silent"] + a["atCommit"]) / denom, 2)

    # -- commit (two-step, hash-bound) -------------------------------------

    def approve(self, manifest_hash: str) -> dict:
        """Confirm the commit against the approved hash (Decision 4). Recomputes
        the target hash; any drift since enumeration refuses. On match: writes
        `commit-approved` durably BEFORE the first fire, sets the verdict, and
        FLUSHES the queue FIFO. Returns a flush report. The caller then unloads
        the frames (which discharge) and calls `finalize_commit`."""
        current = _hash_manifest(self._target())
        if manifest_hash != current:
            raise SessionCommitError(
                "stale manifest hash — the deferral queue or the live "
                "composition changed since enumeration, so the confirm is "
                "refused. Re-enumerate: what fires must be exactly what was "
                "approved (item 245, Decision 4)")
        wal = self._wal()
        if wal is not None:
            wal.record_commit_approved(manifest_hash)
        self._verdict = "commit"
        self.prompts["commit"] += 1
        return self._flush()

    def _flush(self) -> dict:
        """Fire the deferral queue FIFO (program order), the causal order the
        intents were formed in. Each completed fire appends `flushed`; a raise is
        continue-and-record (`flush-residue`), so the remaining queue still
        flushes and the commit still completes."""
        wal = self._wal()
        fired, residue = [], []
        queue, self._queue = self._queue, []
        for d in queue:
            try:
                d.fire()
                fired.append(d.descriptor())
                if wal is not None and d.seq is not None:
                    wal.record_flushed(d.seq)
            except BaseException as error:  # noqa: BLE001 — best-effort, recorded
                d.error = error
                info = {"type": type(error).__name__, "message": str(error)}
                residue.append({"seq": d.seq, **info})
                self.flush_residue.append({"seq": d.seq, "error": info})
                self.prompts["residue"] += 1
                if wal is not None and d.seq is not None:
                    wal.record_flush_residue(d.seq, info)
        return {"fired": fired, "flushResidue": residue}

    def finalize_commit(self) -> list:
        """Write the ONE consolidated discharge record over every committed
        transactional/compensation seq (escrow + live frames), after the flush and
        the frame unload, before `activation-complete` (Decision 1/3). Returns the
        discharged seqs."""
        seqs = self._witnessed_seqs()
        wal = self._wal()
        if wal is not None and seqs:
            wal.record_discharge(seqs)
        return seqs

    # -- abort -------------------------------------------------------------

    def begin_abort(self) -> None:
        """Mark every live registry frame aborting BEFORE any teardown starts
        (Decision 5: the `_aborting` bit must be set before a frame's drain, or
        that drain implicitly commits it), then drop the deferral queue. Zero
        cost, zero crossings — nothing fired, so nothing to invert or offset."""
        self._verdict = "abort"
        for frame in self._registry:
            frame.abort()
        # escrowed frames' entries also revert: they were withdrawn but never
        # committed, so the session abort replays them too. Mark their frames.
        for entry in self._escrow:
            entry.frame.abort()
        self._queue = []   # DROP: no host body runs

    def finalize_abort(self) -> dict:
        """Replay the escrowed entries (reverse-seq, after the live cascade), then
        write the `aborted` completion record naming every seq whose inverse
        actually ran (Decision 5). The absence of `commit-approved` is the
        verdict; this record only lets recover tell a completed abort from a
        crashed one."""
        replayed: list = []
        # escrow replays reverse-seq, in its own two phases (transactional
        # inverses, then owed compensations) — the contract's phase rules.
        # item 247 second-pass (F5): the sort key carries the process-monotonic
        # `stamp` as a tiebreaker AFTER `seq`, so LIFO holds even in a NON-
        # recorded session where every `seq` is None: without it the key
        # collapsed to a constant 0, the stable sort kept insertion order
        # (oldest-first), and a FIFO replay of overlapping idempotent-total
        # inverses destroyed pre-session data while reporting `noResidue: true`
        # (the item-369 hazard). With a WAL, `seq` still dominates (it is
        # monotonic with registration too), so the recorded ordering is
        # unchanged.
        escrow = sorted(self._escrow,
                        key=lambda e: (e.seq if e.seq is not None else 0, e.stamp),
                        reverse=True)
        transactional = [e for e in escrow if isinstance(e, _Transactional)]
        compensations = [e for e in escrow if isinstance(e, _Compensation)]
        for entry in transactional:
            entry()   # verdict is settled (abort), so this replays
            if entry.replayed and entry.seq is not None:
                replayed.append(entry.seq)
        for entry in compensations:
            entry._run_phase2()
            # item 247 gap 2: an escrowed offset that raised is residue too —
            # record it (previously run-and-dropped), same shape and severity as
            # the live-frame Phase-2 residue, so the session boundary sees it.
            if entry.failed:
                self.compensation_residue.append(_residue_record(
                    entry, outcome="failed", attempted_flag=True,
                    attempted={"phase": 2},
                    error={"type": type(entry.error).__name__,
                           "message": str(entry.error)}))
        self._escrow = []
        # collect the seqs the live frames replayed too (their inverses ran
        # during the driver's unload)
        for frame in self._registry:
            for entry in frame._transactional:
                if entry.replayed and entry.seq is not None:
                    replayed.append(entry.seq)
        replayed = sorted(set(replayed))
        wal = self._wal()
        if wal is not None:
            wal.record_aborted(replayed)
        return {"replayed": replayed}

    # -- compensation residue (item 247 gap 2) -----------------------------

    def collect_compensation_residue(self) -> list:
        """Every `unresolved` compensation-residue fact this session produced —
        the merge of each live registry frame's `compensation_residue` and the
        escrow residue this owner captured in `finalize_abort` (item 247 gap 2,
        design Decision 2). This is the owner-held enumeration the 246 session
        boundary reads: a session that ends with an offset it could not land
        surfaces it here, never a silently growing in-memory list.

        Deduplicated by identity, so a frame that appears in both the registry
        and (transiently) elsewhere is not double-counted."""
        records: list = []
        seen: set = set()
        for frame in self._registry:
            for rec in getattr(frame, "compensation_residue", ()):  # noqa: B009
                key = id(rec)
                if key not in seen:
                    seen.add(key)
                    records.append(rec)
        for rec in self.compensation_residue:
            key = id(rec)
            if key not in seen:
                seen.add(key)
                records.append(rec)
        return records


# ---------------------------------------------------------------------------
# config resolution
# ---------------------------------------------------------------------------


class ConfigError(TypeError):
    pass


_TYPES = {"Str": str, "Int": int, "Bool": bool}

#: component name -> the configuration it last actually ran with, *after*
#: defaults were applied.  A demo can read this to show what a component
#: really got, not what the host passed in.
RESOLVED_CONFIG: dict = {}

# ConfigSchema is constructed name-less in emitted output; the resolution is
# parked here until the component's Frame (built on the very next line of the
# emitted `apply`) names it. The third element is the set of field names the
# author declared `Secret[T]` — carried alongside the values so the trace line
# can be built without the secrets and the real values still reach the
# component (item 256 Slice 3, §7b).
_pending_config: Optional[tuple] = None


def resolved_config(name: Optional[str] = None):
    """The resolved configuration of ``name`` (or every component's, as a
    dict) — the values a component actually ran with, defaults included."""
    if name is None:
        return {key: dict(value) for key, value in RESOLVED_CONFIG.items()}
    value = RESOLVED_CONFIG.get(name)
    return dict(value) if value is not None else None


def _flush_config_trace(name: str) -> Optional[dict]:
    """Attribute the parked resolution to ``name``, record it, return it.

    The trace line is an externalisation: `revl run` prints it to stdout and the
    MCP session captures it into the `revl_load` response, where it lands in a
    model's context. A field the author declared `Secret[T]` is therefore
    rendered as the redaction placeholder — the field is still named, still in
    the same position, so the line keeps saying which fields resolved and which
    defaulted; only the confidential bytes are gone (item 256 Slice 3, §7b).

    `RESOLVED_CONFIG` and the dict handed to the component keep the real value:
    the component was granted it, the log and the agent were not."""
    global _pending_config
    if _pending_config is None:
        return None
    resolved, defaulted, secret_fields = _pending_config
    _pending_config = None
    RESOLVED_CONFIG[name] = resolved
    body = ", ".join(
        f"{key}=" + (f'"{confidential.REDACTED}"' if key in secret_fields
                     else json.dumps(resolved[key]))
        for key in sorted(resolved))
    tail = f" [defaults: {', '.join(defaulted)}]" if defaulted else ""
    _record(f"{name}.config {{{body}}}{tail}")
    return resolved


class ConfigSchema:
    """Config fields as ``(name, type, default)`` triples; ``default=None``
    means required (IR ``"default": null``).

    The emitted plugin dict carries the schema as ``'Config'``, and
    cordis-py's ``resolve_config`` validates/resolves before ``apply`` runs
    (dict plugins read ``Config`` via ``dict.get`` since fork commit
    ``1c5e6f1`` — see REPORT.md F2). ``validate`` speaks cordis-py's Config
    protocol (``{issues, value}`` result); ``resolve`` is kept for
    hand-written host code that validates eagerly and wants a plain dict.
    """

    def __init__(self, fields: list, name: Optional[str] = None,
                 secret: Optional[list] = None) -> None:
        self.fields = [tuple(field) for field in fields]
        # Optional: emitted output constructs schemas positionally and
        # name-less, but a hand-written host may name one and get the
        # `<name>.config` trace event without a Frame.
        self.name = name
        # item 256 Slice 3: the fields the author declared `Secret[T]`. The
        # qualifier is stripped off the declared type before lowering, so the
        # emitter passes the names here explicitly; a hand-written host that
        # spells the qualifier in the type is honoured too. Empty for every
        # schema that declares none, which is the byte-identical path.
        self.secret_fields = frozenset(secret or ()) | frozenset(
            field[0] for field in self.fields
            if len(field) > 1 and confidential.is_secret_type(field[1]))

    def _resolve(self, config: Any) -> tuple[dict, list, list]:
        value = dict(config or {})
        defaulted: list = []
        issues = []
        for name, type_name, default in self.fields:
            if name not in value:
                if default is None:
                    issues.append(f'missing required config field "{name}"')
                    continue
                value[name] = default
                defaulted.append(name)
            expected = _TYPES.get(type_name)
            if expected is None:
                continue
            ok = isinstance(value[name], expected)
            if expected is int and isinstance(value[name], bool):
                ok = False  # bool is an int subclass; keep Int honest
            if not ok:
                issues.append(f'config field "{name}" expects {type_name}')
        return value, defaulted, issues

    def _park(self, value: dict, defaulted: list) -> None:
        global _pending_config
        # Remember the resolved value of every `Secret[T]` field, so the SAME
        # bytes are scrubbed if they surface at a capture point that carries no
        # marking of its own — a credential threaded from config into a witness,
        # say. Exact-value matching, so nothing else is affected.
        for field in self.secret_fields:
            if field in value:
                confidential.register_secret_value(value[field])
        _pending_config = (dict(value), defaulted, self.secret_fields)
        if self.name is not None:
            _flush_config_trace(self.name)

    def resolve(self, config: Any) -> dict:
        value, defaulted, issues = self._resolve(config)
        if issues:
            raise ConfigError("invalid config:\n" + "\n".join(f"  - {issue}" for issue in issues))
        self._park(value, defaulted)
        return value

    def validate(self, config: Any) -> "_ConfigResult":
        """cordis-py ``Config.validate`` protocol: return ``{issues, value}``
        instead of raising (fiber.resolve_config turns a truthy ``issues``
        into a ValidationError and the fiber lands FAILED). A valid
        resolution is parked exactly like ``resolve`` so the component's
        Frame — built next in the emitted ``apply`` — still attributes the
        ``<name>.config`` trace event and R4 ``resolved_config`` state."""
        value, defaulted, issues = self._resolve(config)
        if not issues:
            self._park(value, defaulted)
        return _ConfigResult(issues, value)


class _ConfigResult:
    """The ``{issues, value}`` object cordis-py's ``resolve_config`` expects."""

    __slots__ = ("issues", "value")

    def __init__(self, issues: list, value: dict) -> None:
        self.issues = issues
        self.value = value


# ---------------------------------------------------------------------------
# string interpolation (`format` expressions)
# ---------------------------------------------------------------------------


def fmt(template: str, *args: Any) -> str:
    """Substitute ``$0``, ``$1``… placeholders; ``$$`` is a literal dollar
    (IR v1/A4: split on placeholders first, then unescape)."""
    parts = re.split(r"(\$\$|\$\d+)", template)
    out = []
    for part in parts:
        if part == "$$":
            out.append("$")
        elif part.startswith("$") and part[1:].isdigit():
            out.append(str(args[int(part[1:])]))
        else:
            out.append(part)
    return "".join(out)


# ---------------------------------------------------------------------------
# stub stdlib: host builtins
# ---------------------------------------------------------------------------


class _Closable:
    _serial = 0

    def __init__(self) -> None:
        cls = type(self)
        cls._serial += 1
        self.serial = cls._serial
        self.closed = False
        # attribute this resource to the activation acquiring it, so a hot-swap
        # of the owning instance can capture and migrate its state. Inert
        # outside a component body (a bare `Map.new()` in a test tracks nothing).
        _register_resource(self)

    @property
    def _tag(self) -> str:
        return f"{type(self).__name__.lower()}#{self.serial}"

    def _check_open(self, op: str) -> None:
        if self.closed:
            raise RuntimeError(f"{self._tag}.{op} after close/drop — use-after-free")


# .. _pool-job-semantics:
#
# Pool and Job — the cross-tier semantics
# =======================================
#
# `Pool` and `Job` used to be no-ops ("functional placeholders" in
# docs/v2.0-roadmap.md).  They are now real state machines.  This block is
# the single normative definition; backends/typescript/runtime.ts,
# backends/rust/emit.py::_emit_host_stubs and
# backends/java/emit.py::_emit_pool_runtime/_emit_job_runtime implement
# exactly this and reference it by name.  Divergence between tiers is this
# project's recurring bug class, so the rule is: change this text first,
# then change all four implementations.
#
# Both are dependency-free and deterministic — no driver, no wall-clock, no
# threads, no timers.  Every "wait" is an explicit, bounded number of
# cooperative scheduler turns.
#
# Pool — a bounded connection pool
# --------------------------------
#   Pool.open(url, size)   `size` must be an integer >= 1, else PoolError.
#                          A url starting with "boom://" refuses (the A8
#                          test hook).  Opens with `size` idle connections
#                          numbered 1..size.  Records "<tag>.open <url>".
#   acquire() -> Int       Checks out the lowest-numbered idle connection
#                          (determinism).  PoolError if the pool is closed or
#                          exhausted.  Records
#                          "<tag>.acquire conn=<k> <in_use>/<size>".
#   release(conn)          Returns `conn`.  PoolError if the pool is closed or
#                          `conn` is not currently checked out.  Records
#                          "<tag>.release conn=<k> <in_use>/<size>".
#   query(sql) -> []       Borrows a connection for the duration of the call
#                          (so it fails when the pool is exhausted), records
#                          "<tag>.query <sql>", returns no rows.
#   execute(sql) -> 1      Same borrow, records "<tag>.execute <sql>",
#                          returns 1 (rows affected).
#   close()                PoolError if already closed.  Force-releases every
#                          checked-out connection, then capacity is 0 and
#                          EVERY later operation (including close) raises.
#                          Records "<tag>.close <url>".
#   capacity()/in_use()/available()   accounting, for assertions.
#
#   Invariant: while open, in_use() + available() == capacity() == size;
#   after close(), capacity() == in_use() == available() == 0.
#
#   The borrow inside query/execute is deliberately *silent* (no trace event)
#   so existing trace expectations stay byte-identical; only an explicit
#   acquire/release is traced.
#
# Job — a cancellable asynchronous unit of work
# ---------------------------------------------
#   Job.TICKS = 5          scheduler turns of simulated work.
#   Job.run(name)          -> a handle in state "pending"; records
#                          "job.run <name> start" at *call* time.
#   await handle           Suspends the caller, yielding to the scheduler once
#                          per remaining tick, then state becomes "done",
#                          records "job.run <name> done", and the awaited
#                          value is `name`.  Awaiting a job that is (or
#                          becomes) cancelled raises JobCancelled.  Awaiting a
#                          done job again returns `name` without re-recording.
#   handle.cancel() -> Bool  pending -> "cancelled" (True) and records
#                          "job.run <name> cancelled"; on a done or already
#                          cancelled job it is a no-op returning False.
#                          A divert/teardown that cancels the awaiting task
#                          cancels the job — that is the A1 boundary made
#                          observable.
#   handle.state()         "pending" | "done" | "cancelled"
#   Job.pending()          how many handles are still "pending" — a component
#                          torn down mid-await leaves this > 0 unless the
#                          teardown cancelled.
#   Job.reset()            test helper: forget every handle.
#
# Naming per tier follows that tier's conventions (py `in_use()`, TS
# `inUse()`, rust `in_use()`, java `inUse()`); the state machines, the error
# conditions and the trace strings are identical.  One documented entry-point
# difference: rust splits `Job.run` into `Job::spawn(name) -> JobHandle` (the
# handle, as on the other tiers) and `pub async fn Job::run(name) -> String`
# (the async shorthand the emitted `await Job.run(name)` call site uses), so
# that emitted rust stays a plain `.await`.  Same state machine either way.
# Java has no coroutines, so its handle is driven by `handle.await()` rather
# than by the language's `await`.


class PoolError(RuntimeError):
    """A pool used past its capacity or past its close."""


class Pool(_Closable):
    """A bounded connection pool over a deterministic in-memory database.

    Real pool accounting (see :ref:`pool-job-semantics`): a finite set of
    connections, acquire/release bookkeeping, an exhaustion error, and a
    close that actually releases.  The *database* behind it is still a
    deterministic fake — no driver dependency — so `query` returns no rows
    and `execute` reports one affected row.
    """

    _serial = 0

    def __init__(self, url: str, size: int) -> None:
        super().__init__()
        self.url = url
        self.size = size
        self._idle: list = list(range(1, size + 1))
        self._in_use: list = []
        self.queries: list = []
        self.executed: list = []

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, url: str, size: int) -> "Pool":
        if isinstance(url, str) and url.startswith("boom://"):
            # deliberate test hook: a refusing acquisition, so suites can
            # exercise mid-body failure semantics (IR v1/A8, paper L-Raise)
            _record(f"pool.open refused {url}")
            raise RuntimeError(f"refused to open {url}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise PoolError(f"pool size must be an integer >= 1 (got {size!r})")
        pool = cls(url, size)
        _record(f"{pool._tag}.open {url}")
        return pool

    def close(self) -> None:
        self._check_open("close")
        self._in_use.clear()  # a close releases what is still checked out
        self._idle.clear()
        self.closed = True
        _record(f"{self._tag}.close {self.url}")

    # -- accounting --------------------------------------------------------

    def capacity(self) -> int:
        return 0 if self.closed else self.size

    def in_use(self) -> int:
        return len(self._in_use)

    def available(self) -> int:
        return len(self._idle)

    def _check_open(self, op: str) -> None:
        if self.closed:
            raise PoolError(f"{self._tag}.{op} after close/drop — use-after-free")

    def _borrow(self, op: str) -> int:
        self._check_open(op)
        if not self._idle:
            raise PoolError(
                f"{self._tag}.{op} exhausted (size={self.size}, in_use={len(self._in_use)})"
            )
        conn = self._idle.pop(0)
        self._in_use.append(conn)
        return conn

    def _give_back(self, conn: int) -> None:
        self._in_use.remove(conn)
        self._idle.append(conn)
        self._idle.sort()

    # -- explicit acquire/release (traced) ---------------------------------

    def acquire(self) -> int:
        conn = self._borrow("acquire")
        _record(f"{self._tag}.acquire conn={conn} {len(self._in_use)}/{self.size}")
        return conn

    def release(self, conn: int) -> None:
        self._check_open("release")
        if conn not in self._in_use:
            raise PoolError(f"{self._tag}.release conn={conn} is not checked out")
        self._give_back(conn)
        _record(f"{self._tag}.release conn={conn} {len(self._in_use)}/{self.size}")

    # -- statements (borrow a connection for the call) ---------------------

    def query(self, sql: str) -> list:
        conn = self._borrow("query")
        try:
            self.queries.append(sql)
            _record(f"{self._tag}.query {sql}")
            return []
        finally:
            self._give_back(conn)

    def execute(self, sql: str) -> int:
        conn = self._borrow("execute")
        try:
            self.executed.append(sql)
            _record(f"{self._tag}.execute {sql}")
            return 1
        finally:
            self._give_back(conn)


class JobCancelled(RuntimeError):
    """Raised when a cancelled job is awaited."""


class JobHandle:
    """One asynchronous unit of work (see :ref:`pool-job-semantics`).

    Awaitable, cancellable, and observable while in flight — `await
    Job.run(...)` therefore exercises the A1 divert boundary against real
    async state instead of a no-op.  Determinism: the work is exactly
    ``Job.TICKS`` cooperative scheduler turns, never a timer.
    """

    __slots__ = ("name", "serial", "_state", "_remaining")

    def __init__(self, name: str) -> None:
        Job._serial += 1
        self.serial = Job._serial
        self.name = name
        self._state = "pending"
        self._remaining = Job.TICKS
        Job._handles.append(self)
        _record(f"job.run {name} start")

    def state(self) -> str:
        return self._state

    @property
    def remaining(self) -> int:
        return self._remaining

    def cancel(self) -> bool:
        """pending -> cancelled (True); a no-op returning False otherwise."""
        if self._state != "pending":
            return False
        self._state = "cancelled"
        _record(f"job.run {self.name} cancelled")
        return True

    async def _drive(self) -> str:
        import asyncio  # noqa: PLC0415 — only needed when a job is awaited

        if self._state == "done":
            return self.name
        if self._state == "cancelled":
            raise JobCancelled(f'job "{self.name}" cancelled')
        try:
            while self._remaining > 0:
                await asyncio.sleep(0)
                if self._state == "cancelled":
                    raise JobCancelled(f'job "{self.name}" cancelled')
                self._remaining -= 1
        except asyncio.CancelledError:
            # a divert (or any teardown) during the await cancels the job
            self.cancel()
            raise
        self._state = "done"
        Job.runs.append(self.name)
        _record(f"job.run {self.name} done")
        return self.name

    def __await__(self):
        return self._drive().__await__()

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<job#{self.serial} {self.name} {self._state}>"


class Job:
    """Async host builtin (IR v1): `Job.run(name)` schedules a cancellable
    unit of work and returns an awaitable handle — see
    :ref:`pool-job-semantics`."""

    TICKS = 5

    runs: list = []          # names of jobs that completed (back-compatible)
    _handles: list = []
    _serial = 0

    @classmethod
    def run(cls, name: str) -> JobHandle:
        return JobHandle(name)

    @classmethod
    def pending(cls) -> int:
        """Handles still in flight — teardown residue, made countable."""
        return sum(1 for handle in cls._handles if handle.state() == "pending")

    @classmethod
    def handles(cls) -> list:
        return list(cls._handles)

    @classmethod
    def reset(cls) -> None:
        cls._handles.clear()
        cls.runs.clear()
        cls._serial = 0


# ---------------------------------------------------------------------------
# Stream[T] reactive types (roadmap item 130, docs/design/130-stream-reactive-types.md)
# ---------------------------------------------------------------------------
#
# This is the REFERENCE definition of the subscribe / next / close protocol
# (design §4.6). A `Stream[T]` is a capability to acquire a single-consumer
# subscription; the subscription is an acquire/undo BRACKET whose inverse
# `close` runs on the owner's teardown, so unloading the owner CLOSES the
# stream before the owner disappears (the core guarantee, §0). Two review-
# critical properties live here and nowhere else:
#
#   * cancellation-first `next` (§9 Part A). `next` parks on a fresh future
#     woken by an item, a terminal, OR `close`; the loop checks the tripped
#     cancel flag BEFORE a buffered item, so `close` during a parked `next`
#     resolves it as terminal `Closed` and the bracket inverse is reachable off
#     the teardown path — an outstanding `next` on a dead provider can neither
#     deadlock nor leak.
#   * provider death is a terminal, never silence (§9 Part B). A source's
#     `close()`/`fault()` delivers `Closed`/`Faulted` to every live
#     subscription's outstanding `next`, so a `next` is always terminated by
#     exactly one of owner-teardown or a provider terminal.
#
# Slice 2 adds the rest of §4.4's declared policies and the §1 pure combinators
# without touching either property. Every subscription's buffer is BOUNDED —
# there are no unbounded buffers — and the overflow policy is declared at
# `subscribe`:
#
#   error (default)  terminal `Faulted(overflow)`; the subscription closes
#   drop_newest      discard the incoming item, RECORDED (never silent)
#   drop_oldest      evict the buffer head, RECORDED (never silent)
#   block            refuse the delivery and PAUSE the provider (the `Paused`
#                    state index) until the consumer drains
#
# `block`'s resume is the item's one time-windowed behavior, and it fires on the
# deterministic test clock (§8): a `drain <n><unit>` window arms an `after`
# schedule whose firing re-checks the buffer, so a paused provider resumes on
# `Clock.advance(ms)` — a step in the timeline, never a wall-clock sleep. With no
# window declared the resume is eager, at the `next` that drains the buffer.
#
# The combinators (`map`/`filter`/`take`) are DERIVED STREAMS: `StreamStage`
# links sit between the source and the subscription, and a link's `close` closes
# its upstream link, so the chain rides the ONE bracket the `subscribe`
# registers. Both review-critical properties survive the chain by construction —
# cancellation is the consumer-end `_closed` flag plus the owner-withdrawal poll
# (neither of which a stage can intercept), and a terminal pushed at the source
# propagates through every link to the consumer's outstanding `next`.


class StreamFaulted(RuntimeError):
    """A subscription's `next` observed a terminal `Faulted(reason)` — a
    provider abort (§4.3) or a bounded-buffer overflow under the `error` policy
    (§4.4). Carries the reason so a consumer/teardown can attribute it."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _StreamClosed:
    """The terminal value a `next` returns on an orderly close — the `Closed`
    event (design §4.3). A singleton so a consumer can identity-test it."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return "<stream Closed>"


STREAM_CLOSED = _StreamClosed()


class StreamSource:
    """The provider side of a `Stream[T]` (design §1). A test provider `emit`s
    items explicitly; `close()` is the terminal-delivering inverse the
    subscription's core guarantee rests on (§0), and `fault(reason)` models a
    provider abort. Registered on `Stream._sources` so a harness can drive it,
    exactly as `Job._handles` exposes in-flight jobs.

    Slice 3 makes the SAME object the derived stream behind `subscribe
    merge(a, b)` (design §1): a merged stream is a provider whose items come
    from its upstreams instead of a host, so a merge composes — it can be
    subscribed, or merged again — with no second class of stream."""

    DEFAULT_CAPACITY = 8

    def __init__(self, kind: str = "source", up: "list | None" = None) -> None:
        self._subs: list = []
        self._state = "open"          # open | closed | faulted
        self._kind = kind             # source | merge
        self._up: list = list(up or [])       # sources feeding a merged stream
        self._down: list = []                 # merged streams fed by this one
        self._pending = len(self._up)         # upstreams not yet terminal
        self._reason: Optional[str] = None
        Stream._sources.append(self)
        _record(f"stream.{kind} open")

    @property
    def state(self) -> str:
        return self._state

    @property
    def _derived(self) -> bool:
        """Whether this stream is OWNED by the subscription below it rather than
        by a bracket of its own (item 130). A `Stream.source()` provider is not:
        it has its own `effect … undo …`. A `merge(a, b)` fan-in is, exactly as a
        Slice 2 combinator link is, so one bracket inverse unwinds the whole
        derived chain and stops at the providers."""
        return self._kind != "source"

    def emit(self, item: Any) -> bool:
        """Deliver one item to the single consumer. A no-op once terminal.

        Returns whether the item was ACCEPTED. A `False` is backpressure the
        provider can see (a `block`-policy pause, an `error`-policy overflow, an
        exhausted `take`) — the provider is told, so a refusal is never a silent
        loss (§4.4). The `drop_*` policies accept the delivery and record the
        discard instead."""
        if self._state != "open":
            return False
        accepted = self._forward(item)
        _record(f"stream.emit {item}" if accepted
                else f"stream.emit {item} refused")
        return accepted

    def _forward(self, item: Any) -> bool:
        """Carry one item to this stream's consumer, and into any merged stream
        fed by it (the Slice 3 fan-in path; identical for a source and a merge).

        Returns downstream ACCEPTANCE, exactly as a combinator link does: a
        `block` pause or an `error` overflow anywhere below reaches the provider
        through the fan-in rather than being swallowed by it, so a refusal is
        never a silent loss (§4.4)."""
        if self._state != "open":
            return False
        accepted = True
        for sub in list(self._subs):
            if not sub._deliver(item):
                accepted = False
        for down in list(self._down):
            if not down._forward(item):
                accepted = False
        return accepted

    def close(self) -> bool:
        """Orderly provider teardown: deliver `Closed` to every subscription and
        release. The terminal that rule 3.6 requires (§9 Part B).

        For a merged stream this is also the DETACH from both upstreams, so no
        source keeps feeding — or holding a reference to — a fan-in whose owner
        is gone. Multi-source teardown is then plain LIFO on one stack."""
        first = self._state == "open"
        if first:
            self._state = "closed"
            for sub in list(self._subs):
                sub._terminate("closed", None)
            for down in list(self._down):
                down._upstream_terminal("closed", None)
        ups, self._up = list(self._up), []
        # A merged stream leaves its upstreams on the way out. A DERIVED
        # upstream (a nested `merge`) is owned by this one, so it closes with
        # it; a plain source is left to its own bracket.
        for up in ups:
            up._detach_down(self)
            if up._derived:
                up.close()
        if first:
            _record(f"stream.{self._kind} close")
        return first

    def fault(self, reason: str = "provider fault") -> bool:
        """Provider abort: deliver `Faulted(reason)` to every subscription's
        outstanding `next` (never a silent pending, §4.3)."""
        if self._state != "open":
            return False
        self._state = "faulted"
        self._reason = reason
        _record(f"stream.{self._kind} fault {reason}")
        for sub in list(self._subs):
            sub._terminate("faulted", reason)
        for down in list(self._down):
            down._upstream_terminal("faulted", reason)
        return True

    def _detach(self, sub: "Subscription") -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    def _attach_down(self, merged: "StreamSource") -> None:
        if self._state != "open":
            merged._upstream_terminal(self._state, self._reason)
            return
        self._down.append(merged)

    def _detach_down(self, merged: "StreamSource") -> None:
        if merged in self._down:
            self._down.remove(merged)

    def _upstream_terminal(self, kind: str, reason: Optional[str]) -> None:
        """How a merged stream learns one of its sources is done (item 130
        Slice 3). A FAULT propagates at once — no silent loss. An orderly CLOSE
        only counts down: the fan-in stays live while any source is, so one
        source's death never strands a consumer the other can still feed, and
        when the LAST source closes the merged stream delivers its own `Closed`
        — so a parked `next` is terminated, never left on a dead fan-in."""
        if self._state != "open":
            return
        if kind == "faulted":
            self._state = "faulted"
            self._reason = reason
        else:
            self._pending -= 1
            if self._pending > 0:
                return
            self._state = "closed"
            kind = "closed"
        for sub in list(self._subs):
            sub._terminate(kind, reason)
        for down in list(self._down):
            down._upstream_terminal(kind, reason)


class StreamStage:
    """One link of a derived-stream combinator chain — `map(f)`, `filter(p)` or
    `take(n)` (design §1, Slice 2).

    A stage is BOTH a subscriber of its upstream (it exposes `_deliver` /
    `_terminate` / `_detach`) and an upstream of the next link (it exposes
    `_subs`), so a chain is `StreamSource -> stage -> … -> Subscription` and
    nothing in the source or the subscription needs to know the chain exists.

    Teardown stays ONE LIFO stack: `close` detaches this link from its upstream
    and then closes that upstream when it is itself a stage, so the consumer's
    single bracket inverse unwinds the whole chain down to (but not including)
    the provider — the provider is closed by its OWN bracket, which is what keeps
    the Slice 1 close-order proof intact.

    The transforms are G6-pure (rule 3.5), enforced at admission, so a stage
    never introduces an effect, a suspension, or a failure path of its own."""

    __slots__ = ("_upstream", "_kind", "_arg", "_subs", "_state", "_remaining")

    # a combinator link is always owned by the subscription below it, never by a
    # bracket of its own (see `StreamSource._derived`)
    _derived = True

    def __init__(self, upstream: Any, kind: str, arg: Any) -> None:
        if kind not in ("map", "filter", "take"):  # pragma: no cover — emitter invariant
            raise ValueError(f"unknown stream combinator {kind!r}")
        self._upstream = upstream
        self._kind = kind
        self._arg = arg
        self._subs: list = []
        self._state = "open"          # open | closed
        self._remaining = int(arg) if kind == "take" else None
        upstream._subs.append(self)
        Stream._stages.append(self)
        _record(f"stream.stage {kind}")

    @property
    def state(self) -> str:
        return self._state

    # upstream -> this link -> downstream ---------------------------------------
    def _deliver(self, item: Any) -> bool:
        """Transform and forward one item. Returns downstream acceptance, so
        `block` backpressure at the consumer end reaches the provider THROUGH the
        chain rather than being swallowed by a link."""
        if self._state != "open":
            return False
        if self._kind == "map":
            item = self._arg(item)
        elif self._kind == "filter":
            if not self._arg(item):
                # a predicate rejection is not backpressure: the provider's emit
                # succeeded, this derived stream simply has nothing to forward.
                return True
        else:  # take
            if self._remaining <= 0:
                return False
        accepted = True
        for sub in list(self._subs):
            if not sub._deliver(item):
                accepted = False
        if self._kind == "take" and accepted:
            self._remaining -= 1
            if self._remaining == 0:
                # `take(n)` is exhausted: the derived stream ends with a `Closed`
                # TERMINAL pushed downstream (never silence, §4.3), and this link
                # detaches from its upstream so the provider stops feeding it.
                _record("stream.take exhausted")
                self._upstream._detach(self)
                for sub in list(self._subs):
                    sub._terminate("closed", None)
        return accepted

    def _terminate(self, kind: str, reason: Optional[str]) -> None:
        """Forward a provider terminal downstream. This is what makes §9 Part B
        hold END TO END through a chain: a `Closed`/`Faulted` pushed at the
        source reaches the consumer's outstanding `next` through every link."""
        for sub in list(self._subs):
            sub._terminate(kind, reason)

    def _detach(self, sub: Any) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    def close(self) -> bool:
        """The chained inverse: release this link and its upstream chain.
        Idempotent and infallible (G5) — teardown never suspends."""
        if self._state != "open":
            return False
        self._state = "closed"
        self._upstream._detach(self)
        _record(f"stream.stage close {self._kind}")
        # the upstream closes with this link when it is DERIVED — another
        # combinator link, or a Slice 3 `merge(a, b)` fan-in. A provider is left
        # to its own bracket.
        if self._upstream._derived:
            self._upstream.close()
        return True

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<stream stage {self._kind} {self._state}>"


def _fiber_withdrawn(ctx: Any) -> bool:
    """True once the owning activation is no longer live (its fiber left the
    ACTIVE/LOADING states — UNLOADING / FAILED / DISPOSED / PENDING).

    This is the cancellation-first signal §9 Part A needs on the event-loop
    tier. cordis disposal AWAITS the body's in-flight `await` before it runs the
    collected inverses (the divert-inertia contract, item 131: an in-flight
    acquisition LANDS, it is never cancelled), so a `next` parked forever would
    DEADLOCK teardown behind an await that never lands. The fiber's state flips
    to UNLOADING *synchronously* when withdrawal begins — before that await — so
    a `next` that observes it resolves as terminal `Closed`, the body lands, and
    the bracket inverse `close` is reachable off the teardown path. Duck-typed so
    the runtime keeps no hard import of the cordis `FiberState` enum."""
    if ctx is None:
        return False
    try:
        state = ctx.fiber.state
    except Exception:  # pragma: no cover — a non-cordis owner never withdraws
        return False
    name = getattr(state, "name", str(state))
    return name not in ("ACTIVE", "LOADING")


# item 416b: the parked-`next` withdrawal sweep.
#
# Three of the four conditions a parked `next` waits on are owned by the
# `Subscription` itself (a delivered item, a provider terminal, the cancel
# token), so each of those can WAKE the consumer directly. The fourth, owner
# withdrawal, is not: `ctx.fiber.state` flips synchronously inside cordis with
# no callback out, and a `next` that never observes it DEADLOCKS teardown
# behind an await that never lands (see `_fiber_withdrawn`). That is why `next`
# was a per-turn poll.
#
# It stays a per-turn poll, but ONE poll for the whole process instead of one
# per parked consumer: parked owner-bearing subscriptions register here, a
# single sweeper task re-checks each DISTINCT owning context once per scheduler
# turn, and wakes the ones whose owner has withdrawn. Turn granularity and the
# cancellation-first ladder are unchanged (the sweeper only sets the wake event;
# `next` still re-reads every condition in order), so this is a scheduling fix,
# not a semantics change. Per-turn work is now O(distinct owners) — one, in the
# shape that matters, where a component parks many consumers in one activation.
_STREAM_PARKED: "list" = []
_STREAM_SWEEPER: Any = None


def _stream_park_register(sub: "Subscription") -> None:
    import asyncio  # noqa: PLC0415

    global _STREAM_SWEEPER
    _STREAM_PARKED.append(sub)
    task = _STREAM_SWEEPER
    live = task is not None and not task.done()
    if live:
        try:
            live = task.get_loop() is asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover — always inside a running loop
            live = False
    if not live:
        _STREAM_SWEEPER = asyncio.ensure_future(_stream_withdrawal_sweep())


def _stream_park_unregister(sub: "Subscription") -> None:
    try:
        _STREAM_PARKED.remove(sub)
    except ValueError:  # pragma: no cover — reset() cleared it underneath us
        pass


async def _stream_withdrawal_sweep() -> None:
    """The single per-turn owner-withdrawal poll, shared by every parked `next`.

    Exits as soon as nothing is parked, so an idle process runs no task at all;
    the next park starts a fresh one."""
    import asyncio  # noqa: PLC0415

    global _STREAM_SWEEPER
    try:
        while _STREAM_PARKED:
            seen: dict = {}
            for sub in list(_STREAM_PARKED):
                ctx = sub._ctx
                key = id(ctx)
                withdrawn = seen.get(key)
                if withdrawn is None:
                    withdrawn = seen[key] = _fiber_withdrawn(ctx)
                if withdrawn:
                    sub._signal()
            await asyncio.sleep(0)
    except asyncio.CancelledError:  # pragma: no cover — loop teardown
        pass
    finally:
        _STREAM_SWEEPER = None


class Subscription:
    """A single-consumer subscription (design §1, §4.6). `next()` awaits the
    next item raced against the cancel token; `close()` trips that token
    synchronously and releases the listener (the bracket inverse).

    `next` re-reads the same ladder it always did — cancellation-first, the
    cancel token, then owner withdrawal, then a buffered item, then a provider
    terminal — so `close`, a withdrawn owner, or a provider `close`/`fault` all
    resolve a parked `next` at the next scheduler turn, without ever waiting for
    the provider. Determinism, not wall-clock. What changed in item 416b is only
    HOW the parked consumer is woken to re-read it: the three conditions this
    object owns set a wake event directly, and owner withdrawal rides the one
    shared `_stream_withdrawal_sweep` poll rather than a poll per consumer."""

    POLICIES = ("error", "drop_newest", "drop_oldest", "block")

    def __init__(self, source: Any, policy: str = "error",
                 ctx: Any = None,
                 capacity: int = StreamSource.DEFAULT_CAPACITY,
                 drain_ms: Optional[int] = None) -> None:
        if policy not in self.POLICIES:  # pragma: no cover — checker invariant
            raise ValueError(f"unknown backpressure policy {policy!r}")
        # the IMMEDIATE upstream: the provider, or the last link of a combinator
        # chain (Slice 2). Closing this subscription unwinds the chain.
        self._source = source
        self._policy = policy
        self._capacity = capacity
        self._ctx = ctx                  # the owning activation's context, or None
        self._buffer: list = []
        self._terminal: Optional[tuple] = None   # ("closed", None) | ("faulted", reason)
        self._closed = False             # the cancel token, tripped by close()
        # `block`-policy backpressure (§4.4): `_paused` IS the `Paused` state
        # index, and `_drain_ms`/`_drain` are the clock-driven drain window (§8).
        self._paused = False
        self._drain_ms = drain_ms
        self._drain: Any = None
        # item 416b: the wake event a parked `next` blocks on, created lazily
        # because a subscription may be constructed with no running loop.
        self._wake: Any = None
        source._subs.append(self)
        Stream._subs.append(self)
        _record("stream.subscribe")

    @property
    def state(self) -> str:
        """The design's state index (§1) as the runtime sees it: `Closed` once
        the cancel token is tripped or a terminal is drained, `Paused` while a
        `block`-policy buffer is full, `Active` otherwise."""
        if self._closed:
            return "closed"
        if self._paused:
            return "paused"
        return "active"

    # provider -> subscription -------------------------------------------------
    def _deliver(self, item: Any) -> bool:
        """Buffer one item under the declared overflow policy (§4.4). Returns
        whether the delivery was ACCEPTED — a `False` is backpressure the
        provider sees, never a silent drop."""
        if self._closed or self._terminal is not None:
            return False
        if self._paused:
            # `block`: the provider is SUSPENDED, and stays suspended until the
            # subscription resumes — draining alone does not un-block it when a
            # drain window is declared (§8). Refusing here rather than at the
            # capacity check is what makes the window observable at all.
            return False
        if len(self._buffer) < self._capacity:
            self._buffer.append(item)
            self._signal()
            return True
        if self._policy == "drop_newest":
            # lossy-tolerant telemetry: discard the incoming item. Explicitly
            # opted into at `subscribe`, and RECORDED — loss is never silent.
            _record(f"stream.drop_newest {item}")
            return True
        if self._policy == "drop_oldest":
            # latest-wins gauges: evict the buffer head, keep the newest item.
            evicted = self._buffer.pop(0)
            self._buffer.append(item)
            self._signal()
            _record(f"stream.drop_oldest {evicted}")
            return True
        if self._policy == "block":
            # the provider suspends until the consumer drains: the delivery is
            # REFUSED (so `emit` returns False and the provider knows) and the
            # subscription enters the reserved `Paused` state. No implicit retry —
            # the provider re-emits once the subscription is Active again.
            self._pause()
            return False
        # backpressure `error` (default, §4.4): a full buffer is a terminal
        # `Faulted(overflow)` — deterministic, no silent loss.
        self._terminal = ("faulted", "overflow")
        self._signal()
        _record("stream.overflow")
        return False

    # `block` backpressure: pause / drain window / resume (§4.4, §8) ------------
    def _pause(self) -> None:
        if self._paused or self._closed:
            return
        self._paused = True
        _record("stream.paused")
        if self._drain_ms is not None:
            self._arm_drain()

    def _arm_drain(self) -> None:
        """Arm the drain window against the deterministic test clock. The window
        is a revertible schedule like any timer, so it has an inverse (`close`
        cancels it) and leaves no residue (§8)."""
        if self._drain is None:
            self._drain = schedule_after(self._drain_ms, self._drain_fire)

    def _drain_fire(self) -> None:
        """One drain-window firing, driven by `Clock.advance` — a step in the
        timeline, not a wall-clock wake-up. Resumes the provider if the consumer
        has made room; otherwise re-arms for the next window."""
        self._drain = None
        if self._closed or not self._paused:
            return
        if len(self._buffer) < self._capacity:
            self._resume()
        else:
            self._arm_drain()

    def _cancel_drain(self) -> None:
        if self._drain is not None:
            self._drain.cancel()
            self._drain = None

    def _resume(self) -> None:
        self._paused = False
        self._cancel_drain()
        _record("stream.resume")

    def _maybe_resume(self) -> None:
        """Called when the consumer drains an item. With no drain window the
        resume is eager; with one, only a clock `advance` past the window may
        resume the provider (§8)."""
        if not self._paused or self._closed:
            return
        if len(self._buffer) >= self._capacity:
            return
        if self._drain_ms is None:
            self._resume()

    def _terminate(self, kind: str, reason: Optional[str]) -> None:
        if self._closed or self._terminal is not None:
            return
        self._terminal = (kind, reason)
        self._signal()

    # item 416b: waking a parked `next` -----------------------------------------
    def _signal(self) -> None:
        """Wake a parked `next` so it re-reads the ladder. Never decides anything
        itself: a spurious signal costs one re-read and re-park, and a missed one
        is impossible because the event is cleared BEFORE the ladder runs, with
        no await in between."""
        wake = self._wake
        if wake is not None and not wake.is_set():
            wake.set()

    async def _park(self) -> None:
        """Suspend until something the ladder reads may have changed."""
        if self._wake is None:
            self._wake = asyncio.Event()
        if self._ctx is None:
            # no owner means withdrawal cannot happen, so nothing needs polling:
            # this consumer costs the loop nothing while it waits.
            await self._wake.wait()
            self._wake.clear()
            return
        _stream_park_register(self)
        try:
            await self._wake.wait()
        finally:
            _stream_park_unregister(self)
        self._wake.clear()

    async def next(self) -> Any:
        """Await the next item or a terminal event — a suspension point raced
        against the cancel token. Returns the item, returns `STREAM_CLOSED` on a
        `Closed` terminal (orderly close or owner withdrawal), or raises
        `StreamFaulted` on a `Faulted` terminal (provider abort / overflow)."""
        while True:
            # cancellation-first (§9 Part A): the tripped token — or a withdrawn
            # owner — wins over a buffered item, so `close`/withdrawal resolves a
            # parked `next` as `Closed` and the bracket inverse stays reachable.
            if self._closed or _fiber_withdrawn(self._ctx):
                return STREAM_CLOSED
            if self._buffer:
                item = self._buffer.pop(0)
                # draining may release a `block`-paused provider (§4.4)
                self._maybe_resume()
                return item
            if self._terminal is not None:
                kind, reason = self._terminal
                if kind == "faulted":
                    raise StreamFaulted(reason or "faulted")
                return STREAM_CLOSED
            await self._park()

    def close(self) -> bool:
        """The bracket inverse: trip the cancel token synchronously, release the
        host listener, and resolve any parked `next` as `Closed`. Infallible and
        idempotent (a no-op once closed) — teardown never suspends (G5).

        The inverse cascades through every DERIVED upstream — a Slice 2
        combinator link, or a Slice 3 `merge(a, b)` fan-in. A derived stream is
        owned by this subscription rather than by a bracket of its own, so
        closing here closes it, each link closes the one above, and closing a
        merge is what detaches it from both sources. The PROVIDERS are untouched:
        each is closed by its own bracket, which keeps the LIFO close order the
        core guarantee is pinned on."""
        if self._closed:
            return False
        self._closed = True
        self._signal()   # item 416b: resolve a parked `next` as `Closed`
        self._cancel_drain()
        self._source._detach(self)
        _record("stream.close")
        if self._source._derived:
            self._source.close()
        return True


class EventContract:
    """The contract half of a typed event (item 130 Slice 5,
    docs/design/130-stream-reactive-types.md §6).

    An event is a `Stream[T]` element with a contract, so this object holds
    exactly the two things events add on top of the stream protocol and nothing
    else: the SCHEMA every delivered item is checked against before the handler
    body runs, and the bounded window of recently admitted KEYS that collapses a
    redelivery. Everything else about an `on … as` handler — the subscription
    bracket, the cancellation-first `next`, the terminal handling, the LIFO
    teardown — is the Slice 1/4 machinery, untouched.

    A schema violation raises `StreamFaulted`, which is not a new failure mode:
    it is the same terminal a provider abort delivers, so it takes the same path
    the iteration form already defines — uncaught out of the loop, the activation
    fails, the accumulated prefix reverts LIFO, and the subscription bracket on
    that prefix CLOSES the subscription. That is §6's "a failed handler does not
    leave a subscription active", reached here without a line of new teardown.

    The dedup memory is a fixed-size LRU of key values, CONSTANT per handler, so
    it is not the per-item accumulation §4.7 refuses. Being bounded also bounds
    what it claims: a redelivery further apart than the window runs the handler
    again. This collapses redeliveries; it is not a durable exactly-once claim,
    which needs the §4.5 durable cursor (a later slice). Every decision is
    traced (`event.<name> admit` / `event.<name> duplicate`), so a collapsed
    duplicate is observable rather than silent."""

    def __init__(self, name: str, schema: dict, key: str, window: int) -> None:
        self.name = name
        self.schema = schema
        self.key = key
        self.window = max(1, int(window))
        self._seen: "OrderedDict[Any, bool]" = OrderedDict()

    def admit(self, item: Any, where: str = "") -> bool:
        """Check one delivered item against the contract; True to run the body.

        Validation comes FIRST: the key read below is only sound because the
        schema already proved the item is an object carrying that field at a
        scalar type, so there is no path where a malformed item reaches the
        dedup table (or the body) at all."""
        err = _json_schema_error(item, self.schema, "$")
        if err is not None:
            raise StreamFaulted(
                f"{where}: event {self.name} item failed its schema: {err}"
                if where else
                f"event {self.name} item failed its schema: {err}")
        key = item[self.key]
        if key in self._seen:
            self._seen.move_to_end(key)
            _record(f"event.{self.name} duplicate")
            return False
        self._seen[key] = True
        while len(self._seen) > self.window:
            self._seen.popitem(last=False)
        _record(f"event.{self.name} admit")
        return True


class Stream:
    """Host builtin (item 130): `Stream.source()` opens a provider; `subscribe`
    lowers to `Stream.subscribe(source, policy)`. `pending()` is the residue
    probe — open sources plus un-closed subscriptions — so a test can assert the
    core guarantee left no listener behind (mirrors `Job.pending`)."""

    _sources: list = []
    _subs: list = []
    _stages: list = []

    @classmethod
    def source(cls) -> StreamSource:
        return StreamSource()

    @classmethod
    def is_closed(cls, value: Any) -> bool:
        """True for the `Closed` terminal a `next` returned (item 130 Slice 4).

        The consumer-side terminal test the `every … in` iteration form ends on:
        an orderly provider close, the owner's own `close` tripping the cancel
        token, or a spent `take(n)` all resolve `next` to `STREAM_CLOSED`, and
        the loop exits. `Faulted` is deliberately NOT a value here — it RAISES
        out of `next`, so a fault aborts the iteration and the activation rather
        than reading as an ordinary end of stream (§4.3, A8). Mirrors the go
        tier's `IsStreamClosed` so the two tiers spell one predicate."""
        return value is STREAM_CLOSED

    @classmethod
    def contract(cls, name: str, schema: dict, key: str,
                 window: int) -> EventContract:
        """The per-handler contract an `on <Event> as … in <sub>` opens (item
        130 Slice 5). One per handler, built once before the loop — never per
        delivered item, which is what keeps the dedup memory constant in the
        length of the stream."""
        return EventContract(name, schema, key, window)

    @classmethod
    def merge(cls, a: StreamSource, b: StreamSource) -> StreamSource:
        """The fan-in behind `subscribe merge(a, b)` — one derived stream from
        two (item 130 Slice 3, design §1).

        Not a bracket of its own: the merged stream is OWNED by the subscription
        the emitter opens on it, so multi-source teardown rides the ONE bracket
        the `subscribe` registers. `sub.close()` closes the merge, closing the
        merge detaches it from both sources, and each source is left to its own
        bracket — one LIFO stack, no orphaned fan-in, no source left feeding a
        stream whose owner is gone."""
        merged = StreamSource("merge", up=[a, b])
        a._attach_down(merged)
        b._attach_down(merged)
        return merged

    @classmethod
    def subscribe(cls, source: StreamSource, policy: str = "error",
                  ctx: Any = None, *, stages: Optional[list] = None,
                  capacity: int = StreamSource.DEFAULT_CAPACITY,
                  drain_ms: Optional[int] = None) -> Subscription:
        """Open a single-consumer subscription, optionally through a derived
        combinator chain (Slice 2). `stages` is the emitted `[(kind, arg), …]`
        list — `('map', fn)`, `('filter', pred)`, `('take', n)` — applied
        left to right, so the LAST link is the subscription's immediate
        upstream and the chain closes downstream-first on the bracket inverse."""
        upstream: Any = source
        for kind, arg in stages or []:
            upstream = StreamStage(upstream, kind, arg)
        return Subscription(upstream, policy, ctx, capacity, drain_ms)

    @classmethod
    def pending(cls) -> int:
        """Residue: open sources + live (un-closed) subscriptions + live
        combinator links. Zero after a clean unload proves the bracket inverse
        ran and unwound the WHOLE chain (no dangling listener, no orphaned
        derived stream)."""
        return (sum(1 for s in cls._sources if s._state == "open")
                + sum(1 for s in cls._subs if not s._closed)
                + sum(1 for s in cls._stages if s._state == "open"))

    @classmethod
    def sources(cls) -> list:
        return list(cls._sources)

    @classmethod
    def last_source(cls) -> Optional[StreamSource]:
        return cls._sources[-1] if cls._sources else None

    @classmethod
    def stages(cls) -> list:
        return list(cls._stages)

    @classmethod
    def reset(cls) -> None:
        cls._sources.clear()
        cls._subs.clear()
        cls._stages.clear()
        # item 416b: a reset between runs drops the park registry too, so the
        # sweeper from a finished loop cannot hold a dead subscription alive.
        _STREAM_PARKED.clear()


# ---------------------------------------------------------------------------
# Time as a coeffect (roadmap item 57, docs/time-coeffect.md)
# ---------------------------------------------------------------------------
#
# A timer (`every 30s { … }` / `after 5m { … }`) is a *revertible schedule*:
# arming it registers a firing with the clock, and its inverse is cancellation,
# so the emitted body yields `lambda: handle.cancel()` and the component's own
# teardown drains it like any other effect (no orphaned interval — the R1/R4
# residue proof catches a leak through the same `schedule`/`cancel` acquire
# pair Pool.open/close uses).
#
# Determinism is the whole point: the clock is a *coeffect the harness
# provides*, not wall-clock. `Clock.now()` only moves when something calls
# `Clock.advance(ms)` — `revl test`/replay drives it — so a firing is a
# deterministic step in the timeline (`fires on the 3rd tick`), never a race,
# and the fault sweep (item 30) can inject "fail at the third firing". A
# production driver would pump `advance` from a real monotonic source; the
# reference tier keeps it explicit so tests are reproducible.


class TimerHandle:
    """One armed timer. Live until it is cancelled (`every`, and an `after`
    before it fires) or has fired to completion (`after`). Cancellation is the
    schedule's inverse — idempotent, and a no-op once the timer is spent."""

    __slots__ = ("serial", "mode", "interval_ms", "_body", "state", "next_at", "fired")

    def __init__(self, mode: str, interval_ms: int, body: Callable[[], Any]) -> None:
        if mode not in ("every", "after"):  # pragma: no cover — emitter invariant
            raise ValueError(f"timer mode must be 'every' or 'after', got {mode!r}")
        if not isinstance(interval_ms, int) or interval_ms <= 0:  # pragma: no cover
            raise ValueError(f"timer interval must be a positive int (ms), got {interval_ms!r}")
        Clock._serial += 1
        self.serial = Clock._serial
        self.mode = mode
        self.interval_ms = interval_ms
        self._body = body
        self.state = "live"
        self.next_at = Clock._now_ms + interval_ms
        self.fired = 0
        Clock._timers.append(self)
        _record(f"{self._tag}.schedule {mode} {interval_ms}ms")

    @property
    def _tag(self) -> str:
        return f"timer#{self.serial}"

    def cancel(self) -> bool:
        """live -> cancelled (True); a no-op returning False once spent. The
        derived inverse the emitted body yields — running it on teardown proves
        the schedule leaves no residue."""
        if self.state != "live":
            return False
        self.state = "cancelled"
        _record(f"{self._tag}.cancel")
        return True

    def _fire(self) -> None:
        self.fired += 1
        Clock._firings.append((self.serial, Clock._now_ms))
        _record(f"{self._tag}.fire #{self.fired} at {Clock._now_ms}ms")
        self._body()
        if self.mode == "after":
            # a one-shot's schedule is spent once it fires; release it through
            # the same `cancel` verb so the residue trace stays balanced and the
            # teardown's own `handle.cancel()` is a clean no-op.
            self.state = "done"
            _record(f"{self._tag}.cancel")

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<timer#{self.serial} {self.mode} {self.interval_ms}ms {self.state}>"


class Clock:
    """The clock coeffect (item 57). Time advances only when the harness calls
    :meth:`advance`; timer firings are deterministic timeline steps, not
    wall-clock races (docs/time-coeffect.md)."""

    _now_ms = 0
    _serial = 0
    _timers: list = []      # every TimerHandle ever armed, registration order
    _firings: list = []     # (timer serial, now_ms) per firing, in fire order

    @classmethod
    def now(cls) -> int:
        return cls._now_ms

    @classmethod
    def advance(cls, ms: int) -> int:
        """Advance logical time by ``ms``, firing every timer that comes due —
        earliest first, ties broken by arm order — and re-arming `every`
        timers across the whole span. Returns the number of firings, so a test
        can assert exactly how many steps an advance produced."""
        if not isinstance(ms, int) or ms < 0:
            raise ValueError(f"clock advance must be a non-negative int (ms), got {ms!r}")
        target = cls._now_ms + ms
        count = 0
        # An event loop rather than a per-timer pass: an `every` re-arms and may
        # fire again within the same advance, and firings must interleave across
        # timers in true time order. Bounded because each iteration consumes one
        # firing and every `next_at` strictly increases (interval_ms > 0).
        while True:
            due = [t for t in cls._timers
                   if t.state == "live" and t.next_at <= target]
            if not due:
                break
            timer = min(due, key=lambda t: (t.next_at, t.serial))
            cls._now_ms = timer.next_at
            if timer.mode == "every":
                timer.next_at += timer.interval_ms
            timer._fire()
            count += 1
        cls._now_ms = target
        return count

    @classmethod
    def pending(cls) -> int:
        """Live timers — a teardown that abandons one leaves this > 0 (mirrors
        ``Job.pending``); the countable form of the no-orphaned-interval proof."""
        return sum(1 for t in cls._timers if t.state == "live")

    @classmethod
    def firings(cls) -> list:
        """The recorded firing log: ``(timer serial, fired-at ms)`` in order."""
        return list(cls._firings)

    @classmethod
    def reset(cls) -> None:
        cls._now_ms = 0
        cls._serial = 0
        cls._timers = []
        cls._firings = []


def schedule_every(interval_ms: int, body: Callable[[], Any]) -> TimerHandle:
    """Arm a periodic timer against the clock coeffect (`every`). The returned
    handle's :meth:`~TimerHandle.cancel` is the schedule's inverse."""
    return TimerHandle("every", interval_ms, body)


def schedule_after(interval_ms: int, body: Callable[[], Any]) -> TimerHandle:
    """Arm a one-shot delayed timer against the clock coeffect (`after`)."""
    return TimerHandle("after", interval_ms, body)


class Map(_Closable):
    """In-memory key/value store with an explicit drop."""

    _serial = 0

    def __init__(self) -> None:
        super().__init__()
        self.data: dict = {}

    @classmethod
    def new(cls) -> "Map":
        instance = cls()
        _record(f"{instance._tag}.new")
        return instance

    def drop(self) -> None:
        self._check_open("drop")
        self.closed = True
        self.data.clear()
        _record(f"{self._tag}.drop")

    def get(self, key: Any) -> Any:
        self._check_open("get")
        return self.data.get(key)

    # -- iteration surface (docs/stdlib-2.0.md §Map) -----------------------
    # `size()`/`keys()` are the value-Map iteration builtins, and the checker
    # promises them on a host `Map.new()` receiver too (the emitter lowers
    # both as plain method calls on this object). They mirror the value-Map
    # semantics exactly: `keys()` yields the keys in ascending canonical Str
    # order (python str comparison IS code-point order, so sorted() is exact),
    # `size()` is the entry count. Read-only, like `get` — no trace record.

    def keys(self) -> list:
        self._check_open("keys")
        return sorted(self.data)

    def size(self) -> int:
        self._check_open("size")
        return len(self.data)

    def insert(self, key: Any, value: Any) -> None:
        self._check_open("insert")
        self.data[key] = value
        _record(f"{self._tag}.insert {key}")

    def insert_if_absent(self, key: Any, value: Any) -> bool:
        # item 397: the atomic compare-and-set. On this tier the whole method
        # is one synchronous, suspension-free step (no await), so it is atomic
        # by construction under the reference runtime's run-to-completion model
        # (backends/python/runtime.py `.. _pool-job-semantics:`): no task can
        # interleave between the membership test and the insert. Returns whether
        # it inserted; a `false` (key already present) leaves the existing value
        # untouched. The trace records the outcome so the residue prover can
        # fold it into the outstanding-key fingerprint (a `true` counts the key
        # as set, a `false` counts nothing).
        self._check_open("insert_if_absent")
        if key in self.data:
            _record(f"{self._tag}.insert_if_absent {key} -> false")
            return False
        self.data[key] = value
        _record(f"{self._tag}.insert_if_absent {key} -> true")
        return True

    def remove(self, key: Any) -> None:
        self._check_open("remove")
        self.data.pop(key, None)
        _record(f"{self._tag}.remove {key}")

    # -- migratable state (hot-swap with live instances) -------------------
    # A Map *is* an instance's state: its entries. `__revl_state__` snapshots
    # them and `__revl_restore__` writes them into a fresh Map under the
    # successor template, so a hot-swap of the owning instance preserves the
    # store rather than starting cold. A resource without this pair (e.g. a
    # `Pool`, whose checkouts are transient) migrates as a fresh equivalent.

    def __revl_state__(self) -> dict:
        self._check_open("__revl_state__")
        return dict(self.data)

    def __revl_restore__(self, state: dict) -> None:
        self._check_open("__revl_restore__")
        self.data = dict(state)
