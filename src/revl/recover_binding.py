"""The real world for `revl recover`: the composition's own binding (issue #1477).

Without this, recover replays a WAL against `DictWorld`, an in-memory model,
and says so (`world: "model"`). With `revl recover --composition FILE`, it
replays the WAL's discharge descriptors through the composition's own host
bodies and providers instead:

* FILE is compiled and digested exactly as the WAL header's `composition`
  digest was (`replay.composition_digest`). A log with no digest, or with a
  different one, is refused by name: replaying a log through another
  composition's host bodies would call the wrong code with the right arguments.
* The composition's emitted module is loaded through the driver's own plug seam
  (`_Driver._emit_module`, which installs extern config and bound secrets) and
  is NOT activated, so no activation body runs again.
* A descriptor whose `call.receiver` is a required-service key needs the live
  provider. Recover boots only the components that PROVIDE those keys, and what
  they in turn require, as a composition of their own. The verdict names every
  component it booted: booting a provider runs that provider's activation.
* The replay itself is `runtime.replay_descriptors`, the frame's own abort path,
  so the fences, the Phase-2 budget, the `aborted` record that settles what ran,
  and the E-Stop check are the runtime's. This module adds no executor.

The world this builds declares ``kind = "real"``. It re-issues discharge
descriptors and nothing else: a legacy boundary inverse, an owed deferred
emission and a shared reclaim are not calls the runtime's replay path knows how
to make, so recover reports them as residue, not attempted, rather than model
them or guess.
"""

from __future__ import annotations

import sys
from typing import Any, Optional

from .recovery import WORLD_REAL, RecoveryError, World


class CompositionMismatch(RecoveryError):
    """The composition named on the command line is not the one the WAL was
    written by, or the WAL does not say which one it was."""


def _replay_module():
    from .mcp.session import replay_module  # noqa: PLC0415 - backend on the path
    return replay_module()


def _open_descriptor_receivers(records: list) -> set:
    """The required-service keys the still-open descriptors call through. A seq
    named in a `discharge` or `aborted` record is settled and needs nothing."""
    settled: set = set()
    for record in records:
        if record.get("record") == "discharge":
            settled.update(record.get("discharged") or [])
        elif record.get("record") == "aborted":
            settled.update(record.get("replayed") or [])
    return {(r.get("call") or {}).get("receiver") for r in records
            if r.get("record") == "discharge-descriptor"
            and r.get("seq") not in settled
            and (r.get("call") or {}).get("receiver") is not None}


def _provider_closure(ir: dict, keys: set) -> list:
    """The component names to boot so every key in ``keys`` has a live
    provider: each component that provides one, and transitively whatever
    those require. Load order is the composition's own."""
    components = ir.get("components") or []
    providers: dict = {}
    for comp in components:
        for key in comp.get("provides") or {}:
            providers.setdefault(key, comp["name"])
    wanted: set = set()
    pending = list(keys)
    while pending:
        key = pending.pop()
        name = providers.get(key)
        if name is None or name in wanted:
            continue
        wanted.add(name)
        comp = next(c for c in components if c["name"] == name)
        pending.extend((comp.get("requires") or {}).keys())
    order = (ir.get("manifest") or {}).get("loadOrder") or [
        c["name"] for c in components]
    return [name for name in order if name in wanted]


def _sub_composition(ir: dict, names: list) -> dict:
    """``ir`` restricted to the components in ``names``."""
    keep = set(names)
    sub = dict(ir)
    sub["components"] = [c for c in ir.get("components") or []
                         if c["name"] in keep]
    manifest = dict(ir.get("manifest") or {})
    if "components" in manifest:
        manifest["components"] = [c for c in manifest["components"]
                                  if c.get("name") in keep]
    if "loadOrder" in manifest:
        manifest["loadOrder"] = [n for n in manifest["loadOrder"] if n in keep]
    sub["manifest"] = manifest
    return sub


class CompositionWorld(World):
    """The composition's own binding, as the `World` recover replays against.

    Build it with :func:`bind`, use it as a context manager so the providers it
    booted are torn down, and hand it to :func:`revl.recovery.recover`."""

    kind = WORLD_REAL
    #: recover hands this world the discharge-descriptor family as one batch
    replays_descriptors = True
    #: and nothing else: see the module docstring
    re_issues_calls = False

    def __init__(self, *, files: list, digest: str, module: Any, runtime: Any,
                 wal_path: str, services: dict, booted: list,
                 provider_session: Any, cleanup: list) -> None:
        self.files = list(files)
        self.digest = digest
        self.module = module
        self.runtime = runtime
        self.wal_path = wal_path
        self.services = services
        self.booted = list(booted)
        self._provider_session = provider_session
        self._cleanup = cleanup

    def describe(self) -> dict:
        """What the verdict says about the binding it ran through."""
        return {"composition": self.files, "digest": self.digest,
                "booted": self.booted}

    def replay_descriptors(self, descriptors: list) -> dict:
        return self.runtime.replay_descriptors(
            self.module, self.wal_path, descriptors, services=self.services)

    # The referent bookkeeping `DictWorld` keeps is a model's; the real world
    # has nothing to seed and cannot enumerate what is still out there.
    def present(self, referent: str) -> bool:
        return False

    def seed(self, referent: str, value: Any = True) -> None:
        return None

    def remaining(self) -> list:
        return []

    def close(self) -> None:
        session, self._provider_session = self._provider_session, None
        try:
            if session is not None and session.loaded:
                session.unload()
        finally:
            while self._cleanup:
                self._cleanup.pop()()

    def __enter__(self) -> "CompositionWorld":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def check_digest(wal: dict, digest: str, files: list) -> None:
    """Refuse unless the WAL header names this composition."""
    recorded = (wal.get("header") or {}).get("composition")
    named = ", ".join(files)
    if recorded is None:
        raise CompositionMismatch(
            f"the WAL header carries no composition digest, so recover cannot "
            f"tell whether {named} is the composition that wrote it. A log "
            f"written before issue #1477, or opened without an IR, can only be "
            f"recovered against the model (drop --composition).")
    if recorded != digest:
        raise CompositionMismatch(
            f"{named} is not the composition that wrote this WAL: the header "
            f"records {recorded}, {named} compiles to {digest}. Recovering "
            f"through a different composition would replay the log's calls "
            f"against the wrong host bodies.")


def bind(files: list, wal_path: str, *, config: Optional[dict] = None) -> CompositionWorld:
    """Compile ``files``, check them against the WAL at ``wal_path``, load
    their emitted module without activating it, and boot the providers the
    open descriptors call through. Raises :class:`CompositionMismatch` for a
    log the composition did not write, and `RecoveryError` for a composition
    that cannot be bound."""
    from .compiler import compile_files  # noqa: PLC0415
    from .errors import RevlError  # noqa: PLC0415
    from .mcp.session import Session, SessionError, _backend  # noqa: PLC0415
    from .run import _Driver  # noqa: PLC0415
    from .wal import read_wal  # noqa: PLC0415

    try:
        ir = compile_files(list(files))
    except RevlError as error:
        raise RecoveryError(f"cannot compile {', '.join(files)}: {error}") from None
    digest = _replay_module().composition_digest(ir)
    wal = read_wal(wal_path)
    check_digest(wal, digest, list(files))

    try:
        emit, runtime, Context, FiberState = _backend()
    except SessionError as error:
        raise RecoveryError(str(error)) from None
    config = dict(config or {})
    cleanup: list = []
    driver = _Driver(ir, config, emit, runtime, Context, FiberState)
    module = driver._emit_module(ir)
    name = module.__name__

    def _forget_module() -> None:
        if sys.modules.get(name) is module:
            del sys.modules[name]
    cleanup.append(_forget_module)

    session = None
    booted: list = []
    services: dict = {}
    try:
        keys = _open_descriptor_receivers(wal["records"])
        booted = _provider_closure(ir, keys)
        if booted:
            session = Session()
            session.load(_sub_composition(ir, booted), config)
            for key in sorted(keys):
                provider = session._driver.root.get(key)
                if provider is not None:
                    services[key] = provider
    except Exception as error:
        if session is not None and session.loaded:
            session.unload()
        while cleanup:
            cleanup.pop()()
        if isinstance(error, SessionError):
            raise RecoveryError(
                f"cannot boot the providers {', '.join(booted)} the WAL's "
                f"descriptors call through: {error}") from None
        raise
    return CompositionWorld(files=list(files), digest=digest, module=module,
                            runtime=runtime, wal_path=wal_path,
                            services=services, booted=booted,
                            provider_session=session, cleanup=cleanup)
