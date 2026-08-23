"""Drive an adapter over the catalog and score each case with the pin discipline.

The scoring rules (per case, for an adapter named ``R``) are the heart of the
kit — they are what make the report change only *deliberately*:

* Compile-time case                -> **pending** (a runtime cannot exercise it).
* Adapter says unsupported         -> **pending** (never green for "unchecked").
* A pinned divergence owns ``R``:
    - observation meets the ideal   -> **fail** (the pin went stale; re-baseline
                                       deliberately — a case that starts passing
                                       against a live pin fails the kit).
    - observation matches the pin   -> **divergence** (expected, recorded).
    - neither                       -> **fail** (unknown behavior).
* No pin owns ``R``:
    - observation meets the ideal   -> **pass**.
    - observation matches some pin
      owned by another runtime       -> **fail** (unexpected divergence: this
                                       runtime behaves like a *different*
                                       runtime's known bug, which is a finding).
    - neither                       -> **fail** (does not meet the requirement).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .adapter import Observation, RuntimeAdapter
from .spec import CATALOG, Case, Divergence, Kind


class Outcome(str, Enum):
    PASS = "pass"
    DIVERGENCE = "divergence"   # a pinned, expected divergence
    PENDING = "pending"         # not exercised — never conflated with pass
    FAIL = "fail"


# outcomes that do not sink the run: pass, an expected pinned divergence, and
# pending (which is honest about not having checked).
_OK = {Outcome.PASS, Outcome.DIVERGENCE, Outcome.PENDING}


@dataclass
class CaseResult:
    case: Case
    outcome: Outcome
    detail: str
    divergence: Optional[Divergence] = None
    observation: Optional[Observation] = None


@dataclass
class Report:
    adapter_name: str
    runtime_version: str
    results: list[CaseResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing failed. Pending and pinned divergences are OK."""
        return all(r.outcome in _OK for r in self.results)

    def by_requirement(self) -> dict[str, list[CaseResult]]:
        out: dict[str, list[CaseResult]] = {}
        for r in self.results:
            out.setdefault(r.case.requirement, []).append(r)
        return out

    def counts(self) -> dict[str, int]:
        c = {o.value: 0 for o in Outcome}
        for r in self.results:
            c[r.outcome.value] += 1
        return c


def _pin_for(case: Case, adapter_name: str) -> Optional[Divergence]:
    for d in case.divergences:
        if adapter_name in d.runtimes:
            return d
    return None


def _score(case: Case, adapter: RuntimeAdapter, obs: Observation) -> CaseResult:
    if not obs.supported:
        reason = obs.notes or "adapter does not exercise this case"
        return CaseResult(case, Outcome.PENDING, f"not exercised: {reason}",
                          observation=obs)

    assert case.ideal is not None  # runtime cases always carry an oracle
    ideal_ok, ideal_detail = case.ideal(obs)
    pin = _pin_for(case, adapter.name)

    if pin is not None:
        if ideal_ok:
            return CaseResult(
                case, Outcome.FAIL,
                f"pinned divergence '{pin.label}' now meets the ideal — the pin "
                f"is stale; re-baseline deliberately. ({ideal_detail})",
                divergence=pin, observation=obs)
        if pin.matches(obs):
            return CaseResult(
                case, Outcome.DIVERGENCE,
                f"matches pinned divergence '{pin.label}'",
                divergence=pin, observation=obs)
        return CaseResult(
            case, Outcome.FAIL,
            f"neither the ideal nor the pinned divergence '{pin.label}': "
            f"{ideal_detail}",
            divergence=pin, observation=obs)

    if ideal_ok:
        return CaseResult(case, Outcome.PASS, ideal_detail, observation=obs)

    # not pinned for this runtime, and not ideal — is it someone else's pin?
    for d in case.divergences:
        if d.matches(obs):
            return CaseResult(
                case, Outcome.FAIL,
                f"unexpected divergence: behaves like '{d.label}' "
                f"(pinned for {sorted(d.runtimes)}, not {adapter.name!r})",
                divergence=d, observation=obs)
    return CaseResult(case, Outcome.FAIL, ideal_detail, observation=obs)


def run_suite(adapter: RuntimeAdapter, cases: tuple[Case, ...] = CATALOG) -> Report:
    report = Report(adapter.name, adapter.runtime_version)
    for case in cases:
        if case.kind == Kind.COMPILE_TIME:
            report.results.append(CaseResult(
                case, Outcome.PENDING,
                f"compile-time requirement — not exercised by a runtime "
                f"adapter. Enforced by: {case.enforced_by}"))
            continue
        if not adapter.supports(case.id):
            report.results.append(CaseResult(
                case, Outcome.PENDING,
                "adapter reports it cannot exercise this case"))
            continue
        try:
            obs = adapter.run(case.id)
        except Exception as exc:  # a crashing scenario is a failure, not a skip
            report.results.append(CaseResult(
                case, Outcome.FAIL, f"adapter raised: {exc!r}"))
            continue
        report.results.append(_score(case, adapter, obs))
    return report
