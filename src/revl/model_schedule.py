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
load, unload and `no_residue` teardown, is slice S2 and is not built.
"""

from __future__ import annotations

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
    order the program wrote them; a council member is a one-option step."""
    component: str
    action: str
    origin: str
    options: tuple
    council: str | None = None


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
        return [Step(component, action, origin, candidates)]
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
    """
    host: str
    devices: tuple
    placements: tuple
    resident: dict = field(default_factory=dict)

    def loads(self) -> int:
        """How many model loads this host makes: one per resident role."""
        return len(self.resident)

    def lines(self) -> list:
        return [f"model schedule [{self.host}]: {p.describe()}"
                for p in self.placements]

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


def schedule(host: str, devices, roles: dict, steps) -> Schedule:
    """Place every step on `devices`, or raise `ScheduleRefusal`."""
    search = _Search(host, tuple(devices), roles, list(steps))
    if not search.place(0):
        raise ScheduleRefusal(_refusal(search))
    return Schedule(host, tuple(devices), tuple(search.chosen),
                    dict(search.resident))


class _Search:
    """Depth-first placement over the steps, candidates in written order."""

    def __init__(self, host, devices, roles, steps):
        self.host = host
        self.devices = devices
        self.roles = roles
        self.steps = steps
        self.free = {d.name: d.memory_mib for d in devices}
        self.resident: dict[str, str] = {}
        self.chosen: list = [None] * len(steps)
        self.attempts = 0
        self.dead_end = None

    def place(self, index: int) -> bool:
        if index == len(self.steps):
            return True
        step = self.steps[index]
        for rank, role_name in enumerate(step.options):
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
    return (
        f"host `{search.host}` cannot place action `{step.action}` "
        f"({step.component}), origin `{step.origin}`{member}: no candidate "
        f"fits. {reasons}. A candidate set falls back only to the roles it "
        f"names, so the placement is refused rather than moved to a device "
        f"the program did not declare; declare a device that fits in "
        f"[[processes.{search.host}.devices]], or place the component on a "
        f"host that has one (item 515, {_DESIGN})")


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


def placement_schedules(files, processes: dict) -> list:
    """Schedule every placement host, or raise `ScheduleRefusal`.

    Every host's `devices` table is validated whether or not anything is
    routed to it. A host with no routed model action gets no schedule, so a
    composition with no `route model` block schedules nothing and changes
    nothing downstream.
    """
    hosts = {name: parse_devices(name, (conf or {}).get("devices"))
             for name, conf in processes.items()}
    roles, table = routes_of(composition_program(files))
    schedules = []
    for name, conf in processes.items():
        steps = steps_for(table, (conf or {}).get("components") or [])
        if steps:
            schedules.append(schedule(name, hosts[name], roles, steps))
    return schedules
