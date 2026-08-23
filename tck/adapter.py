"""The adapter interface a candidate runtime implements for the TCK to drive it.

The TCK is a *runtime* compatibility kit, not an emitter test. It defines a set
of named scenarios (see ``spec.py``) and, for each, an oracle: the observation a
conforming runtime must produce. A candidate runtime supplies a
:class:`RuntimeAdapter` that runs each scenario against the real runtime and
returns a normalized :class:`Observation`. The runner (``runner.py``) compares
that observation to the oracle and writes a conformance report.

This mirrors the stc-go pattern — a hand-written scenario reference is "the
executable oracle the emitter targets" — generalized so any runtime can be
scored by implementing the same scenarios and reporting the same observations.

An adapter implements exactly three things:

* ``name`` / ``runtime_version`` — identity. ``name`` is load-bearing: it is
  how a known divergence is attributed to a runtime family (see ``spec.py``'s
  pinned divergences and ``runner.py``'s pin discipline).
* ``supports(case_id) -> bool`` — whether this runtime can exercise the case at
  all. A case a runtime cannot drive is reported **pending**, never green:
  "nothing checked" must never read as "passed".
* ``run(case_id) -> Observation`` — execute the scenario and return what
  happened, normalized to the fields below so the oracle is language-agnostic.

An adapter for a non-Python runtime is free to shell out to a native driver
(the runtime's own hand-written scenario reference) and parse a canonical JSON
observation back into :class:`Observation`; :meth:`Observation.from_json` exists
for exactly that. The worked example (``adapters/py_adapter.py``) drives
cordis-py in-process because Python is the reference tier.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Observation:
    """What a runtime did when it ran one scenario — the checkable surface.

    Every field is normalized so an oracle never has to know the runtime's
    language:

    * ``trace`` — the ordered host-operation log, with instance serials
      stripped (``map#3.drop`` -> ``map.drop``). Order is significant: LIFO
      recovery (R1/G7) and boundary divert (A1) are ordering claims.
    * ``states`` — terminal lifecycle state per component label, one of
      ``"ACTIVE"`` / ``"PENDING"`` / ``"FAILED"``. A8 is a claim about which.
    * ``residue`` — host-runtime introspection after the scenario's final
      unload: ``store`` (live provisions), ``registry`` (registered plugins),
      ``hooks`` (event listeners), ``disposables`` (root-level effects). R4 is
      the claim that these return to their pre-composition baseline (0).
    * ``errors`` — count of teardown faults the host logged rather than raised.
      A clean teardown logs none.
    * ``supported`` — False signals the adapter could not exercise the case;
      the runner turns that into *pending*.
    """

    trace: list[str] = field(default_factory=list)
    states: dict[str, str] = field(default_factory=dict)
    residue: dict[str, int] = field(default_factory=dict)
    errors: int = 0
    supported: bool = True
    notes: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "trace": list(self.trace),
            "states": dict(self.states),
            "residue": dict(self.residue),
            "errors": self.errors,
            "supported": self.supported,
            "notes": self.notes,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Observation":
        return cls(
            trace=list(data.get("trace", [])),
            states=dict(data.get("states", {})),
            residue=dict(data.get("residue", {})),
            errors=int(data.get("errors", 0)),
            supported=bool(data.get("supported", True)),
            notes=str(data.get("notes", "")),
        )

    @classmethod
    def unsupported(cls, reason: str) -> "Observation":
        return cls(supported=False, notes=reason)


class RuntimeAdapter(abc.ABC):
    """A candidate runtime, as the TCK drives it. See the module docstring."""

    #: Runtime family identity, e.g. ``"cordis-py"``. Used to attribute a known
    #: pinned divergence to this runtime (``runner.py``). Two adapters for the
    #: same runtime family must share this name.
    name: str = "unknown"

    #: Informational: the exact runtime build under test.
    runtime_version: str = "unknown"

    @abc.abstractmethod
    def supports(self, case_id: str) -> bool:
        """Whether this runtime can exercise ``case_id``. False -> pending."""

    @abc.abstractmethod
    def run(self, case_id: str) -> Observation:
        """Run the scenario named ``case_id`` and return its observation.

        Must return :meth:`Observation.unsupported` (or set ``supported=False``)
        rather than raising when the case cannot be driven, so an unexercised
        requirement is reported pending instead of failing the whole run.
        """
