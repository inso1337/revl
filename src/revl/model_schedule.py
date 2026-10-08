"""The model scheduler: which declared candidate each routed action runs on,
given the devices a host declares (roadmap item 515, slice S4).

`docs/design/539-model-portfolio.md` section 10 is the design and
`docs/model-scheduling.md` is the user-facing page. Slice 1 made the DEMAND
checkable: a `model role` may declare `device <class> memory <MiB> quant
<tag>`, and a `route model` arm may name an ordered candidate set whose
members are all profiled and share one residence. This module is the other
half of the sentence the roadmap item ends on: "a placement onto a host whose
declared device cannot satisfy the profile is refused".

THE SUPPLY IS CONFIGURATION, DECLARED PER HOST
----------------------------------------------
A placement host (`[processes.<p>]`) may list the devices it offers:

    [[processes.edge.devices]]
    name = "gpu0"
    device = "gpu"
    memory_mib = 8192
    quantisation = ["q4_k_m", "int8"]

The three field names after `name` are the ones `model_profile.declared_floor`
returns for the demand, so the comparison is field by field. This is the same
kind of claim as item 119's `capabilities = [...]`: an operator's statement
about a host, checked against the program before anything spawns. Nothing
here probes hardware. See "What is not checked" below.

THE DECISION
------------
For each host, every routed action of every component placed there is a
STEP, in program order. A role arm is satisfied by ONE of its candidates, in
the order written. A council arm is satisfied only when EVERY member role is
placed, because a council asks every member. A candidate fits a device when
the device class is equal, the quantisation tag is one the device lists, and
the device has the memory free.

A role is loaded once per host and shared: a later step that picks a role
already resident reuses its device and reserves nothing. That is the issue's
"two consumers that inject `small` share one provision rather than loading
twice", at the level of the schedule, and a test shows it changing the answer.

The search is a depth-first walk that tries candidates in written order and,
within a candidate, devices in declared order. The first complete assignment
it finds is the answer, so an earlier step gets its preferred candidate
before a later one does, and the same inputs always give the same schedule.
The walk is exhaustive up to `SEARCH_BUDGET` placement attempts; past that it
refuses rather than admitting a schedule it did not finish looking for.

ONE ARM MAY ASK FOR RESIDENCY TO BREAK A TIE (item 2118)
-------------------------------------------------------
An arm may close with `prefer resident`:

    route model on classify {
      confidential -> fast | small prefer resident,
      * -> cloud
    }

That arm's author is saying a candidate the host ALREADY HOLDS may beat an
earlier-written one, and the clause is on one arm: there is no block-level
form of it, no host-level form, and no command-line form, so it can never
reorder a second arm and never becomes a default. An arm that does not write
it is ordered by the written set whatever the host reports, and a caller that
passes no residency at all - which is every caller that does not mean to rank
by it - schedules exactly as it did before the clause existed. That is
design note 539 section 11.6 item 2, over decision 12: the written order is
the preference, and residency is a preference an author states.

What residency can change is WHICH candidate an opted-in arm settles on. It
does not change the candidate set, and it does not change the memory the plan
reserves for the role it picks: reading what the host holds into the plan's
own accounting is item 1's second half, which this clause is the surface for
rather than the whole of. `resident_roles()` reads what a host holds off the
landed reader, and `schedule(..., residency=...)` is where it is consumed.

WHICH WAY THIS FAILS
--------------------
Closed.

* No candidate fits: refused, naming the host, the action, the origin, and
  for each candidate why it does not fit. A candidate set falls back only to
  the roles it names; there is no "any free device".
* A host with no `devices` offers none. A profiled on-device role placed on it
  is refused, as item 119 refuses a capability the host does not list.
* A malformed `devices` table (unknown key, missing key, unknown device
  class, a non-positive memory, an empty quantisation list, a repeated name)
  is refused by name, never read as "no requirement".

Two kinds of candidate reserve nothing, and both are stated in the schedule
rather than hidden: an `off_device` role runs off this host, so this host's
devices say nothing about it; and an on-device role with no `device` clause
makes no resource claim (slice 1's decision 10 allows that only for a
single-candidate arm). They are placed with `device = None`.

WHAT IS NOT CHECKED
-------------------
That the declared devices exist. `memory_mib = 8192` is a claim in a
configuration file, exactly as `device gpu memory 6144` is a claim in a
program. The scheduler compares two declarations. Whether the loaded member
matches is the provider's published profile, which reaches revl only as
`placement_digest` (`revl.model_profile`, item 538).

Nothing loads or unloads a model here. The schedule is a plan-time decision
the conductor prints and refuses on; the provision keyed by role, with its
load, unload and `no_residue` teardown, is slice S2 and has landed.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field

from .model_profile import DEVICE_CLASSES

#: The keys a `[[processes.<p>.devices]]` entry must carry, and the only ones.
DEVICE_KEYS = ("name", "device", "memory_mib", "quantisation")

#: How many placement attempts the search makes before it refuses. A real
#: portfolio is a handful of roles on a handful of devices and settles in
#: tens of attempts; the bound exists so a pathological placement file is a
#: refusal and not a hang.
SEARCH_BUDGET = 10_000

#: How a placement came to have its device, as the schedule reports it.
RESERVED = "reserved"
SHARED = "shared"
OFF_DEVICE = "off_device"
UNPROFILED = "unprofiled"

_DESIGN = "docs/model-scheduling.md"


class ScheduleRefusal(ValueError):
    """A model placement a host cannot satisfy, or a malformed host profile."""


# --------------------------------------------------------------------------
# The supply: a host's declared devices
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Device:
    """One device a host declares it offers (the SUPPLY)."""
    name: str
    device: str
    memory_mib: int
    quantisation: tuple

    def describe(self) -> str:
        return (f"{self.name} ({self.device}, {self.memory_mib} MiB, "
                f"quant {', '.join(self.quantisation)})")


def parse_devices(host: str, raw) -> tuple:
    """Validate a host's `devices` list. `None` is a host that offers none."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ScheduleRefusal(
            f"host `{host}`: `devices` must be a list of tables "
            f"(`[[processes.{host}.devices]]`), got {type(raw).__name__}")
    devices: list[Device] = []
    for index, entry in enumerate(raw):
        device = _parse_device(host, index, entry)
        if any(d.name == device.name for d in devices):
            raise ScheduleRefusal(
                f"host `{host}` declares device `{device.name}` twice; a "
                f"placement names a device, so a name must pick one")
        devices.append(device)
    return tuple(devices)


def _parse_device(host: str, index: int, entry) -> Device:
    where = f"host `{host}` device #{index + 1}"
    if not isinstance(entry, dict):
        raise ScheduleRefusal(f"{where} must be a table with keys "
                              f"{', '.join(DEVICE_KEYS)}")
    unknown = sorted(set(entry) - set(DEVICE_KEYS))
    if unknown:
        raise ScheduleRefusal(
            f"{where} has unknown key(s) {', '.join(unknown)}; the keys are "
            f"{', '.join(DEVICE_KEYS)}")
    missing = [key for key in DEVICE_KEYS if key not in entry]
    if missing:
        raise ScheduleRefusal(
            f"{where} is missing {', '.join(missing)}; a device that leaves a "
            f"field out is not read as offering anything in it")
    return Device(_device_name(where, entry["name"]),
                  _device_class(where, entry["device"]),
                  _device_memory(where, entry["memory_mib"]),
                  _device_quantisation(where, entry["quantisation"]))


def _device_name(where: str, value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScheduleRefusal(f"{where}: `name` must be a non-empty string")
    return value


def _device_class(where: str, value) -> str:
    if value not in DEVICE_CLASSES:
        raise ScheduleRefusal(
            f"{where}: unknown device class {value!r}; the vocabulary is "
            f"{', '.join(DEVICE_CLASSES)}, the same one a `model role` "
            f"declares against")
    return value


def _device_memory(where: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ScheduleRefusal(
            f"{where}: `memory_mib` must be a positive integer, got {value!r}")
    return value


def _device_quantisation(where: str, value) -> tuple:
    if (not isinstance(value, list) or not value
            or not all(isinstance(q, str) and q for q in value)):
        raise ScheduleRefusal(
            f"{where}: `quantisation` must be a non-empty list of tags; a "
            f"device that loads no quantisation places nothing")
    return tuple(value)


# --------------------------------------------------------------------------
# The demand: one step per routed action (per member, for a council)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    """One placement the schedule must make. `options` are role names in the
    order the program wrote them; a council member is a one-option step.

    `prefer_resident` is the arm's per-arm opt-in (item 2118, `prefer
    resident`). It is False on every step of every arm that did not write the
    clause, and `_Search.order()` reads residency on a step ONLY when it is
    True - so `options` is the whole ordering for an arm that did not opt in,
    whatever the host reports."""
    component: str
    action: str
    origin: str
    options: tuple
    council: str | None = None
    prefer_resident: bool = False


def steps_for(table: dict, components=None) -> list:
    """The steps of `model_route.check()`'s table, in program order.

    `components` restricts the steps to the components placed on one host;
    `None` takes every component in the table.
    """
    wanted = None if components is None else set(components)
    steps: list[Step] = []
    for component, actions in table.items():
        if wanted is not None and component not in wanted:
            continue
        for action, arms in actions.items():
            for origin, placement in arms.items():
                steps.extend(_arm_steps(component, action, origin, placement))
    return steps


def _arm_steps(component: str, action: str, origin: str, placement: dict) -> list:
    council = placement.get("council")
    candidates = tuple(placement.get("candidates") or (placement["role"],))
    if council is None:
        return [Step(component, action, origin, candidates, None,
                     bool(placement.get("prefer_resident")))]
    # A council member step is one option by construction, so it is never
    # rankable and never carries the opt-in; `revl.model_route` refuses the
    # clause on a council arm rather than leaving it here unread.
    return [Step(component, action, origin, (member,), council)
            for member in candidates]


# --------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Placement:
    """Where one step landed."""
    component: str
    action: str
    origin: str
    candidates: tuple
    role: str
    rank: int
    device: str | None
    how: str
    council: str | None = None

    def describe(self) -> str:
        where = {RESERVED: f"on {self.device}",
                 SHARED: f"on {self.device}, shared with an earlier placement",
                 OFF_DEVICE: "off_device, reserves nothing on this host",
                 UNPROFILED: "declares no device profile, reserves nothing"}
        via = f" (member of council `{self.council}`)" if self.council else ""
        fallback = (f", fallback {self.rank} of {len(self.candidates) - 1}"
                    if self.rank else "")
        return (f"{self.component}.{self.action} {self.origin} -> "
                f"{self.role}{via} {where[self.how]}{fallback}")


@dataclass(frozen=True)
class Schedule:
    """The decision for one host.

    `resident` maps each role this host loads to the one device it is loaded
    on: one entry per role however many steps chose it.

    `residency` is the OTHER direction and is NOT `resident`: it is what the
    host ALREADY HELD when this decision was made, `{role: device}`, and it is
    an INPUT, not part of the decision. It is recorded only when an arm on this
    host wrote `prefer resident` and so actually ranked by it (item 2118), so
    it is empty for every schedule made before that clause existed and for
    every host whose arms all left the clause out - a reader that ignores it
    reads what it always read.

    A value is where the role is held, as precisely as the reader that
    produced it can know it: a device NAME from a load revl made
    (`resident_roles`, which reads the device its own provision chose), and a
    device CLASS from a server's own report
    (`revl.providers.plan_time_residency`, because `/api/ps` answers which
    memory a member occupies and not which device it sits in). Only the KEYS
    are read - `_Search.order` asks whether a candidate is held, never on
    what - so either vocabulary is a usable input and the two producers are
    interchangeable.
    """
    host: str
    devices: tuple
    placements: tuple
    resident: dict = field(default_factory=dict)
    demands: dict = field(default_factory=dict, compare=False)
    residency: dict = field(default_factory=dict, compare=False)

    def loads(self) -> int:
        """How many model loads this host makes: one per resident role."""
        return len(self.resident)

    def lines(self) -> list:
        lines = [f"model schedule [{self.host}]: {p.describe()}"
                 for p in self.placements]
        if self.residency:
            # item 1189 §11.6 item 1: a decision that read what the host held
            # is not reproducible from the composition alone, and a reader who
            # is not told so would read the written order back as the reason.
            held = ", ".join(f"{role} ({device})"
                             for role, device in sorted(self.residency.items()))
            lines.append(
                f"model schedule [{self.host}]: ranked by what the server "
                f"reported holding at plan time ({held}); this decision is "
                f"not reproducible from the composition alone")
        return lines

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "resident": dict(self.resident),
            "placements": [
                {"component": p.component, "action": p.action,
                 "origin": p.origin, "candidates": list(p.candidates),
                 "role": p.role, "rank": p.rank, "device": p.device,
                 "how": p.how, "council": p.council}
                for p in self.placements],
        }


def schedule(host: str, devices, roles: dict, steps,
             residency: dict | None = None) -> Schedule:
    """Place every step on `devices`, or raise `ScheduleRefusal`.

    `residency` is `{role: device}` for what the host already holds at plan
    time - `resident_roles()` reads it off the landed reader - and it is read
    by exactly the steps whose arm opted in with `prefer resident` (item
    2118). The default is no residency at all, so a caller that passes none
    makes the decision it made before the clause existed: written order.
    """
    residency = dict(residency or {})
    steps = list(steps)
    search = _Search(host, tuple(devices), roles, steps, residency)
    if not search.place(0):
        raise ScheduleRefusal(_refusal(search))
    chosen = tuple(search.chosen)
    demands = {p.role: roles[p.role].profile for p in chosen
               if roles[p.role].profile is not None}
    # A residency is recorded only when some arm on this host asked for one.
    # A caller that passes residency to a program whose arms all left the
    # clause out gets the schedule it always got AND the entry it always got,
    # because an input no arm reads is not part of the decision.
    ranked = residency if any(s.prefer_resident for s in steps) else {}
    return Schedule(host, tuple(devices), chosen, dict(search.resident),
                    demands, ranked)


class _Search:
    """Depth-first placement over the steps, candidates in written order.

    `residency` is what the host already holds, and `order()` is the only
    place it is read: the search itself is unchanged, so the set of
    placements it considers is the same set it always considered and only the
    sequence in which an opted-in arm tries them differs.
    """

    def __init__(self, host, devices, roles, steps, residency=None):
        self.host = host
        self.devices = devices
        self.roles = roles
        self.steps = steps
        self.residency = dict(residency or {})
        self.free = {d.name: d.memory_mib for d in devices}
        self.resident: dict[str, str] = {}
        self.chosen: list = [None] * len(steps)
        self.attempts = 0
        self.dead_end = None

    def order(self, step: Step) -> tuple:
        """`step.options` in the order this plan tries them.

        The written order, unless the arm opted in AND the host reported what
        it holds: then the candidates the host already holds come first, each
        group still in the order the program wrote it. `Step.options` remains
        the written set, and `Placement.rank` is still an index into it, so
        what the plan REPORTS is unchanged - only which candidate it settles
        on can differ, and only for an arm that asked.

        Residency orders candidates here and does nothing else: it does not
        change the memory the plan reserves for the role it picks, and it
        cannot move a role into or out of the candidate set. Reading what the
        host holds into the plan's own memory accounting is item 1's second
        half, which this clause is the surface for rather than the whole of.
        """
        if not step.prefer_resident or not self.residency:
            return step.options
        held = tuple(name for name in step.options if name in self.residency)
        cold = tuple(name for name in step.options if name not in self.residency)
        return held + cold

    def place(self, index: int) -> bool:
        if index == len(self.steps):
            return True
        step = self.steps[index]
        for role_name in self.order(step):
            # `rank` indexes the WRITTEN set, not the order tried, so a
            # fallback reported by an opted-in arm is the position the program
            # wrote rather than the position residency moved it to.
            rank = step.options.index(role_name)
            for device, how in self.options(role_name):
                self._count_attempt()
                self._take(index, step, rank, role_name, device, how)
                if self.place(index + 1):
                    return True
                self._undo(index, role_name, device, how)
        self._note_dead_end(index)
        return False

    def options(self, role_name: str):
        """The ways `role_name` can be placed now, in preference order."""
        role = self.roles[role_name]
        if role.off_device:
            return [(None, OFF_DEVICE)]
        if role.profile is None:
            return [(None, UNPROFILED)]
        if role_name in self.resident:
            return [(self.resident[role_name], SHARED)]
        return [(d.name, RESERVED) for d in self.devices
                if fits(role.profile, d, self.free[d.name])]

    def _count_attempt(self) -> None:
        self.attempts += 1
        if self.attempts > SEARCH_BUDGET:
            raise ScheduleRefusal(
                f"host `{self.host}`: the model schedule did not settle within "
                f"{SEARCH_BUDGET} placement attempts, so it is refused rather "
                f"than admitted without a finished search ({_DESIGN})")

    def _take(self, index, step, rank, role_name, device, how) -> None:
        if how == RESERVED:
            self.resident[role_name] = device
            self.free[device] -= self.roles[role_name].profile.memory_mib
        self.chosen[index] = Placement(step.component, step.action,
                                       step.origin, step.options, role_name,
                                       rank, device, how, step.council)

    def _undo(self, index, role_name, device, how) -> None:
        if how == RESERVED:
            del self.resident[role_name]
            self.free[device] += self.roles[role_name].profile.memory_mib
        self.chosen[index] = None

    def _note_dead_end(self, index: int) -> None:
        if self.dead_end is None or index > self.dead_end[0]:
            self.dead_end = (index, dict(self.free), dict(self.resident))


def fits(profile, device: Device, free_mib: int) -> bool:
    """Whether a declared demand fits a declared device with `free_mib` left."""
    return (device.device == profile.device
            and profile.quant in device.quantisation
            and free_mib >= profile.memory_mib)


# --------------------------------------------------------------------------
# The refusal
# --------------------------------------------------------------------------

def _refusal(search: _Search) -> str:
    """Name the step that cannot be placed, and why each candidate misses.

    A step that fits nowhere even on an empty host is named first, because
    that is a fact about the host and not about contention. Otherwise the
    deepest step the search could not place is named, with the memory the
    earlier placements had already taken.
    """
    empty = {d.name: d.memory_mib for d in search.devices}
    for step in search.steps:
        if not _fits_alone(search, step, empty):
            return _message(search, step, empty, {})
    index, free, resident = search.dead_end
    return _message(search, search.steps[index], free, resident)


def _fits_alone(search: _Search, step: Step, free: dict) -> bool:
    for role_name in step.options:
        role = search.roles[role_name]
        if role.off_device or role.profile is None:
            return True
        if any(fits(role.profile, d, free[d.name]) for d in search.devices):
            return True
    return False


def _message(search: _Search, step: Step, free: dict, resident: dict) -> str:
    member = (f" (member of council `{step.council}`)" if step.council
              else "")
    reasons = "; ".join(
        f"`{name}` ({search.roles[name].profile.describe()}): "
        f"{_why_not(search, search.roles[name].profile, free, resident)}"
        for name in step.options)
    # item 2118: an arm that opted in was tried in an order residency chose,
    # and a refusal that did not say so would leave the author reading a list
    # that does not match the search.
    ranked = ""
    if step.prefer_resident:
        ranked = (" This arm wrote `prefer resident`, so its candidates were "
                  "tried " + ", then ".join(f"`{name}`"
                                            for name in search.order(step))
                  + ".")
    return (
        f"host `{search.host}` cannot place action `{step.action}` "
        f"({step.component}), origin `{step.origin}`{member}: no candidate "
        f"fits. {reasons}. A candidate set falls back only to the roles it "
        f"names, so the placement is refused rather than moved to a device "
        f"the program did not declare; declare a device that fits in "
        f"[[processes.{search.host}.devices]], or place the component on a "
        f"host that has one (item 515, {_DESIGN}){ranked}")


def _why_not(search: _Search, profile, free: dict, resident: dict) -> str:
    if not search.devices:
        return f"host `{search.host}` declares no devices"
    same_class = [d for d in search.devices if d.device == profile.device]
    if not same_class:
        return f"no {profile.device} device"
    loads = [d for d in same_class if profile.quant in d.quantisation]
    if not loads:
        return "; ".join(f"{d.name} does not load quant {profile.quant}"
                         for d in same_class)
    return "; ".join(_memory_short(d, profile, free, resident) for d in loads)


def _memory_short(device: Device, profile, free: dict, resident: dict) -> str:
    holders = sorted(role for role, name in resident.items()
                     if name == device.name)
    held = f", held by {', '.join(holders)}" if holders else ""
    return (f"{device.name} has {free[device.name]} of {device.memory_mib} "
            f"MiB free{held}, needs {profile.memory_mib}")


# --------------------------------------------------------------------------
# Reading the route table of a composition, and the conductor's entry point
# --------------------------------------------------------------------------

def routes_of(program) -> tuple:
    """`(roles, table)` for a parsed program, checked the way `lower` checks
    them: councils first, then routes, then the role table."""
    from . import model_council, model_route  # noqa: PLC0415 - import cycle
    councils = model_council.check(program)
    table = model_route.check(program, councils=councils)
    return model_route.roles(program), table


def composition_program(files) -> object:
    """The components and model declarations of a multi-file composition.

    Mirrors how `compiler.compile_files` merges them: components from the
    root files, and `model role` / `model council` declarations from the
    roots plus the closure of modules whose pure declarations they import.
    Called only after `compile_files` admitted the same files, so a load
    failure here is not expected and is not caught.
    """
    from .compiler import _load_root, _ModuleLoader  # noqa: PLC0415
    from .parser import Program  # noqa: PLC0415
    loader = _ModuleLoader()
    loader.mark_roots(os.path.abspath(p) for p in files)
    roots = [_load_root(loader, str(p)) for p in files]
    merged = Program(filename=str(files[0]) if files else "<none>")
    for module in _unique(roots):
        merged.components.extend(module.program.components)
    for module in _declaration_closure(loader, roots):
        merged.model_roles.extend(module.program.model_roles)
        merged.model_councils.extend(module.program.model_councils)
    return merged


def _unique(modules) -> list:
    seen: set[int] = set()
    out = []
    for module in modules:
        if id(module) not in seen:
            seen.add(id(module))
            out.append(module)
    return out


def _declaration_closure(loader, roots) -> list:
    by_id = {id(m): m for m in loader._cache.values()}
    included = _unique(roots)
    queue = list(included)
    seen = {id(m) for m in included}
    while queue:
        module = queue.pop(0)
        for dep_id in sorted(module.pure_dependencies,
                             key=lambda value: by_id[value].path):
            if dep_id not in seen:
                seen.add(dep_id)
                included.append(by_id[dep_id])
                queue.append(by_id[dep_id])
    return included


def placement_schedules(files, processes: dict,
                        residency: dict | None = None) -> list:
    """Schedule every placement host, or raise `ScheduleRefusal`.

    Every host's `devices` table is validated whether or not anything is
    routed to it. A host with no routed model action gets no schedule, so a
    composition with no `route model` block schedules nothing and changes
    nothing downstream.

    `residency` is `{host: {role: device}}` - what each host already holds at
    plan time, as `resident_roles()` reads it off the landed reader - and each
    host's entry reaches `schedule()` under the same rule: read by the steps
    whose arm wrote `prefer resident`, and by no others. A caller that passes
    none schedules exactly as it did before the clause existed, which is every
    caller that does not mean to rank by residency.
    """
    hosts = {name: parse_devices(name, (conf or {}).get("devices"))
             for name, conf in processes.items()}
    roles, table = routes_of(composition_program(files))
    schedules = []
    for name, conf in processes.items():
        steps = steps_for(table, (conf or {}).get("components") or [])
        if steps:
            schedules.append(schedule(name, hosts[name], roles, steps,
                                      (residency or {}).get(name) or {}))
    return schedules


def resident_roles(provisions) -> dict:
    """`{role: device}` for every role a `Provisions` reports as HELD now.

    This is the read side of item 2118 for a caller that HOLDS the provisions:
    `placement_schedules(files, processes, residency={host:
    resident_roles(provisions)})`. It asks the reader landed by item 1
    (`revl.providers.provision.Provisions.residency()`) and derives nothing of
    its own - a role counts as held exactly when that report shows it loaded
    more often than it unloaded AND on a device, because a role that was
    loaded and unloaded is not resident and one that never reached a device is
    not a placement this host can reuse.

    A conductor planning a fresh placement has no provisions to read - nothing
    is loaded yet - so it reaches residency the other way, by asking each
    server what it holds now: `residency_candidates()` below says which roles
    are worth asking about, and `revl.providers.plan_time_residency()` asks.
    Neither route is the other's input; both end at `schedule()`.

    Pure: `residency()` reads the record and asks the server nothing, so this
    can be called at plan time.
    """
    held = {}
    for role, report in provisions.residency().items():
        if report.get("loads", 0) > report.get("unloads", 0) and report.get("device"):
            held[role] = report["device"]
    return held


def residency_candidates(files, processes: dict) -> dict:
    """`{host: (role, ...)}`: the candidate roles whose held-ness could change
    a host's decision, and nothing else.

    Residency reaches a step only through `prefer resident`, so the candidates
    of the arms that wrote the clause are exactly the roles a plan-time read
    has to ask a server about. Every other role on the host is left out, which
    is what keeps the read off the network for the compositions that do not
    use the clause and for the hosts whose arms all left it out.

    Pure: reads the composition's files and the placement, asks no server.
    """
    _, table = routes_of(composition_program(files))
    candidates: dict = {}
    for name, conf in processes.items():
        steps = steps_for(table, (conf or {}).get("components") or [])
        names = sorted({role for step in steps if step.prefer_resident
                        for role in step.options})
        if names:
            candidates[name] = tuple(names)
    return candidates


def residency_wanted(files, processes: dict) -> bool:
    """Whether any step this placement schedules wrote `prefer resident`.

    False for every composition that does not use the clause, and False for a
    placement whose opted-in components are on no host. A caller reads this
    BEFORE it reads any configuration, because a placement that ranks nothing
    must not be planned against a server it does not otherwise need.
    """
    return bool(residency_candidates(files, processes))


# --------------------------------------------------------------------------
# The handoff: the conductor writes a host's schedule into its child's spec,
# and the child re-derives it before believing it
# --------------------------------------------------------------------------

#: The spec key the conductor writes a host's schedule under. It is absent
#: from the spec of every host with no routed model action, so a composition
#: with no `route model` block spawns byte for byte as it did before.
SPEC_KEY = "modelSchedule"

#: The backend tiers whose process runner reads `SPEC_KEY`. A schedule handed
#: to a runner that ignores it is a decision nothing enforces, so the
#: conductor refuses to place a scheduled host on any other tier.
READING_TIERS = ("py",)


def handoff(decided: Schedule) -> dict:
    """The `spec[SPEC_KEY]` entry for one host: its declared devices and the
    decision. The devices are carried so the child can re-derive the decision
    from the composition's files instead of believing the entry.

    A schedule that ranked by residency also carries the residency it ranked
    on, under `residency`. That is an INPUT of the decision, exactly as
    `devices` is, so carrying it is what keeps the child's re-derivation the
    SAME decision rather than a second one: the child still recomputes from
    the files and still refuses any difference, and the entry is still not
    believed over them. A schedule that ranked nothing - every host whose arms
    all left the clause out, and every host scheduled before the clause
    existed - carries no `residency` key at all, so its entry is byte for byte
    the entry it was.
    """
    entry = {
        "host": decided.host,
        "devices": [{"name": d.name, "device": d.device,
                     "memory_mib": d.memory_mib,
                     "quantisation": list(d.quantisation)}
                    for d in decided.devices],
        "schedule": decided.to_dict(),
    }
    if decided.residency:
        entry["residency"] = dict(decided.residency)
    return entry


def verify_handoff(files, host: str, components, entry) -> dict | None:
    """Re-derive a child's schedule and compare it with the one it was handed.

    Returns `{role: device}` to install, or None when the host routes no model
    action and was handed nothing. Raises `ScheduleRefusal` when:

    * the host routes a model action and the spec carries no schedule;
    * the spec carries a schedule for a host that routes nothing;
    * the entry is malformed, names another host, or differs in any field from
      the schedule derived from `files`, `components` and the entry's own
      declared devices.

    `residency` is the one field a child does not derive for itself, because
    it is a fact about the host rather than about the composition: it is what
    the conductor observed the host holding when it planned. The child takes
    it as given and re-derives the DECISION from it, so a conductor that
    ranked by residency and a child that did not know what the host held would
    still not disagree - the child is handed the input the conductor used.
    """
    roles, table = routes_of(composition_program(files))
    steps = steps_for(table, components)
    if entry is None:
        if steps:
            raise ScheduleRefusal(
                f"host `{host}` routes model action(s) but its spec carries no "
                f"model schedule; a process does not run a model it was not "
                f"scheduled onto ({_DESIGN})")
        return None
    if not isinstance(entry, dict) or not (
            {"host", "devices", "schedule"} <= set(entry)
            and set(entry) <= {"host", "devices", "schedule", "residency"}):
        raise ScheduleRefusal(
            f"host `{host}`: the model schedule in its spec is malformed")
    if entry["host"] != host:
        raise ScheduleRefusal(
            f"host `{host}` was handed the model schedule of host "
            f"`{entry['host']}`")
    if not steps:
        raise ScheduleRefusal(
            f"host `{host}` routes no model action but its spec carries a "
            f"model schedule; a schedule for nothing is refused")
    residency = entry.get("residency") or {}
    if not isinstance(residency, dict) or not all(
            isinstance(role, str) and isinstance(device, str)
            for role, device in residency.items()):
        raise ScheduleRefusal(
            f"host `{host}`: the residency in its model schedule spec is "
            f"malformed; it must map role names to the device each was held on "
            f"(or, for a server read, the class it was held in)")
    if residency and not any(step.prefer_resident for step in steps):
        raise ScheduleRefusal(
            f"host `{host}`: the model schedule in its spec carries a "
            f"residency but no arm placed here wrote `prefer resident`, so "
            f"nothing ranked by it; an input no arm reads is refused rather "
            f"than carried (item 2118, {_DESIGN})")
    expected = schedule(host, parse_devices(host, entry["devices"]), roles,
                        steps, residency).to_dict()
    if entry["schedule"] != expected:
        raise ScheduleRefusal(
            f"host `{host}`: the model schedule in its spec does not match the "
            f"one derived from the composition and the host's declared "
            f"devices ({_describe_difference(entry['schedule'], expected)}); "
            f"the spec is not believed over the files it names")
    return dict(expected["resident"])


def _describe_difference(handed, expected: dict) -> str:
    if not isinstance(handed, dict):
        return "the schedule is not a table"
    if handed.get("resident") != expected["resident"]:
        return (f"handed resident {handed.get('resident')!r}, derived "
                f"{expected['resident']!r}")
    return "the placements differ"


# --------------------------------------------------------------------------
# The binding manifest: each role's binding, per host, and a digest over it
# (slice S5, placement-side)
# --------------------------------------------------------------------------

#: The first line of the digest preimage. A second version of the manifest
#: gets a second tag rather than a reinterpretation of this one.
BINDINGS_VERSION = "revl-model-bindings-v1"


def binding_manifest(schedules) -> dict | None:
    """The placement-side record of which member each role is bound to.

    One entry per host that routes a model action, carrying the host's
    declared devices and one row per role the schedule chose there: the
    role's residence, its declared demand, the device it is bound to, and the
    routed actions that use it. `digest` is sha256 over everything else, so a
    change to one role's device, quantisation or memory, to a host's declared
    devices, or to which actions share a role, changes it.

    Returns None when nothing is scheduled, so a composition with no
    `route model` block has no manifest at all rather than an empty one.

    This is NOT item 517's `placement_digest`, which the PROVIDER computes over
    what it actually loaded. This digest is over what the placement DECLARED
    and the scheduler DECIDED; the compiler IR carries neither.
    """
    if not schedules:
        return None
    hosts = [_host_entry(decided) for decided in schedules]
    return {"version": BINDINGS_VERSION, "hosts": hosts,
            "digest": bindings_digest(hosts)}


def _host_entry(decided: Schedule) -> dict:
    rows: dict[str, dict] = {}
    for placement in decided.placements:
        row = rows.get(placement.role)
        if row is None:
            row = rows[placement.role] = _binding_row(decided, placement)
        row["consumers"].append(
            f"{placement.component}.{placement.action} {placement.origin}")
    return {"host": decided.host,
            "devices": handoff(decided)["devices"],
            "bindings": [rows[name] for name in sorted(rows)]}


def _binding_row(decided: Schedule, placement: Placement) -> dict:
    role = _role_of(decided, placement)
    device = next((d for d in decided.devices if d.name == placement.device),
                  None)
    return {
        "role": placement.role,
        "residence": "off_device" if placement.how == OFF_DEVICE else "on_device",
        "demand": role,
        "device": placement.device,
        "device_class": device.device if device else None,
        "consumers": [],
    }


def _role_of(decided: Schedule, placement: Placement) -> dict | None:
    """The declared demand the schedule placed, as plain data."""
    demand = decided.demands.get(placement.role)
    if demand is None:
        return None
    return {"device": demand.device, "memory_mib": demand.memory_mib,
            "quant": demand.quant}


def bindings_digest(hosts) -> str:
    """sha256 over the version line and the canonical JSON of `hosts`."""
    body = json.dumps(hosts, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)
    return hashlib.sha256(
        (BINDINGS_VERSION + "\n" + body).encode("utf-8")).hexdigest()


def binding_lines(manifest: dict | None) -> list:
    """The operator's view of the manifest: one line per binding, then the
    digest. Empty for a composition with no scheduled role."""
    if manifest is None:
        return []
    lines = []
    for host in manifest["hosts"]:
        for row in host["bindings"]:
            demand = row["demand"]
            wants = (f"device {demand['device']} memory {demand['memory_mib']} "
                     f"quant {demand['quant']}" if demand else "no profile")
            where = (f"on {row['device']} ({row['device_class']})"
                     if row["device"] else f"{row['residence']}, no device")
            lines.append(f"model binding [{host['host']}]: {row['role']} "
                         f"{where}, {wants}; used by "
                         f"{', '.join(row['consumers'])}")
    lines.append(f"model bindings digest: {manifest['digest']}")
    return lines
