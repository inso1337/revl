"""All-or-nothing federation updates from a written plan (issue #2718).

`deploy.federation_admission` already refuses, as one unit, a plan that breaks a
pinned consumer surface or necessarily crosses a non-deferrable irreversible
effect. `deploy.run_deploy` already drives the coordinated PREPARE/COMMIT/ABORT
protocol over co-located participants, and `deploy.write_commit_approval` /
`deploy.write_abort_decision` already make the federation's decision durable.
All three were reachable only from Python: there was no plan file, no command,
nothing probed health after the commit, and nothing checked a composition
against the generation it replaces.

This module is the missing surface, and it adds no protocol of its own:

  * a plan file (TOML or JSON) names each composition's new source, the consumer
    contracts the update must hold, the generation being deployed, and an
    optional per-composition health probe;
  * :func:`federation_plan_admission` compiles the sources, reports any
    capability the new generation reaches and the one it replaces does not
    (and names every composition with no `prior`, whose widening is not
    compared), and hands the whole thing to `deploy.federation_admission`
    UNCHANGED: the refusal is the landed rule's, not a second copy;
  * :func:`apply_federation_plan` drives `deploy.run_deploy` over one local
    participant per composition (the co-located case the issue defines), then
    runs the declared probes AFTER the commit. It keeps the decision file the
    way `revl deploy` does (issue #2725): a `federation-decision-pending`
    record before the first participant is launched, the abort decision
    before any abort message, and the `federation-commit-approved` record only
    once every participant committed and every declared probe passed. A
    failed, timed-out or interrupted probe writes the abort decision FIRST and
    then asks every committed participant to run its own local LIFO unwind
    back to the prior generation, the same settle rule
    `deploy.settle_stranded` applies to a stranded participant, never a
    conductor-side inverse.

The generation is a real directory boundary, not a label: participant state
lives in one directory per generation, which an apply locks for its whole run
and uses once. A directory that already holds a decision record is refused
before anything is written, so a new update never writes into an earlier
generation's WAL or world files, and a failed update unwinds only its own
generation. Two-phase commit across hosts,
with partial failure and coordinator crash, stays out (`docs/deploy.md` §4), so
every participant here is the conductor's own child on one kernel.

A plan is unauthenticated operator input, like a deploy map: an unknown key, a
missing source, an unreadable consumer surface and a composition name that
cannot be a file name are all REFUSED rather than ignored or downgraded.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

from . import deploy
from .errors import RevlError

#: Why a plan is refused before any participant moves.
REFUSE_PLAN = "federation-plan"

#: A capability the new generation reaches and the generation it replaces does
#: not, which the plan did not name in `allow_capabilities`.
REFUSE_CAPABILITY_WIDENING = "capability-widening"

#: A plan's top-level keys. Anything else is a misspelling the author believes
#: configured something, so it is refused rather than ignored (same rule the
#: deploy map applies to its own tables).
PLAN_KEYS = ("federation", "generation", "compositions", "contracts")

#: One `[compositions.<id>]` table.
COMPOSITION_KEYS = ("source", "prior", "health_probe", "allow_capabilities")

#: One `[[contracts]]` entry.
CONTRACT_KEYS = ("surface", "provider")

#: The file an apply locks, exclusively, in its generation directory for its
#: whole run. `deploy.settle_stranded` refuses to settle while it is held.
LOCK_FILE = deploy.STATE_LOCK_FILE


# ---------------------------------------------------------------------------
# reading and validating the written plan
# ---------------------------------------------------------------------------


def _refuse(where: str, reason: str) -> ValueError:
    return ValueError(f"{where}: {reason}")


def _string(value: Any, where: str, *, required: bool = True) -> Optional[str]:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise _refuse(where, f"must be a non-empty string (got {value!r})")
    return value


def _string_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not value:
        raise _refuse(where, f"must be a non-empty list of strings (got {value!r})")
    out = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise _refuse(f"{where}[{index}]",
                          f"must be a non-empty string (got {item!r})")
        out.append(item)
    return out


def _relative_path(value: Any, where: str, base: Path) -> Path:
    """Resolve a plan path against the plan's own directory.

    A plan is a document about a set of sources; a relative path in it must mean
    the path beside the plan, never the caller's working directory, or the same
    plan would name different sources from different shells.
    """
    text = _string(value, where)
    candidate = Path(text)
    return candidate if candidate.is_absolute() else base / candidate


def _unknown_keys(table: Mapping, allowed: Sequence[str], where: str) -> None:
    unknown = sorted(set(table) - set(allowed))
    if unknown:
        raise _refuse(
            where,
            f"unrecognized key(s) {', '.join(repr(k) for k in unknown)}; "
            f"known keys are {', '.join(allowed)}. An ignored key is an author "
            "believing they configured something they did not.")


def _load_document(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        document = json.loads(text)
    else:
        import tomllib  # noqa: PLC0415 (stdlib, py3.11+)

        document = tomllib.loads(text)
    if not isinstance(document, dict):
        raise _refuse(str(path), "the plan must be a table/object")
    return document


def load_federation_plan(path: str | os.PathLike) -> dict:
    """Read and validate a federation plan, resolving its paths against the
    plan's own directory.

    Returns a normalized plan::

        {
          "federation": str,          # the update's identity
          "generation": int,          # the generation being deployed
          "path": str, "base": str,
          "compositions": {
            "<id>": {"source": Path, "prior": Path | None,
                     "health_probe": [argv...],
                     "allow_capabilities": [label...]},
          },
          "contracts": [{"surface": Path, "provider": str}],
        }

    Raises :class:`ValueError` with the offending table/entry named. Nothing is
    read beyond the plan file itself here; sources are read by
    :func:`federation_plan_admission`, so a shape error is reported before any
    compile work starts.
    """
    plan_path = Path(path)
    raw = _load_document(plan_path)
    _unknown_keys(raw, PLAN_KEYS, str(plan_path))

    base = plan_path.resolve().parent
    generation = raw.get("generation", 0)
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise _refuse(str(plan_path),
                      f"`generation` must be an integer (got {generation!r})")
    if generation < 0:
        raise _refuse(str(plan_path),
                      f"`generation` must be a non-negative integer (got {generation})")

    raw_compositions = raw.get("compositions")
    if not isinstance(raw_compositions, dict) or not raw_compositions:
        raise _refuse(
            str(plan_path),
            "`compositions` must be a non-empty table of "
            "[compositions.<id>] entries; an update that names no composition "
            "has nothing to admit or apply")

    compositions: dict[str, dict] = {}
    for composition_id, entry in raw_compositions.items():
        problem = deploy._process_name_problem(composition_id)
        if problem is not None:
            raise _refuse(
                f"compositions.{composition_id}",
                f"the composition id is refused: {problem}. It becomes a file "
                "name in the federation state directory, and a name that leaves "
                "it would write outside it.")
        if not isinstance(entry, dict):
            raise _refuse(f"compositions.{composition_id}",
                          "must be a table/object")
        _unknown_keys(entry, COMPOSITION_KEYS, f"compositions.{composition_id}")
        source = _relative_path(entry.get("source"),
                                f"compositions.{composition_id}.source", base)
        prior = None
        if entry.get("prior") is not None:
            prior = _relative_path(entry["prior"],
                                   f"compositions.{composition_id}.prior", base)
        probe = _string_list(entry.get("health_probe"),
                             f"compositions.{composition_id}.health_probe")
        for index, word in enumerate(probe):
            if "\0" in word:
                raise _refuse(
                    f"compositions.{composition_id}.health_probe[{index}]",
                    "may not contain a NUL byte: no program can be started "
                    "with one, so the probe could never run")
        allow = _string_list(entry.get("allow_capabilities"),
                             f"compositions.{composition_id}.allow_capabilities")
        compositions[composition_id] = {
            "source": source, "prior": prior, "health_probe": probe,
            "allow_capabilities": allow,
        }

    raw_contracts = raw.get("contracts") or []
    if not isinstance(raw_contracts, list):
        raise _refuse(str(plan_path),
                      "`contracts` must be a list of [[contracts]] entries")
    contracts = []
    for index, contract in enumerate(raw_contracts):
        where = f"contracts[{index}]"
        if not isinstance(contract, dict):
            raise _refuse(where, "must be a table/object")
        _unknown_keys(contract, CONTRACT_KEYS, where)
        contracts.append({
            "surface": _relative_path(contract.get("surface"),
                                      f"{where}.surface", base),
            "provider": _string(contract.get("provider"),
                                f"{where}.provider"),
        })

    return {
        "federation": _string(raw.get("federation", plan_path.stem),
                              "`federation`"),
        "generation": generation,
        "path": str(plan_path),
        "base": str(base),
        "compositions": compositions,
        "contracts": contracts,
    }


# ---------------------------------------------------------------------------
# admission: compile, contract, widening, and the landed federation rule
# ---------------------------------------------------------------------------


def _compile(path: Path, where: str) -> dict:
    from .compiler import compile_source  # noqa: PLC0415 (CLI path only)

    try:
        source = path.read_text(encoding="utf-8")
    except OSError as error:
        raise _refuse(where, f"cannot read the composition source {str(path)!r}: "
                             f"{error}") from error
    return compile_source(source, str(path))


def _compile_plan(plan: Mapping) -> tuple[dict, list[dict]]:
    """Compile every composition's new (and prior) source.

    Returns `(plans, refusals)`. A source that does not compile is a refusal
    entry, not an exception: a plan is a batch, and an author who mistyped one
    composition should see every refusal the plan can produce in one run.
    """
    plans: dict[str, dict] = {}
    refusals: list[dict] = []
    for composition_id in sorted(plan["compositions"]):
        entry = plan["compositions"][composition_id]
        try:
            plans[composition_id] = _compile(
                entry["source"], f"compositions.{composition_id}.source")
        except (ValueError, RevlError) as error:
            refusals.append({
                "kind": REFUSE_PLAN, "composition": composition_id,
                "reason": f"`{composition_id}` did not compile, so the update "
                          f"cannot be admitted: {error}",
            })
    return plans, refusals


def _contract_documents(plan: Mapping) -> tuple[list[tuple], list[dict]]:
    """Read every pinned consumer surface the plan says the update must hold."""
    contracts: list[tuple] = []
    refusals: list[dict] = []
    for index, contract in enumerate(plan["contracts"]):
        provider = contract["provider"]
        surface_path = contract["surface"]
        try:
            document = json.loads(surface_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
            refusals.append({
                "kind": REFUSE_PLAN, "composition": provider,
                "reason": (f"contracts[{index}] pins `{provider}` against "
                           f"{str(surface_path)!r}, which cannot be read as a "
                           f"consumer-surface artifact: {error}"),
            })
            continue
        if not isinstance(document, dict):
            refusals.append({
                "kind": REFUSE_PLAN, "composition": provider,
                "reason": (f"contracts[{index}] pins `{provider}` against "
                           f"{str(surface_path)!r}, which is not a JSON object"),
            })
            continue
        contracts.append((document, provider))
    return contracts, refusals


def _capability_widening(plan: Mapping, plans: Mapping[str, dict]
                         ) -> tuple[list[dict], list[str], list[dict]]:
    """Compare each new generation against the one it replaces.

    Returns `(widened, not_compared, refusals)`. `widened` reports every
    capability label the new generation reaches and the prior one does not,
    whether or not the plan allows it: a refusal the author can act on has to
    name what widened, and an allowed widening is still worth printing.
    `not_compared` names every composition whose plan entry has no `prior`, so
    a widening there is reported as not checked rather than passing silently.
    """
    widened: list[dict] = []
    not_compared: list[str] = []
    refusals: list[dict] = []
    for composition_id in sorted(plan["compositions"]):
        entry = plan["compositions"][composition_id]
        prior_path = entry.get("prior")
        if composition_id not in plans:
            continue
        if prior_path is None:
            not_compared.append(composition_id)
            continue
        try:
            prior_ir = _compile(prior_path,
                                f"compositions.{composition_id}.prior")
        except (ValueError, RevlError) as error:
            refusals.append({
                "kind": REFUSE_PLAN, "composition": composition_id,
                "reason": (f"`{composition_id}` names the generation it "
                           f"replaces in {str(prior_path)!r}, which did not "
                           f"compile, so authority widening cannot be "
                           f"compared: {error}"),
            })
            continue
        try:
            new_caps, _ = deploy.capability_surface(plans[composition_id])
            old_caps, _ = deploy.capability_surface(prior_ir)
        except RevlError as error:
            refusals.append({
                "kind": REFUSE_CAPABILITY_WIDENING,
                "composition": composition_id,
                "reason": (f"`{composition_id}`'s capability surface cannot be "
                           f"derived, so widening cannot be ruled out: {error}"),
            })
            continue
        added = sorted(new_caps - old_caps)
        if not added:
            continue
        allowed = sorted(set(added) & set(entry["allow_capabilities"]))
        unreviewed = sorted(set(added) - set(allowed))
        widened.append({"composition": composition_id,
                        "capabilities": added, "allowed": allowed})
        if unreviewed:
            refusals.append({
                "kind": REFUSE_CAPABILITY_WIDENING,
                "composition": composition_id,
                "capabilities": unreviewed,
                "allowed": allowed,
                "reason": (
                    f"`{composition_id}` reaches capabilities in its new "
                    f"generation that the generation it replaces does not: "
                    f"{', '.join(unreviewed)}. A federation update may not "
                    "silently widen authority; name each one in this "
                    "composition's `allow_capabilities` to admit the widening "
                    "deliberately."),
            })
    return widened, not_compared, refusals


def _admit_plan(plan: Mapping) -> dict:
    plans, refusals = _compile_plan(plan)
    contracts, contract_refusals = _contract_documents(plan)
    refusals.extend(contract_refusals)
    widened, not_compared, widening_refusals = _capability_widening(plan, plans)
    refusals.extend(widening_refusals)

    admitted: dict = {"admitted": False, "refusals": [], "deferred": []}
    if plans:
        try:
            admitted = deploy.federation_admission(
                plans, contracts=contracts)
        except (ValueError, KeyError, TypeError) as error:
            # A surface document of the wrong kind is an operator input error,
            # not a crash of the admission rule; report it as a plan refusal so
            # the command exits non-zero with it named.
            refusals.append({
                "kind": REFUSE_PLAN, "composition": None,
                "reason": f"the federation admission could not read its plan "
                          f"input: {error}",
            })
    elif not refusals:
        refusals.append({
            "kind": REFUSE_PLAN, "composition": None,
            "reason": "the plan names no compilable composition",
        })

    refusals.extend(admitted.get("refusals") or [])
    return {
        "admitted": not refusals,
        "refusals": refusals,
        "deferred": admitted.get("deferred") or [],
        "widened": widened,
        "notCompared": not_compared,
        "compositions": sorted(plans),
        "plans": plans,
    }


def federation_plan_admission(plan: Mapping) -> dict:
    """Admit, or REFUSE, a whole federation plan without moving anything.

    The contract and irreversible-crossing verdicts are `deploy.federation_admission`'s
    own; on top of them this reports the authority comparison the landed
    admission did not make: every capability label the new generation reaches
    and the generation it replaces does not. A widened set is refused unless the
    plan names it in `allow_capabilities`.

    Returns::

        {"admitted": bool,
         "refusals": [{kind, composition, ...reason}],
         "deferred": [...],          # the class-(b) crossings PREPARE will hold
         "widened": [{composition, capabilities, allowed}],
         "notCompared": [id, ...],   # no `prior` named: widening not compared
         "compositions": [id, ...]}

    Nothing is spawned, compiled artifacts are not written, and no participant
    state directory is touched.
    """
    return {key: value for key, value in _admit_plan(plan).items()
            if key != "plans"}


# ---------------------------------------------------------------------------
# apply: PREPARE / COMMIT / health probe / restore the prior generation
# ---------------------------------------------------------------------------


def _stop_group(process) -> None:
    """Kill the probe's whole process group, so nothing it started outlives it.

    The probe is started as the leader of its own session, so its group id is
    its pid. A group that is already empty is not an error.
    """
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return
    try:  # pragma: no cover - no process groups to kill there
        process.kill()
    except OSError:  # pragma: no cover
        pass


def _run_probe(argv: Sequence[str], cwd: str, timeout: float) -> dict:
    """Run one probe in its own process group and judge it by its exit code.

    The probe's own exit is what is waited for. Its output goes to unnamed
    temporary files rather than pipes, so a child it leaves behind holding
    them cannot turn a finished probe into a timeout; the group is killed on
    every way out, so that child does not keep running either.

    What the probe printed is counted, never copied: the report goes to a
    terminal, a `--json` pipeline or a log, and a probe's output may carry a
    credential the operator never meant to publish.
    """
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            process = subprocess.Popen(  # noqa: S603 (argv list, no shell)
                list(argv), cwd=cwd, stdin=subprocess.DEVNULL,
                stdout=out, stderr=err, start_new_session=True)
        except FileNotFoundError as error:
            return {"ok": False, "exit": None,
                    "reason": f"the probe could not be started: {error}"}
        except OSError as error:
            return {"ok": False, "exit": None,
                    "reason": f"the probe could not be run: {error}"}
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            # On every way out (exit, timeout, an interrupt) the whole group
            # goes, so nothing the probe started keeps running.
            _stop_group(process)
            process.wait()
        size = os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size
    if timed_out:
        return {"ok": False, "exit": None,
                "reason": (f"the probe did not finish within {timeout:g}s; "
                           "its process group was killed")}
    code = process.returncode
    if code == 0:
        return {"ok": True, "exit": code}
    return {"ok": False, "exit": code,
            "reason": (f"the probe exited {code}"
                       + (f"; its {size} byte(s) of output are not copied "
                          "into this report, run the probe by hand to read "
                          "them" if size else ""))}


def run_health_probes(plan: Mapping, *, timeout: float = 30.0) -> list[dict]:
    """Run every probe the plan declares, after the commit.

    A probe is an argv list run in the plan's own directory, in a process
    group of its own: exit 0 is healthy and anything else is a failed probe. A
    probe that runs past `timeout` is failed and its whole group is killed. It
    is deliberately not a shell string; a plan is unauthenticated operator
    input, so it may name a program and its arguments but not splice a command
    language into the conductor.

    A composition that declares no probe reports `declared: false` and does not
    fail the update: the probe is an invitation to verify, not a requirement
    this slice can invent for an author who did not write one.

    Anything this function raises (an interrupt while a probe hangs, say)
    propagates; :func:`apply_federation_plan` then aborts the update.
    """
    rows: list[dict] = []
    base = plan.get("base") or "."
    for composition_id in sorted(plan["compositions"]):
        argv = plan["compositions"][composition_id]["health_probe"]
        if not argv:
            rows.append({"composition": composition_id, "declared": False,
                         "ok": True, "reason": "no health probe declared"})
            continue
        row = {"composition": composition_id, "declared": True,
               "argv": list(argv)}
        row.update(_run_probe(argv, base, timeout))
        rows.append(row)
    return rows


def _abort_committed(participants: Sequence, report: Mapping) -> dict:
    """Ask every committed participant to unwind its own slice, newest-first.

    This is the settle rule's roll-BACK branch performed by the process that
    owns the inverses: the conductor holds only the commit ledger, exactly as in
    `run_deploy`'s own abort path. A participant that cannot be reached is
    reported `unresolved` with `revl recover` named, never as restored.

    An interrupt (Ctrl-C, `SystemExit`) while one participant unwinds is
    recorded like any other failure and the remaining participants are still
    asked to unwind; the interrupt is re-raised once every one was asked.
    """
    by_identity = {participant.identity: participant for participant in participants}
    interrupted: Optional[BaseException] = None
    ledger = list(report.get("commitLedger") or [])
    outcomes: dict[str, dict] = {}
    unresolved: list[dict] = []
    residue: list[dict] = []
    for entry in reversed(ledger):
        identity = entry["identity"]
        participant = by_identity.get(identity)
        if participant is None:
            unresolved.append({"identity": identity,
                               "reason": "the conductor no longer holds it"})
            outcomes[identity] = {
                "outcome": deploy.UNRESOLVED,
                "reason": "the conductor no longer holds it",
                "settleWith": "run `revl recover` on that participant's WAL",
            }
            continue
        try:
            settled = participant.abort()
        except BaseException as error:  # noqa: BLE001 (any failure is unresolved)
            reason = f"{type(error).__name__}: {error}"
            unresolved.append({"identity": identity, "reason": reason})
            outcomes[identity] = {
                "outcome": deploy.UNRESOLVED, "reason": reason,
                "settleWith": "run `revl recover` on that participant's WAL",
            }
            if not isinstance(error, Exception) and interrupted is None:
                interrupted = error
            continue
        clean = bool((settled.get("residue") or {}).get("clean"))
        outcomes[identity] = {
            "outcome": (deploy.ROLLED_BACK_CLEAN if clean
                        else deploy.ROLLED_BACK_WITH_RESIDUE),
            "residue": settled.get("residue"),
            "unwoundBy": settled.get("pid"),
        }
        if not clean:
            residue.append({"identity": identity,
                            "outstanding": (settled.get("residue") or {})
                            .get("outstanding")})

    if interrupted is not None:
        raise interrupted
    for participant in participants:
        outcomes.setdefault(participant.identity,
                            {"outcome": deploy.NEVER_COMMITTED})
    clean = not unresolved and not residue
    return {
        "verdict": (deploy.DEPLOY_ABORTED_CLEAN if clean
                    else deploy.DEPLOY_ABORTED_WITH_RESIDUE),
        "abortOrder": [entry["identity"] for entry in reversed(ledger)],
        "participants": outcomes,
        "residue": {
            "clean": clean,
            "outstanding": unresolved + residue,
            "proof": ("every committed participant ran its own local LIFO unwind "
                      "back to the prior generation and proved its own no-residue"
                      if clean else
                      "at least one committed participant could not prove it "
                      "returned to the prior generation; the residue is named"),
        },
    }


@contextmanager
def _generation_lock(state: Path) -> Iterator[bool]:
    """Hold `<state>/lock` exclusively for the body, or yield False at once.

    It does not wait: a second apply on one generation directory is an
    operator error to report, and once the first finishes the directory holds
    a decision record, which refuses the second anyway. The lock covers one
    generation directory, not the federation: two applies of different
    generations of one federation are not serialized by it.
    """
    fd = os.open(str(state / LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        held = deploy._try_lock(fd)
        try:
            yield held
        finally:
            if held:
                deploy._unlock(fd)
    finally:
        os.close(fd)


def _refused_before_launch(phase: str, reason: str) -> dict:
    return {
        "verdict": deploy.DEPLOY_REFUSED,
        "phase": phase,
        "reason": reason,
        "participants": {},
        "proof": ("refused before any record was written or any participant "
                  "was launched: no composition moved from its prior "
                  "generation."),
    }


def apply_federation_plan(plan: Mapping, *, state_dir: str | os.PathLike,
                          probe_timeout: float = 30.0,
                          on_event=None) -> dict:
    """Admit a plan and, if it admits, apply it as one co-located update.

    The order is the issue's and the protocol's:

      1. admission (:func:`federation_plan_admission`): a refusal here opens no
         boundary and creates no directory, so every composition is still on
         its prior generation;
      2. `state_dir` is locked (`<state_dir>/lock`) for the rest of the run, and
         refused if another apply holds it or if it already holds a decision
         record: a generation directory is used once;
      3. a `federation-decision-pending` record is written before the first
         participant is launched (issue #2725, as `revl deploy` does), then one
         local participant per composition is handed to `deploy.run_deploy`. A
         COMMIT failure writes the abort decision before any abort message and
         rolls back through the landed protocol;
      4. the declared health probes, AFTER the commit, while the pending record
         is still the last one;
      5. every probe passed: the `federation-commit-approved` record. A probe
         failed, timed out or raised (an interrupt included): the durable
         abort decision is written FIRST, then every committed participant
         unwinds itself back to the prior generation; an exception is
         re-raised after that.

    A conductor killed at any point before step 5 leaves the pending record or
    the abort decision last, so `deploy.settle_stranded` never reads an
    approval for this run and settles every stranded participant back.

    `state_dir` is the exact directory for THIS generation's participant state
    (the CLI derives `<wal-dir>/federation/<federation>/<generation>`).
    Keeping the generation in the path is what makes "restore the prior
    generation" an observable fact rather than a claim.
    """
    admitted = _admit_plan(plan)
    report: dict = {
        "protocol": deploy.PROTOCOL,
        "federation": plan["federation"],
        "generation": plan["generation"],
    }
    if not admitted["admitted"]:
        report.update({
            "verdict": deploy.DEPLOY_REFUSED,
            "phase": "admit",
            "refusals": admitted["refusals"],
            "deferred": admitted["deferred"],
            "widened": admitted["widened"],
            "notCompared": admitted["notCompared"],
            "participants": {},
            "proof": ("admission refused the whole update before a boundary was "
                      "opened: no composition moved from its prior generation."),
        })
        return report

    state = Path(state_dir)
    state.mkdir(parents=True, exist_ok=True)
    with _generation_lock(state) as held:
        if not held:
            report.update(_refused_before_launch("lock", (
                f"another apply holds {state / LOCK_FILE}; two applies on one "
                "generation directory would interleave its decision file and "
                "world files")))
            return report
        decision_path = state / deploy.DECISION_FILE
        # `lexists`, not `exists`: a dangling symlink in the decision file's
        # place would otherwise read as fresh and be written through.
        if os.path.lexists(decision_path):
            report.update(_refused_before_launch("generation", (
                f"{decision_path} already holds a decision record, so "
                f"{state} was already used by an apply. A generation "
                "directory is used once, or a new update would write into an "
                "earlier generation's WAL and world files: give the plan a new "
                "`generation`, or a new state directory")))
            return report
        report.update(_apply_locked(plan, admitted, state, str(decision_path),
                                    probe_timeout=probe_timeout,
                                    on_event=on_event))
        return report


def _apply_locked(plan: Mapping, admitted: Mapping, state: Path,
                  decision_path: str, *, probe_timeout: float,
                  on_event) -> dict:
    federation, generation = plan["federation"], plan["generation"]
    plans = admitted["plans"]
    # Issue #2725, as `revl deploy` applies it: from here on this run owns the
    # decision file's last record, so a conductor killed before the approval
    # reads as "this run decided nothing" (roll back).
    deploy.write_decision_started(decision_path, federation_id=federation,
                                  generation=generation)

    def event(phase: str, identity: str, detail: dict) -> None:
        if phase == "commit" and not detail.get("ok"):
            # `run_deploy` aborts from this report on. Its approval is not
            # handed the decision path (the approval waits for the probes), so
            # the abort decision is made durable here, before `run_deploy`
            # sends any ABORT message, which is the order it keeps itself.
            deploy.write_abort_decision(
                decision_path, federation_id=federation, generation=generation,
                reason=detail.get("reason"), failed=identity)
        if on_event is not None:
            on_event(phase, identity, detail)

    participants: list = []
    try:
        for composition_id in sorted(plans):
            components = [component.get("name")
                          for component in plans[composition_id].get("components")
                          or [] if component.get("name")]
            spec_path = deploy._local_participant_spec(
                composition_id, components, state, generation=generation)
            target = deploy.DeployTarget(process=composition_id,
                                         via=deploy.VIA_LOCAL, raw={})
            participant, error = deploy.launch_local_participant(
                target, spec_path=spec_path)
            if error is not None:
                return {
                    "verdict": deploy.DEPLOY_REFUSED,
                    "phase": "launch",
                    "refusedBy": composition_id,
                    "reason": error,
                    "participants": {},
                    "proof": ("a participant that could not be spawned is a "
                              "PREPARE-time refusal: nothing committed, so every "
                              "composition is still on its prior generation."),
                }
            participants.append(participant)

        report = deploy.run_deploy(
            participants, approval_path=None, federation_id=federation,
            generation=generation, on_event=event)
        report.update({"federation": federation, "generation": generation,
                       "widened": admitted["widened"],
                       "notCompared": admitted["notCompared"],
                       "deferred": admitted["deferred"]})
        if report.get("verdict") != deploy.DEPLOY_APPLIED:
            return report

        try:
            probes = run_health_probes(plan, timeout=probe_timeout)
        except BaseException as error:
            # Ctrl-C on a hung probe, or a probe the OS could not be handed:
            # the update's health is unknown, so it is not kept. Decision
            # first, then the unwind, then the error goes on up.
            deploy.write_abort_decision(
                decision_path, federation_id=federation, generation=generation,
                reason=(f"the health probes did not finish: "
                        f"{type(error).__name__}: {error}"),
                failed=None)
            _abort_committed(participants, report)
            raise
        report["probes"] = probes
        failed = [probe for probe in probes if not probe.get("ok")]
        if not failed:
            deploy.write_commit_approval(
                decision_path, federation_id=federation, generation=generation,
                participants=[p.identity for p in participants])
            return report

        # Durable decision FIRST, exactly as the protocol's abort path does, so a
        # participant stranded from here on settles against a record.
        deploy.write_abort_decision(
            decision_path, federation_id=federation, generation=generation,
            reason=("health probe failed for `" + failed[0]["composition"] + "`: "
                    + str(failed[0].get("reason") or "the probe failed")),
            failed=failed[0]["composition"])
        report.update(_abort_committed(participants, report))
        report["phase"] = "health"
        report["failedProbe"] = failed[0]
        return report
    finally:
        for participant in participants:
            participant.stop()


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------


def _render_admission(plan_path: str, verdict: Mapping) -> list[str]:
    lines = []
    if verdict.get("admitted"):
        lines.append(f"federation plan {plan_path}: ADMITTED, "
                     f"{len(verdict.get('compositions') or [])} composition(s)")
        for row in verdict.get("widened") or ():
            allowed = row.get("allowed") or ()
            lines.append(
                f"  {row['composition']} reaches newly widened capability "
                f"{', '.join(row['capabilities'])}"
                + (f" (allowed: {', '.join(allowed)})" if allowed else ""))
        for row in verdict.get("deferred") or ():
            lines.append(f"  PREPARE will hold `{row['extern']}` in "
                         f"{row['composition']}/{row['component']} "
                         "(deferrable crossing)")
    else:
        lines.append(f"federation plan {plan_path}: REFUSED")
        for refusal in verdict.get("refusals") or ():
            where = refusal.get("composition") or "-"
            lines.append(f"  REFUSED [{refusal.get('kind')}] {where}: "
                         f"{refusal.get('reason')}")
    lines.extend(_not_compared_lines(verdict))
    return lines


def _not_compared_lines(verdict: Mapping) -> list[str]:
    return [f"  {composition_id}: capability widening not compared (the plan "
            "names no `prior`)"
            for composition_id in verdict.get("notCompared") or ()]


def _render_report(plan_path: str, report: Mapping) -> list[str]:
    lines = [f"federation plan {plan_path}: {report.get('verdict')} "
             f"({report.get('protocol')})"]
    if report.get("reason"):
        lines.append(f"  {report['reason']}")
    if report.get("refusedBy"):
        lines.append(f"  refused by: {report['refusedBy']}")
    for refusal in report.get("refusals") or ():
        lines.append(f"  REFUSED [{refusal.get('kind')}] "
                     f"{refusal.get('composition') or '-'}: "
                     f"{refusal.get('reason')}")
    for row in report.get("widened") or ():
        lines.append(f"  widened capabilities: {row['composition']}: "
                     f"{', '.join(row['capabilities'])}")
    lines.extend(_not_compared_lines(report))
    participants = report.get("participants") or {}
    for identity in sorted(participants):
        lines.append(f"  participant {identity}: "
                     f"{participants[identity].get('outcome')}")
    ledger = report.get("commitLedger") or []
    if ledger:
        lines.append("  commit ledger: "
                     + " -> ".join(f"{entry['identity']}(#{entry['order']})"
                                   for entry in ledger))
    for probe in report.get("probes") or ():
        if probe.get("declared"):
            state = "ok" if probe.get("ok") else "FAILED"
            lines.append(f"  health probe {probe['composition']}: {state}"
                         + (f" ({probe.get('reason')})"
                            if probe.get("reason") and not probe.get("ok") else ""))
        else:
            lines.append(f"  health probe {probe['composition']}: not declared")
    failed = report.get("failedProbe")
    if failed:
        lines.append(f"  failed probe: {failed['composition']}")
    if report.get("abortOrder"):
        lines.append("  restore order (reverse commit): "
                     + " -> ".join(report["abortOrder"]))
    residue = report.get("residue") or {}
    if residue:
        lines.append("  prior generation restored: "
                     + ("yes" if residue.get("clean") else "NO"))
        for item in residue.get("outstanding") or ():
            lines.append(f"    outstanding: {item}")
    return lines


def federation_apply_command(args) -> int:
    """`revl federation-apply PLAN [--dry-run] [--json] [--state-dir DIR]`.

    Without `--dry-run`, admit the plan and apply it as one co-located update;
    with it, run only the effect-free admission half and print the verdict.
    Either way a refusal exits non-zero and names the rule, the composition and
    the reason: a command that refused silently would be a command an operator
    pipelines past.
    """
    try:
        plan = load_federation_plan(args.plan)
    except (OSError, ValueError) as error:
        print(f"error: cannot read federation plan {args.plan!r}: {error}",
              file=sys.stderr)
        return 1

    as_json = bool(getattr(args, "json", False))
    if getattr(args, "dry_run", False):
        verdict = federation_plan_admission(plan)
        if as_json:
            print(json.dumps({"plan": args.plan, **verdict}, indent=2))
        else:
            for line in _render_admission(args.plan, verdict):
                print(line)
        return 0 if verdict.get("admitted") else 1

    state_dir = getattr(args, "state_dir", None)
    if not state_dir:
        from . import wal  # noqa: PLC0415 (only the apply path needs it)

        # Keyed on the update's identity, not the plan file's name: two plans
        # called `plan.toml` in different directories are different updates.
        problem = deploy._process_name_problem(plan["federation"])
        if problem is not None:
            print(f"error: federation plan {args.plan!r}: the federation name "
                  f"{plan['federation']!r} cannot name the default state "
                  f"directory ({problem}); pass --state-dir", file=sys.stderr)
            return 1
        state_dir = (Path(wal.default_wal_dir()) / "federation"
                     / plan["federation"] / str(plan["generation"]))
    report = apply_federation_plan(
        plan, state_dir=state_dir,
        probe_timeout=float(getattr(args, "probe_timeout", 30.0)))
    if as_json:
        print(json.dumps({"plan": args.plan, **report}, indent=2))
    else:
        for line in _render_report(args.plan, report):
            print(line)
    return 0 if report.get("verdict") == deploy.DEPLOY_APPLIED else 1

