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
  So a provider whose activation crosses the boundary is NOT booted: it would
  make those crossings a second time. Calls through its key are declined,
  naming the provider and its crossings (`reactivation_refusals`).
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


def _open_calls(records: list) -> dict:
    """``{seq: call}`` for every call the binding could still be asked to
    make: a discharge descriptor, or a legacy boundary effect with a
    reconstructible inverse op. A seq named in a `discharge` or `aborted`
    record is settled and needs nothing."""
    settled: set = set()
    for record in records:
        if record.get("record") == "discharge":
            settled.update(record.get("discharged") or [])
        elif record.get("record") == "aborted":
            settled.update(record.get("replayed") or [])
        elif record.get("record") in ("flushed", "flush-residue"):
            settled.add(record.get("seq"))
    calls: dict = {}
    for r in records:
        seq = r.get("seq")
        if seq in settled:
            continue
        if r.get("record") in ("discharge-descriptor", "deferred-emission"):
            calls[seq] = r.get("call") or {}
        elif r.get("record") == "effect" \
                and (r.get("inverse") or {}).get("reconstructible"):
            calls[seq] = (r.get("inverse") or {}).get("op") or {}
    return calls


def _segments(wal: dict) -> list:
    """The log's openings, in order. The header opens it; every later opening
    (a `--watch` reload, or a later run reusing the file, #641/#642) writes a
    `generation` record naming its own composition digest and the first seq it
    owns."""
    header = wal.get("header") or {}
    segments = [{"opening": 1, "generation": header.get("generation"),
                 "fromSeq": 0, "composition": header.get("composition")}]
    for record in wal.get("records") or []:
        if record.get("record") == "generation":
            segments.append({"opening": len(segments) + 1,
                             "generation": record.get("generation"),
                             "fromSeq": record.get("fromSeq") or 0,
                             "composition": record.get("composition")})
    return segments


def _open_grants(wal: dict) -> dict:
    """``{handle: (grant, segment)}`` for the latest `shared-grant` of every
    handle with no `shared-complete`. A grant carries no seq, so its opening is
    the last one written before it in the file."""
    segments = _segments(wal)
    opening = 0
    latest: dict = {}
    completed: set = set()
    for record in wal.get("records") or []:
        kind = record.get("record")
        current = segments[opening]
        if kind == "generation":
            opening += 1   # `_segments` lists the openings in this same order
        elif kind == "shared-grant":
            latest[record.get("handle")] = (record, current)
        elif kind == "shared-complete":
            completed.add(record.get("handle"))
    return {h: v for h, v in latest.items() if h not in completed}


def _segment_of(segments: list, seq: Any) -> dict:
    owner = segments[0]
    for segment in segments:
        if isinstance(seq, int) and seq >= segment["fromSeq"]:
            owner = segment
    return owner


def _label(segment: dict) -> str:
    return (f"generation {segment['generation']} (opening {segment['opening']} "
            f"of this log, from seq {segment['fromSeq']})")


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
    #: it makes no generic `apply_inverse` / `reissue` call: every family it
    #: performs goes to a runtime entry point, below
    re_issues_calls = False
    #: owed deferred emissions: `runtime.reissue_deferred`
    reissues_deferred = True
    #: `shared` grants' inverses: `runtime.reclaim_shared`
    reclaims_shared = True

    def __init__(self, *, files: list, digest: str, module: Any, runtime: Any,
                 wal_path: str, services: dict, booted: list,
                 provider_session: Any, cleanup: list,
                 foreign: Optional[dict] = None,
                 foreign_handles: Optional[dict] = None,
                 refused: Optional[dict] = None) -> None:
        self.files = list(files)
        self.digest = digest
        self.module = module
        self.runtime = runtime
        self.wal_path = wal_path
        self.services = services
        self.booted = list(booted)
        self._provider_session = provider_session
        self._cleanup = cleanup
        #: seq -> the log opening (generation) that wrote it, for every open
        #: call a different composition wrote. Never handed to the runtime.
        self.foreign = dict(foreign or {})
        #: the same, for `shared` grants (keyed by handle: a grant has no seq)
        self.foreign_handles = dict(foreign_handles or {})
        #: key -> {component, crossings}: a required-service key whose
        #: provider recover will not boot, because its activation (or that of
        #: a component it requires) crosses the boundary and booting it would
        #: cross again. Calls through such a key are declined, by name.
        self.refused = dict(refused or {})

    def describe(self) -> dict:
        """What the verdict says about the binding it ran through."""
        return {"composition": self.files, "digest": self.digest,
                "booted": self.booted,
                "refused": [{"key": key, **why}
                            for key, why in sorted(self.refused.items())],
                "otherGenerations": sorted(
                    {_label(seg) for seg in self.foreign.values()}
                    | {_label(seg) for seg in self.foreign_handles.values()})}

    def declined(self, seq: Any, receiver: Any) -> Optional[str]:
        """The outcome for a call this binding will not make, or ``None``.
        ``other-generation``: another composition wrote it.
        ``would-reactivate``: its receiver's provider was not booted, because
        booting it runs an activation that crosses the boundary again."""
        if seq in self.foreign or seq in self.foreign_handles:
            return "other-generation"
        if receiver in self.refused:
            return "would-reactivate"
        return None

    def _split(self, items: list, ident, receiver) -> tuple:
        mine, outcome = [], {}
        for item in items:
            state = self.declined(ident(item), receiver(item))
            if state is None:
                mine.append(item)
            else:
                outcome[ident(item)] = state
        return mine, outcome

    def _calls(self, descriptors: list) -> tuple:
        return self._split(descriptors, lambda d: d.get("seq"),
                           lambda d: (d.get("call") or {}).get("receiver"))

    def replay_descriptors(self, descriptors: list) -> dict:
        """The runtime's outcomes for the calls this binding makes, and the
        :meth:`declined` outcome for each call it does not hand over."""
        mine, outcome = self._calls(descriptors)
        if mine:
            outcome.update(self.runtime.replay_descriptors(
                self.module, self.wal_path, mine, services=self.services))
        return outcome

    def reissue_deferred(self, descriptors: list) -> dict:
        """Owed deferred emissions through the runtime's own flush
        (`runtime.reissue_deferred`): `flushed` or `flush-residue` per seq. A
        declined call is not fired."""
        mine, outcome = self._calls(descriptors)
        if mine:
            outcome.update(self.runtime.reissue_deferred(
                self.module, self.wal_path, mine, services=self.services))
        return outcome

    def reclaim_shared(self, grants: list) -> dict:
        """`shared` grants' inverses through `runtime.reclaim_shared`, which
        writes the handle's `shared-reclaim-fence` before and `shared-complete`
        after. A declined grant is not reclaimed."""
        mine, outcome = self._split(
            grants, lambda g: g.get("handle"),
            lambda g: (g.get("inverse") or {}).get("receiver"))
        if mine:
            outcome.update(self.runtime.reclaim_shared(
                self.module, self.wal_path, mine, services=self.services))
        return outcome

    def generation_note(self, seq: Any) -> str:
        segment = self.foreign.get(seq, self.foreign_handles.get(seq))
        if segment is None:
            return ""
        return (f"written by {_label(segment)}, composition "
                f"{segment['composition'] or '(no digest)'}; recover it with "
                f"that composition")

    def reactivation_note(self, receiver: Any) -> str:
        why = self.refused.get(receiver)
        if why is None:
            return ""
        return (f"reaching `{receiver}` needs {why['component']}, and booting "
                f"it runs its activation again, which crosses "
                f"{', '.join(why['crossings'])}; recover will not make those "
                f"crossings a second time")

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


def _mismatch(segment: dict, digest: str, named: str) -> str:
    recorded = segment["composition"]
    if recorded is None:
        return (f"{_label(segment)} carries no composition digest, so recover "
                f"cannot tell whether {named} wrote it (a log written before "
                f"issue #1477, or opened without an IR)")
    return (f"{named} is not the composition that wrote {_label(segment)}: "
            f"the log records {recorded}, {named} compiles to {digest}")


def plan_generations(wal: dict, digest: str, files: list) -> dict:
    """Which of the log's open calls this composition may make.

    Every opening of the log names the composition that wrote it. An open call
    is replayed only through its own opening's composition. Returns
    ``{seq: segment}`` for the open calls another composition wrote, which the
    binding refuses call by call. Raises :class:`CompositionMismatch`, naming
    each opening, when the composition wrote none of the open calls, or, with
    nothing open, is not the composition of the log's latest opening."""
    named = ", ".join(files)
    segments = _segments(wal)
    calls = _open_calls(wal.get("records") or [])
    owners = {seq: _segment_of(segments, seq) for seq in calls}
    mine = {seq for seq, seg in owners.items() if seg["composition"] == digest}
    if calls and not mine:
        opened = {seg["opening"]: seg for seg in owners.values()}
        reasons = "; ".join(_mismatch(opened[k], digest, named)
                            for k in sorted(opened))
        raise CompositionMismatch(
            f"{named} wrote none of this WAL's open calls: {reasons}. Recover "
            f"each generation with its own composition; recovering through a "
            f"different one would replay the log's calls against the wrong "
            f"host bodies.")
    if not calls and segments[-1]["composition"] != digest:
        raise CompositionMismatch(
            _mismatch(segments[-1], digest, named) + ". Recovering through a "
            "different composition would replay the log's calls against the "
            "wrong host bodies.")
    return {seq: seg for seq, seg in owners.items() if seq not in mine}


def check_digest(wal: dict, digest: str, files: list) -> None:
    """Refuse unless this composition wrote the log (see
    :func:`plan_generations`)."""
    plan_generations(wal, digest, files)


def compile_composition(files: list) -> tuple:
    """``(ir, digest)`` for ``files``, digested exactly as a WAL header's
    `composition` is (`replay.composition_digest`)."""
    from .compiler import compile_files  # noqa: PLC0415
    from .errors import RevlError  # noqa: PLC0415

    try:
        ir = compile_files(list(files))
    except RevlError as error:
        raise RecoveryError(f"cannot compile {', '.join(files)}: {error}") from None
    return ir, _replay_module().composition_digest(ir)


def emit_unactivated(ir: dict, config: dict, cleanup: list) -> tuple:
    """``(module, runtime)``: the composition's emitted module, loaded through
    the driver's own plug seam (extern config, bound secrets) and NOT
    activated. Forgetting the module is pushed onto ``cleanup``."""
    from .mcp.session import SessionError, _backend  # noqa: PLC0415
    from .run import _Driver  # noqa: PLC0415

    try:
        emit, runtime, Context, FiberState = _backend()
    except SessionError as error:
        raise RecoveryError(str(error)) from None
    driver = _Driver(ir, config, emit, runtime, Context, FiberState)
    module = driver._emit_module(ir)
    name = module.__name__

    def _forget_module() -> None:
        if sys.modules.get(name) is module:
            del sys.modules[name]
    cleanup.append(_forget_module)
    return module, runtime


def open_keys(wal: dict, foreign: dict, grants: dict, foreign_handles: dict) -> set:
    """The required-service keys this composition's open calls and open
    `shared` grants call through."""
    keys = {call.get("receiver")
            for seq, call in _open_calls(wal["records"]).items()
            if seq not in foreign and call.get("receiver") is not None}
    keys |= {(g.get("inverse") or {}).get("receiver")
             for h, (g, _seg) in grants.items()
             if h not in foreign_handles
             and (g.get("inverse") or {}).get("receiver") is not None}
    return keys


def _without_provided_bodies(node: Any) -> Any:
    """``node`` with every `provide` step's method bodies removed, and every
    `compensate` expression: neither runs when a component activates."""
    if isinstance(node, list):
        return [_without_provided_bodies(n) for n in node]
    if not isinstance(node, dict):
        return node
    out = {k: _without_provided_bodies(v) for k, v in node.items()
           if k != "compensate"}
    if node.get("step") == "provide":
        out["methods"] = []
    return out


def activation_crossings(ir: dict, name: str) -> list:
    """What component ``name`` crosses when it boots: every non-pure extern its
    activation body reaches (directly or through functions), and every
    emission method of a required service it calls. Read with `revl audit`'s
    own boundary walk (`boundary._boundary`) over the component's body with its
    provided methods and compensations removed, since booting runs neither.
    Teardown-position crossings stay in: recover unloads what it boots."""
    from .boundary import _boundary  # noqa: PLC0415

    comp = next(c for c in ir.get("components") or [] if c["name"] == name)
    stripped = {**comp, "body": _without_provided_bodies(comp.get("body") or [])}
    report = _boundary({**ir, "components": [stripped]}).get(name) or {}
    crossed = {e["name"] for e in report.get("externs") or []
               if e.get("class") != "pure"}
    crossed |= set(report.get("emissions") or [])
    return sorted(crossed)


def reactivation_refusals(ir: dict, keys: set) -> dict:
    """``{key: {component, crossings}}`` for each key whose provider recover
    must not boot: booting a provider runs its activation, and the activation
    of each component it requires, so any crossing among them would be made a
    second time. The WAL already holds the first."""
    refused: dict = {}
    for key in sorted(keys):
        for name in _provider_closure(ir, {key}):
            crossings = activation_crossings(ir, name)
            if crossings:
                refused[key] = {"component": name, "crossings": crossings}
                break
    return refused


def boot_providers(ir: dict, keys: set, config: dict) -> tuple:
    """``(session, booted, services)``: the components that provide ``keys``,
    and what they require, booted as a composition of their own. Booting a
    provider runs its activation, which is why the verdict names each one."""
    from .mcp.session import Session, SessionError  # noqa: PLC0415

    booted = _provider_closure(ir, keys)
    if not booted:
        return None, [], {}
    session = Session()
    try:
        session.load(_sub_composition(ir, booted), config)
    except Exception as error:
        if session.loaded:
            session.unload()
        if isinstance(error, SessionError):
            raise RecoveryError(
                f"cannot boot the providers {', '.join(booted)} the WAL's "
                f"descriptors call through: {error}") from None
        raise
    services = {}
    for key in sorted(keys):
        provider = session._driver.root.get(key)
        if provider is not None:
            services[key] = provider
    return session, booted, services


def plan_wal(wal_path: str, digest: str, files: list) -> tuple:
    """``(wal, foreign, grants, foreign_handles)`` for one WAL checked against
    this composition (see :func:`plan_generations`)."""
    from .wal import read_wal  # noqa: PLC0415

    wal = read_wal(wal_path)
    foreign = plan_generations(wal, digest, list(files))
    grants = _open_grants(wal)
    foreign_handles = {h: seg for h, (_g, seg) in grants.items()
                       if seg["composition"] != digest}
    return wal, foreign, grants, foreign_handles


def bind(files: list, wal_path: str, *, config: Optional[dict] = None) -> CompositionWorld:
    """Compile ``files``, check them against the WAL at ``wal_path``, load
    their emitted module without activating it, and boot the providers the
    open descriptors call through. Raises :class:`CompositionMismatch` for a
    log the composition did not write, and `RecoveryError` for a composition
    that cannot be bound."""
    ir, digest = compile_composition(files)
    wal, foreign, grants, foreign_handles = plan_wal(wal_path, digest, files)
    config = dict(config or {})
    cleanup: list = []
    module, runtime = emit_unactivated(ir, config, cleanup)
    keys = open_keys(wal, foreign, grants, foreign_handles)
    refused = reactivation_refusals(ir, keys)
    try:
        session, booted, services = boot_providers(
            ir, keys - set(refused), config)
    except Exception:
        while cleanup:
            cleanup.pop()()
        raise
    return CompositionWorld(files=list(files), digest=digest, module=module,
                            runtime=runtime, wal_path=wal_path,
                            services=services, booted=booted,
                            provider_session=session, cleanup=cleanup,
                            foreign=foreign, foreign_handles=foreign_handles,
                            refused=refused)
