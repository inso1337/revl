"""The model schedule inside a running process (roadmap item 515, slice S4).

`revl.model_schedule` decides, at plan time, which device each routed model
role is loaded on for each placement host. The conductor hands that decision
to the host's child in its spec (`spec["modelSchedule"]`), the py runner
re-derives it from the composition's own files and the host's declared
devices, refuses to boot on any difference, and installs the result here.

This module is what code running in that child reads. A host body that loads
or calls a model asks for its role's device, or states the device it is about
to use and has it checked:

    extern fn model_device(role: Str) -> Str
      = @py { from revl import model_placement; return model_placement.device_for(role) }

Both refuse by name, with `ModelPlacementRefused`:

* a role that is not scheduled on this host (a fallback the scheduler did not
  pick, or a role no arm placed here);
* a device other than the scheduled one;
* any question at all in a process with no schedule installed. A process that
  was not handed a schedule has no device to answer with, and guessing one is
  the "any free device" answer the scheduler exists to refuse.

WHAT THIS DOES NOT DO. It does not load a model, and it cannot stop host code
that never asks. The provider adapters that will load and unload a member per
role (slice S2) are the code that is meant to ask, on every load. Until they
exist, this is the checked answer a provider reads, not a sandbox around one.
"""

from __future__ import annotations

import threading
from types import MappingProxyType

_DOC = "docs/model-scheduling.md"

_lock = threading.Lock()
_host: str | None = None
_resident: MappingProxyType | None = None


class ModelPlacementRefused(RuntimeError):
    """A model role was asked for, or claimed, on a device the schedule this
    process was handed does not place it on."""

    def __init__(self, role: str, message: str):
        super().__init__(message)
        self.role = role


def install(host: str, resident: dict) -> None:
    """Install this process's schedule: `{role: device}` for host `host`.

    Called once by the runner at boot, after it has re-derived the schedule.
    A second install with a different schedule is refused rather than
    replacing the first, so the device a role was answered with at boot is the
    one it is answered with for the life of the process.
    """
    global _host, _resident
    frozen = MappingProxyType(dict(resident))
    with _lock:
        if _resident is not None and (_host, dict(_resident)) != (host, dict(frozen)):
            raise ModelPlacementRefused(
                "", f"a model schedule for host `{_host}` is already installed "
                    f"in this process; a second, different schedule is refused")
        _host, _resident = host, frozen


def uninstall() -> None:
    """Remove the installed schedule. For tests and in-process tooling."""
    global _host, _resident
    with _lock:
        _host, _resident = None, None


def installed() -> dict | None:
    """`{"host": ..., "resident": {role: device}}`, or None if none is."""
    with _lock:
        if _resident is None:
            return None
        return {"host": _host, "resident": dict(_resident)}


def device_for(role: str) -> str:
    """The device `role` is scheduled on in this process, or a refusal."""
    with _lock:
        host, resident = _host, _resident
    if resident is None:
        raise ModelPlacementRefused(
            role, f"model role `{role}` was asked for, but this process was "
                  f"handed no model schedule, so no device is scheduled for "
                  f"it here ({_DOC})")
    if role not in resident:
        placed = ", ".join(f"{r} on {d}" for r, d in sorted(resident.items()))
        raise ModelPlacementRefused(
            role, f"model role `{role}` is not scheduled on host `{host}` "
                  f"(scheduled here: {placed or 'nothing'}); a role runs only "
                  f"where the schedule placed it ({_DOC})")
    return resident[role]


def claim(role: str, device: str) -> str:
    """Check that `role` is about to be used on `device`, and return it.

    Refuses a device other than the scheduled one, naming the role, the
    device asked for, and the device the schedule placed it on.
    """
    scheduled = device_for(role)
    if device != scheduled:
        with _lock:
            host = _host
        raise ModelPlacementRefused(
            role, f"model role `{role}` was claimed on device `{device}`, but "
                  f"the schedule for host `{host}` places it on `{scheduled}`; "
                  f"a role is used only on the device it was scheduled on "
                  f"({_DOC})")
    return device
