"""`revl recover --wal INDEX` for a placement run (issue #1477).

`revl run --placement P --wal INDEX` writes one WAL per process and an index
naming them (`revl.placement_wal`). This module recovers the whole run from the
index:

* every process WAL the index names is found next to it. A missing one is
  residue, named by its process: recover cannot tell what that process crossed.
  An empty one was created by the conductor and never opened by its process,
  which crossed nothing.
* each WAL is recovered by the same core a single-process WAL is
  (`revl.recovery.recover`). With `--composition`, each is replayed through
  the composition's binding: one emitted module, and one boot of the
  components that provide the keys the open calls go through. A compensation
  that crosses processes (an Agent in one process compensating through a
  `tickets` service a Desk in another provides) reaches the providing
  component, and the verdict names the process the placement hosted it in. A
  key no process of this placement provides is named as unreached; the
  runtime then reports the call as residue.
* processes are recovered consumers first, so a consumer's compensations run
  before its providers' own, newest first across the run as within a process.
* if the index recorded the run's commit (every process was UP) but a process
  died before stamping its own `activation-complete`, recover stamps it from
  the index first, so that process rolls forward with the others instead of
  rolling back alone.

The verdict is one report: per process, its own verdict, and one residue proof
that names each process's residue. It is clean only when every process is.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from .placement_wal import process_wal_path
from .recovery import WORLD_MODEL, WORLD_REAL, RecoveryError, recover, render


def _consumers_first(entries: list, ir: Optional[dict]) -> list:
    """The index's processes, each consumer before the processes it requires
    a key from. Without an IR (a model run) the order is the index's own,
    reversed: providers are usually declared first."""
    if ir is None:
        return list(reversed(entries))
    host = {c: e["name"] for e in entries for c in e["components"]}
    owner = {key: host.get(comp["name"]) for comp in ir.get("components") or []
             for key in comp.get("provides") or {}}
    needs = {e["name"]: set() for e in entries}
    for comp in ir.get("components") or []:
        consumer = host.get(comp["name"])
        for key in comp.get("requires") or {}:
            provider = owner.get(key)
            if consumer and provider and provider != consumer:
                needs[consumer].add(provider)
    order: list = []

    def visit(name: str, path: tuple) -> None:
        if name in order or name in path:
            return
        for other in sorted(needs[name]):
            visit(other, path + (name,))
        order.append(name)

    for entry in entries:
        visit(entry["name"], ())
    by_name = {e["name"]: e for e in entries}
    return [by_name[name] for name in reversed(order)]


def _stamp_commit(path: str, entry: dict) -> None:
    """The index recorded the run's commit; this process died before it
    stamped its own marker. Stamp it, so recover rolls it forward."""
    record = {"record": "activation-complete", "generation": 1,
              "components": entry["components"], "stampedBy": "placement-index"}
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _process_state(path: str, entry: dict, committed: bool) -> dict:
    """What is on disk for one process, completing a recorded commit."""
    from .wal import read_wal  # noqa: PLC0415

    if not os.path.exists(path):
        return {"state": "missing"}
    wal = read_wal(path)
    if not wal["header"]:
        return {"state": "never-opened"}
    stamped = committed and not wal["complete"]
    if stamped:
        _stamp_commit(path, entry)
    return {"state": "recorded", "commitStamped": stamped}


class _PlacementBinding:
    """The composition's binding, shared by every process of the run: one
    emitted module, one boot of the providers the open calls need."""

    def __init__(self, files: list, ir: dict, digest: str, config: dict) -> None:
        self.files, self.ir, self.digest, self.config = files, ir, digest, config
        self.cleanup: list = []
        self.session = None
        self.services: dict = {}
        #: process -> the required-service keys its open calls go through
        self.keys: dict = {}
        self.module = self.runtime = None

    def bind(self, recorded: list) -> dict:
        """``{process: CompositionWorld}`` for each ``(entry, path)``."""
        from .recover_binding import (boot_providers, emit_unactivated,  # noqa: PLC0415
                                      open_keys, plan_wal)
        plans = {}
        for entry, path in recorded:
            try:
                plans[entry["name"]] = plan_wal(path, self.digest, self.files)
            except RecoveryError as error:
                raise type(error)(f"process {entry['name']} ({path}): {error}") from None
        self.keys = {name: open_keys(*plan) for name, plan in plans.items()}
        self.module, self.runtime = emit_unactivated(self.ir, self.config, self.cleanup)
        self.session, _booted, self.services = boot_providers(
            self.ir, set().union(set(), *self.keys.values()), self.config)
        return {entry["name"]: self._world(path, plans[entry["name"]],
                                           self.keys[entry["name"]])
                for entry, path in recorded}

    def _world(self, path: str, plan: tuple, keys: set):
        from .recover_binding import CompositionWorld, _provider_closure  # noqa: PLC0415
        _wal, foreign, _grants, foreign_handles = plan
        return CompositionWorld(
            files=self.files, digest=self.digest, module=self.module,
            runtime=self.runtime, wal_path=path,
            services={k: v for k, v in self.services.items() if k in keys},
            booted=_provider_closure(self.ir, keys), provider_session=None,
            cleanup=[], foreign=foreign, foreign_handles=foreign_handles)

    def close(self) -> None:
        session, self.session = self.session, None
        try:
            if session is not None and session.loaded:
                session.unload()
        finally:
            while self.cleanup:
                self.cleanup.pop()()


def _reach(ir: dict, entries: list, keys: set) -> tuple:
    """``(reached, unreached)`` for the keys one process's open calls go
    through: which component of which process provides each, and which keys no
    process of this placement provides."""
    host = {c: e["name"] for e in entries for c in e["components"]}
    owner = {key: comp["name"] for comp in ir.get("components") or []
             for key in comp.get("provides") or {}}
    reached = {k: {"component": owner[k], "process": host.get(owner[k])}
               for k in sorted(keys) if k in owner}
    unreached = [k for k in sorted(keys) if k not in owner]
    return reached, unreached


def _describe(world, keys: set, ir: dict, entries: list) -> dict:
    described = world.describe()
    described["reached"], described["unreached"] = _reach(ir, entries, keys)
    return described


def _missing_residue(result: dict) -> dict:
    return {"kind": "missing-wal", "process": result["process"],
            "components": result["components"], "wal": result["wal"],
            "outcome": "not-attempted",
            "error": {"type": "missing-wal",
                      "message": f"the index names {result['wal']} for process "
                                 f"{result['process']}, and it is not there: "
                                 f"recover cannot tell what that process "
                                 f"crossed"}}


def _proof_line(result: dict) -> str:
    name = result["process"]
    if result["state"] == "missing":
        return f"process {name}: RESIDUE: its WAL {result['wal']} is missing"
    if result["state"] == "never-opened":
        return (f"process {name}: CLEAN: its WAL was never opened, so the "
                f"process crossed nothing")
    residue = result["report"].get("residue") or {}
    head = "CLEAN" if residue.get("clean") else "RESIDUE"
    return f"process {name}: {head}: {residue.get('proof', '')}"


def _aggregate(index_path: str, index: dict, results: list, world: str) -> dict:
    outstanding: list = []
    calls = 0
    for result in results:
        if result["state"] == "missing":
            outstanding.append(_missing_residue(result))
            continue
        report = result.get("report") or {}
        calls += report.get("worldCalls", 0)
        for item in (report.get("residue") or {}).get("outstanding") or []:
            tagged = dict(item) if isinstance(item, dict) else {"item": item}
            tagged["process"] = result["process"]
            outstanding.append(tagged)
    from .recovery import _guarantee  # noqa: PLC0415
    return {
        "verdict": "placement",
        "decision": (f"a placement run of {len(results)} process(es), each with "
                     f"its own WAL, recovered process by process, consumers "
                     f"first. The run "
                     + ("committed: every process was UP before the crash."
                        if index["committed"] else
                        "never committed: the crash came before every process "
                        "was UP, so each process's activation rolls back.")),
        "placement": {"index": index_path, "run": index["run"],
                      "committed": index["committed"],
                      "order": [r["process"] for r in results]},
        "processes": results,
        "world": world,
        "worldCalls": calls,
        "residue": {"clean": not outstanding, "outstanding": outstanding,
                    "proof": " | ".join(_proof_line(r) for r in results)},
        "guarantee": _guarantee(),
    }


def recover_index(index_path: str, index: dict, *,
                  composition: Optional[list] = None,
                  config: Optional[dict] = None, reissue: Optional[str] = None,
                  forward_admissions: bool = False) -> dict:
    """Recover every process WAL of the placement run ``index`` describes.
    With ``composition``, against the real world through its binding;
    without, against the in-memory model, as a single WAL is."""
    entries = index["processes"]
    ir = digest = None
    if composition:
        from .recover_binding import compile_composition  # noqa: PLC0415
        ir, digest = compile_composition(composition)
        _check_roster(entries, ir, composition)
    order = _consumers_first(entries, ir)
    results = []
    for entry in order:
        path = process_wal_path(index_path, entry)
        results.append({"process": entry["name"],
                        "components": entry["components"], "wal": path,
                        **_process_state(path, entry, index["committed"])})
    recorded = [(e, r["wal"]) for e, r in zip(order, results)
                if r["state"] == "recorded"]
    binding = (_PlacementBinding(list(composition), ir, digest, dict(config or {}))
               if composition else None)
    try:
        worlds = binding.bind(recorded) if binding else {}
        for result in results:
            if result["state"] != "recorded":
                continue
            world = worlds.get(result["process"])
            report = recover(result["wal"], world=world, reissue=reissue,
                             forward_admissions=forward_admissions)
            if world is not None:
                report["binding"] = _describe(
                    world, binding.keys[result["process"]], ir, entries)
            result["report"] = report
    finally:
        if binding is not None:
            binding.close()
    return _aggregate(index_path, index, results,
                      WORLD_REAL if composition else WORLD_MODEL)


def _check_roster(entries: list, ir: dict, composition: list) -> None:
    """Refuse a composition whose components are not the ones the index
    placed: its processes' calls would bind to the wrong code."""
    placed = sorted(c for e in entries for c in e["components"])
    have = sorted(c["name"] for c in ir.get("components") or [])
    if placed != have:
        raise RecoveryError(
            f"{', '.join(composition)} has components {', '.join(have)}; the "
            f"placement index placed {', '.join(placed)}. Recover the run with "
            f"the composition it ran.")


def render_placement(report: dict) -> str:
    """The text verdict: the run, then each process's own verdict, then one
    residue proof naming each process's residue."""
    placement = report["placement"]
    lines = [f"verdict: PLACEMENT ({len(report['processes'])} processes, "
             f"recovered in the order {', '.join(placement['order'])})",
             report["decision"], ""]
    for result in report["processes"]:
        lines.append(f"== process {result['process']} "
                     f"[{', '.join(result['components'])}]  {result['wal']}")
        lines.extend("  " + line for line in _process_body(result).splitlines())
        lines.append("")
    residue = report["residue"]
    lines.append(f"residue proof [{'CLEAN' if residue['clean'] else 'RESIDUE'}]"
                 + (" in the model:" if report["world"] == WORLD_MODEL else ":"))
    lines.extend(f"  {part}" for part in residue["proof"].split(" | "))
    return "\n".join(lines)


def _process_body(result: dict) -> str:
    if result["state"] == "missing":
        return "WAL MISSING: recover cannot tell what this process crossed."
    if result["state"] == "never-opened":
        return "WAL never opened: the process crossed nothing."
    text = render(result["report"])
    binding = result["report"].get("binding") or {}
    reached = binding.get("reached") or {}
    for key, where in reached.items():
        text += (f"\nreached {key}: provided by {where['component']}, hosted "
                 f"by process {where['process']}")
    for key in binding.get("unreached") or []:
        text += (f"\nUNREACHED {key}: no process of this placement provides it; "
                 f"its calls are residue")
    if result.get("commitStamped"):
        text += ("\ncommit completed: the index recorded the run's commit and "
                 "this process died before stamping its own activation-complete")
    return text

