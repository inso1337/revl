"""Telling an emitter's REFUSAL apart from its FAULT, in one place (issue #1406).

Every backend defines its own ``class EmitError(ValueError)`` INSIDE its
dynamically loaded ``emit.py``. Three consequences drive this module:

* there is no class under ``src/`` to name in an ``except`` clause, so no caller
  can write ``except EmitError``;
* loading ``emit.py`` twice yields two unrelated classes, so the class has to be
  read off the module object that will raise, not off any other one;
* it inherits from ``ValueError``, so the lazy stand-in — ``except ValueError``
  — swallows half of every emitter's real internal faults as well.

So each of the twelve entry points under ``src/revl`` that loads a backend
emitter has to decide, by hand, between three outcomes:

1. **the backend module is absent here** — a legitimate quiet skip;
2. **the emitter refused this document** — an ANSWER the caller must report,
   carrying the emitter's own sentence;
3. **the emitter faulted** — a bug, which must stay loud.

Getting that wrong produced issue #1393 (``revl run`` handed the refusal over as
a 26-line traceback), issue #1400 (``revl bundle`` swallowed refusals AND real
crashes into a silent omission and exited 0) and issue #1403 (``truc reproduce``
caught only ``ImportError``, so a refusal escaped the attestation path as a
12-frame traceback). Each was found by accident while someone was looking at
something else, and the first two fixes each grew their own private copy of the
helper below. Six callers remembering the same three-way distinction is the
hand-kept-mirror shape, so the distinction lives here and the callers import it.

``tests/test_emitter_entry_points_1406.py`` holds the census of who loads an
emitter and requires a new one to declare which of the three it does.
"""

from __future__ import annotations

__all__ = ["refusal_class", "refusals", "is_refusal"]


def refusal_class(module) -> type | None:
    """The ``EmitError`` class carried on an emitter MODULE object, or None for a
    module that carries none.

    Read off the module that will raise, and off THAT module object: two loads of
    the same ``emit.py`` produce two distinct classes, and an ``except`` against
    one does not catch an instance of the other. A module attribute that is not
    an exception class is not a refusal vocabulary and answers None, so a
    ``emit.py`` that happens to bind the name to something else cannot widen any
    caller's catch.
    """
    cls = getattr(module, "EmitError", None)
    if isinstance(cls, type) and issubclass(cls, BaseException):
        return cls
    return None


def refusals(module) -> tuple[type, ...]:
    """The ``except`` tuple for `module`'s refusals: its own ``EmitError``, or
    the empty tuple.

    ``except ()`` catches nothing, which is exactly right for a module that
    declares no refusal vocabulary: everything such a module raises is a fault.
    """
    cls = refusal_class(module)
    return (cls,) if cls is not None else ()


def is_refusal(module, error: BaseException) -> bool:
    """Whether `error` is `module`'s own refusal rather than a fault.

    For the callers that already hold the exception (a broad ``except`` they
    cannot narrow because the same block spans several tiers' modules) rather
    than the ones that can put `refusals` in the ``except`` clause itself.
    """
    cls = refusal_class(module)
    return cls is not None and isinstance(error, cls)
