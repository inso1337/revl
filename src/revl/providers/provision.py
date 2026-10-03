"""A model role's provision: one load and one unload per role per process,
however many consumers share it (roadmap item 515, slice S2; issue #1189).

A `model role` is placed by the model schedule (`revl.model_schedule`, slice
S4): the conductor picks, per host, the device each role is loaded on, and
the host's child installs that decision in `revl.model_placement`. This module
is the code that acts on it.

* A role's provision is keyed by the ROLE, not by the caller. Every model host
  whose operations route to the role is a consumer of the same provision, so
  two keys that reach `small` share one loaded member rather than loading it
  twice.
* The FIRST consumer's `acquire` loads the member, on exactly the device
  `model_placement.device_for(role)` answers, through the role's adapter. A
  role the schedule did not place on this host is not loaded, and a process
  that was handed no schedule loads nothing: both refuse by name
  (`ModelPlacementRefused`), because loading the member somewhere else is the
  "any free device" answer the schedule exists to refuse.
* After the load, the server is asked what it holds. A member it does not
  report as loaded, or one loaded on a different device class than the
  schedule chose, is unloaded again and refused (`ProvisionRefused`). The
  class is read from the share of the model in GPU memory, rounded as
  `ollama ps` rounds it: a `cpu` device must read 0%, a `gpu` device 100%, so
  a model that spilled partly off the GPU is refused too. An `npu` device, or
  a server that reports no sizes, is not checked.
* The LAST consumer's `release` unloads the member. An acquire and a release
  are a pair per consumer: a second acquire by the same consumer, or a release
  by one that holds nothing, is refused rather than counted, so the count of
  holders cannot drift from the consumers that exist.
* `residue()` is the teardown proof. After the last release it lists every way
  the provision is not back to nothing: a holder still present, loads and
  unloads that do not pair, and a member the SERVER still reports loaded.
  Empty is `no_residue`. The last check asks the server rather than trusting
  the unload's reply, since a reply is what revl sent for and residency is
  what the server did.

Only a MANAGED binding (`provider = "ollama"`) has a lifecycle here. Every
other endpoint manages its own residency, and its role has no provision: revl
sends it completions and loads nothing.

RESIDENCY OVER TIME (slice S3's raw material, recorded, not yet read). Every
load and unload is appended to `timeline` with its monotonic time, the device,
the wall time revl waited for the load, and the load duration the server
reported. Nothing reads it to rank candidates yet; the scheduler that would is
S3's.
"""

from __future__ import annotations

import threading
import time

from .. import model_placement
from .transport import ProviderError

DOC = "docs/model-providers.md"


def gpu_share(entry: dict) -> int | None:
    """The percentage of a loaded model held in GPU memory, rounded, read the
    way `ollama ps` prints its PROCESSOR column. None when the server did not
    report both sizes.

    Rounded rather than compared with zero on purpose: a model loaded with
    `num_gpu: 0` still holds a few tens of MiB of GPU memory for the compute
    graph (measured: 64 MiB of a 22 GB model on a local Ollama 0.34.4), which
    the server itself reports as "100% CPU".
    """
    size, vram = entry.get("size"), entry.get("size_vram")
    if not isinstance(size, int) or not isinstance(vram, int) or size <= 0:
        return None
    return round(100 * vram / size)


class ProvisionRefused(RuntimeError):
    """A role's member could not be provisioned the way the schedule says."""

    def __init__(self, role: str, message: str) -> None:
        super().__init__(message)
        self.role = role


class RoleProvision:
    """One managed role's loaded member, shared by every consumer."""

    def __init__(self, role: str, adapter, *, clock=time.monotonic) -> None:
        self.role = role
        self.adapter = adapter
        self._clock = clock
        self._lock = threading.RLock()
        self.holders: list = []
        #: every consumer that ever acquired, in order, for the report
        self.consumers: list = []
        self.loads = 0
        self.unloads = 0
        self.device: str | None = None
        #: the load and unload events, in order (see the module docstring)
        self.timeline: list = []

    # -- the witness pair ---------------------------------------------------

    def acquire(self, consumer: str) -> None:
        with self._lock:
            if consumer in self.holders:
                raise ProvisionRefused(
                    self.role, f"`{consumer}` acquired model role "
                               f"`{self.role}` twice; a consumer holds a role's "
                               f"provision once, so its one release unloads "
                               f"what it loaded")
            if not self.holders:
                self._load()
            self.holders.append(consumer)
            if consumer not in self.consumers:
                self.consumers.append(consumer)

    def release(self, consumer: str) -> None:
        with self._lock:
            if consumer not in self.holders:
                raise ProvisionRefused(
                    self.role, f"`{consumer}` released model role "
                               f"`{self.role}`, which it does not hold")
            self.holders.remove(consumer)
            if not self.holders:
                self._unload()

    # -- load and unload ----------------------------------------------------

    def _load(self) -> None:
        # the schedule's answer, or its refusal: no schedule installed, or the
        # role not placed on this host
        device = model_placement.device_for(self.role)
        binding = self.adapter.binding
        if binding.device_options(device) is None:
            raise ProvisionRefused(
                self.role,
                f"the model schedule places role `{self.role}` on device "
                f"`{device}`, and its {binding.provider} binding names no load "
                f"options for `{device}` (it names "
                f"{', '.join(binding.device_names())}). The member is loaded "
                f"only where the schedule placed it; add `devices.{device}` to "
                f"the binding. See {DOC}")
        started = self._clock()
        reply = self.adapter.load(device)
        self.loads += 1
        self.device = device
        self.timeline.append({
            "event": "load", "device": device, "at": started,
            "load_seconds": reply.get("revl_load_seconds"),
            "server_load_ns": reply.get("load_duration")})
        try:
            self._check_resident(device)
        except BaseException:
            self._unload()
            raise

    def _check_resident(self, device: str) -> None:
        entry = self.adapter.residency()
        if entry is None:
            raise ProvisionRefused(
                self.role, f"model role `{self.role}` was loaded on `{device}`, "
                           f"but the server does not report "
                           f"`{self.adapter.binding.model}` as loaded")
        kind = model_placement.device_class(device)
        share = gpu_share(entry)
        if kind is None or share is None:
            return
        if kind == "cpu" and share != 0:
            found = f"holds {share}% of it in GPU memory"
        elif kind == "gpu" and share != 100:
            found = f"holds {share}% of it in GPU memory"
        else:
            return
        raise ProvisionRefused(
            self.role, f"model role `{self.role}` is scheduled on `{device}`, "
                       f"a `{kind}` device, but after the load the server "
                       f"{found}. The binding's load options for `{device}` do "
                       f"not put the member there, so it is unloaded and the "
                       f"boot refused. See {DOC}")

    def _unload(self) -> None:
        # counted only once the server accepted it: an unload that failed is
        # a load with no pair, which `residue()` then reports
        at = self._clock()
        self.adapter.unload()
        self.unloads += 1
        self.timeline.append({"event": "unload", "device": self.device,
                              "at": at})
        self.device = None

    # -- the teardown proof -------------------------------------------------

    def residue(self) -> list:
        """Every way this provision is not back to nothing. Empty is
        `no_residue`."""
        with self._lock:
            problems = []
            if self.holders:
                problems.append(f"still held by {', '.join(self.holders)}")
            if self.loads != self.unloads:
                problems.append(f"{self.loads} load(s) and {self.unloads} "
                                f"unload(s)")
            if self.loads:
                try:
                    entry = self.adapter.residency()
                except ProviderError as exc:
                    problems.append(f"the server could not be asked what it "
                                    f"holds: {exc}")
                else:
                    if entry is not None:
                        problems.append(
                            f"the server still reports "
                            f"`{self.adapter.binding.model}` loaded")
            return problems

    def summary(self) -> str:
        where = f" on {self.timeline[0]['device']}" if self.timeline else ""
        return (f"model role `{self.role}`: {len(self.consumers)} consumer(s) "
                f"({', '.join(self.consumers)}), {self.loads} load(s){where}, "
                f"{self.unloads} unload(s)")


class Provisions:
    """Every managed role's provision in one process, keyed by role."""

    def __init__(self, adapters: dict) -> None:
        self._by_role = {role: RoleProvision(role, adapter)
                         for role, adapter in sorted(adapters.items())
                         if adapter.managed}

    def __contains__(self, role: str) -> bool:
        return role in self._by_role

    def get(self, role: str) -> RoleProvision | None:
        return self._by_role.get(role)

    def roles(self) -> tuple:
        return tuple(self._by_role)

    def open(self, consumer: str, roles) -> tuple:
        """Acquire, for `consumer`, every managed role in `roles` that the
        schedule placed on this host. Returns the roles acquired.

        A managed role the schedule placed elsewhere is not loaded here; the
        consumer's calls on it refuse by name. A process with no schedule at
        all refuses the first managed role, since it has nowhere to load it.
        On any refusal, the roles already acquired for `consumer` are
        released before the refusal propagates.
        """
        managed = sorted(r for r in set(roles) if r in self._by_role)
        placed = model_placement.installed()
        taken: list = []
        try:
            for role in managed:
                if placed is not None and role not in placed["resident"]:
                    continue
                self._by_role[role].acquire(consumer)
                taken.append(role)
        except BaseException:
            for role in reversed(taken):
                self._by_role[role].release(consumer)
            raise
        return tuple(taken)

    def close(self, consumer: str, roles) -> None:
        """Release every role `consumer` acquired, in reverse order."""
        failure = None
        for role in reversed(tuple(roles)):
            try:
                self._by_role[role].release(consumer)
            except BaseException as exc:  # noqa: BLE001 - release the rest
                failure = failure or exc
        if failure is not None:
            raise failure

    def residue(self) -> dict:
        """`{role: [problem, ...]}` for every role with residue."""
        out = {}
        for role, provision in self._by_role.items():
            problems = provision.residue()
            if problems:
                out[role] = problems
        return out

    def summaries(self) -> list:
        return [p.summary() for p in self._by_role.values() if p.loads]
