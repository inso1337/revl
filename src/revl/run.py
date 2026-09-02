"""Boot a compiled composition on a Cordis runtime.

Run: ``revl run app.rvl`` loads the composition, streams the lifecycle/host
trace, holds it live with an interactive REPL over its provided services, and
on EOF/Ctrl-C tears down and reports no-residue. With stdin closed (``<
/dev/null``) the same command completes the round-trip non-interactively.
Flags: ``--watch`` recompiles on source change (a rejected or mis-hosted edit
never touches the running generation); ``--plan`` prints the load plan without
a runtime; ``--record`` powers the backwards-replay REPL commands
(docs/replay.md); ``--placement`` splits the composition across processes.

Admission is the host's job: ``--config FILE`` supplies the per-component
config each component declares, and a component admitted with a missing
*required* field (IR ``config`` with ``default: null``) refuses the run
*before* any runtime is loaded — with the diagnostic available even on an
interpreter that has no cordis at all.

The frontend stays pure: the backend emitter and the cordis-py runtime are
imported lazily, inside ``run_command``, so ``compile``/``audit``/``test``/
``fmt`` and ``run --plan`` all work with no runtime installed. ``py`` is the
runnable in-process tier; ``ts``, ``rust``, ``java``, ``wasm`` and ``go`` are
runnable too, each as a separate process over the bridge seam (see
:mod:`revl.run_ts`, :mod:`revl.run_rust`, :mod:`revl.run_java`,
:mod:`revl.run_wasm`, :mod:`revl.run_go` — --once boots and tears down a real
composition with a no-residue proof, behind a per-tier runtime-availability
gate).
"""

from __future__ import annotations

import asyncio
import builtins
import json
import os
import signal
import sys
import time
import types
from pathlib import Path

from . import hostref as _hostref
from ._paths import backends_root
from .compiler import compile_files
from .holes import refuse_admission
from .errors import RevlError
from . import diagnostics
from . import taint
from . import why_runtime

KNOWN_BACKENDS = ("py", "ts", "rust", "java", "wasm", "go")


def _host_usage(raw) -> tuple:
    """Item 121: the HOST-REPORTED model/tokens/cost, best-effort read off a
    model completion's return value (§2.1, "whatever usage metadata the host
    ``make_call`` return carries back from the provider").

    Recognized ONLY when the return is a mapping carrying the conventional keys
    (top-level, or a nested ``usage`` object for the token counts); anything
    else honest-degrades to ``None`` for every field — the hop is still recorded
    (with `latency`/`attempts`, the revl-owned numbers), the host fields simply
    absent. These values are unverifiable (§4 attack 2); `revl_model_hop` tags
    them `host-reported`, so a passthrough here promotes nothing to ground
    truth. Returns ``(model, tokens_in, tokens_out, cost)``."""
    if not isinstance(raw, dict):
        return None, None, None, None
    usage = raw.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    tokens_in = raw.get("tokensIn", usage.get("tokensIn"))
    tokens_out = raw.get("tokensOut", usage.get("tokensOut"))
    cost = raw.get("cost")
    cost = cost if isinstance(cost, dict) else None
    return raw.get("model"), tokens_in, tokens_out, cost


class ActivationError(RuntimeError):
    """A component's activation did not complete — "loaded" would be a lie
    (roadmap item 372).

    `ctx.plugin()` defers the component apply to the event loop via a `_LazyTask`
    whose first statement is `await asyncio.sleep(0)`. On an offloaded/closing-
    loop path (item 115's synchronous-`@py`-body `http_post` pins the single
    loop, so dispatch is offloaded to a thread running `asyncio.run` with a
    main-side timeout) the per-turn loop can close underneath that deferred
    activation and SILENTLY cancel it — and because `str(CancelledError)` is the
    empty string there is no diagnostic. The plugin is then left listed as
    LOADED while `ROOT.get(key)` stays `None` forever.

    The driver refuses that: a key that did not finish activating is never
    counted loaded, and a cancelled/failed activation is surfaced LOUDLY through
    this typed error naming the component and the unresolved key(s) — never a
    silent empty string.
    """

    def __init__(self, component: str, keys, reason: str) -> None:
        self.component = component
        self.keys = list(keys)
        self.reason = reason
        keys_txt = ", ".join(self.keys) if self.keys else "(no provided keys)"
        super().__init__(
            f"activation of {component!r} did not complete "
            f"[{keys_txt}]: {reason}")

# fiber states that count as "settled" — a lifecycle transition has come to
# rest, so it is worth one causal-trace record. UNLOADING/LOADING are
# in-flight and never recorded on their own.
_SETTLED_DOWN = ("DISPOSED", "PENDING", "FAILED")
# py boots cordis-py in-process (the _Driver below); ts/rust/java/wasm/go each
# boot the composition as a *separate process* over the same bridge seam
# (run_ts.py, run_rust.py, run_java.py, run_wasm.py, run_go.py): ts a node
# process on cordis v4, rust a cordis-rs binary, java a JVM on cordis4j, wasm
# the cordis-wasm runtime on wasmtime, go the stc-go placement runner. On every
# non-py tier --once (boot -> LIFO teardown -> no-residue proof -> exit) is
# wired, each behind its own runtime-availability gate (skip-with-reason, never
# a green run that booted nothing).
RUNNABLE_BACKENDS = ("py", "ts", "rust", "java", "wasm", "go")


# --------------------------------------------------------------------------
# helpers that need no runtime
# --------------------------------------------------------------------------


def _load_config(path: str | None) -> dict:
    """component-name -> config dict, from a TOML or JSON file (optional)."""
    if not path:
        return {}
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    if source.suffix == ".json":
        data = json.loads(text)
    else:
        import tomllib  # stdlib, py3.11+

        data = tomllib.loads(text)
    if not isinstance(data, dict) or any(not isinstance(v, dict) for v in data.values()):
        # `RevlError` is a diagnostic, not a bare message: its signature is
        # (filename, line, message) and it renders `<file>:<line>: <message>`.
        # Handing it a single string raises `TypeError` instead — and this
        # branch only runs once the config is ALREADY malformed, so the bug
        # surfaced as a traceback that the `except RevlError` in
        # `run_command` could never catch.
        raise RevlError(
            path, 0,
            "expected a table of `component-name = { ... }` entries",
        )
    return data


def _components(ir: dict) -> list[dict]:
    return ir.get("components") or []


def _required_config_problem(ir: dict, config: dict) -> str | None:
    """The mis-hosted required-config fields, as a diagnostic, or ``None``.

    A component's IR ``config`` list is the schema it runs against: a field
    with ``default: null`` is *required* (the same rule the runtime enforces
    in backends/python/runtime.py, :class:`ConfigSchema`). The CLI preflights
    that list before any runtime is imported, for two reasons:

    * a component admitted with a missing required field should fail the run
      *loudly and at admission*, not boot a fiber that silently settles onto
      ``FAILED`` while the rest of the composition waits on it, and
    * the diagnostic must work with no cordis installed at all — config is a
      host concern (DESIGN §2 non-goals), not a runtime one.

    The runtime stays the authority for everything else (type mismatches,
    mid-body failures); the preflight only mirrors the deterministic
    required-ness carried in the IR itself.
    """
    by_name = {c["name"]: c for c in _components(ir)}
    missing: list[str] = []
    for name in _load_order(ir):
        fields = (by_name.get(name) or {}).get("config") or []
        supplied = config.get(name) or {}
        for field in fields:
            if field.get("default") is None and field["name"] not in supplied:
                missing.append(
                    f'{name} is missing required config "{field["name"]}"'
                    f" ({field.get('type') or '?'})"
                )
    # item 379 / option (b) of docs/design/378-sync-extern-service-reach.md: a
    # config extern is a document-global mechanism configured through the SAME
    # --config seam a component is, keyed by extern name. Preflight its required
    # fields (IR `config` with `default: null`) here, so a missing one fails
    # loudly at admission — never as a runtime KeyError inside the host body.
    for ext in _config_externs(ir):
        supplied = config.get(ext["name"]) or {}
        for field in ext.get("config") or []:
            if field.get("default") is None and field["name"] not in supplied:
                missing.append(
                    f'extern {ext["name"]} is missing required config "{field["name"]}"'
                    f" ({field.get('type') or '?'})"
                )
    if not missing:
        return None
    rendered = "\n".join(f"  - {entry}" for entry in missing)
    return (
        "invalid config:\n"
        f"{rendered}\n"
        "  components are declarations — supply their config with "
        "--config <file>."
    )


def _config_externs(ir: dict) -> list[dict]:
    """The document-global externs that declare a typed `config` schema
    (item 379). Absent/empty for every composition that uses none, so the
    driver's config path is byte-identical there."""
    return [e for e in (ir.get("externs") or []) if e.get("config")]


def _resolve_extern_config(ir: dict, config: dict) -> dict:
    """Resolve each config extern's schema once, from the composition config
    map (item 379), merging supplied values over the declared defaults. This is
    the plug-time seam: the value is fixed here, then installed into the emitted
    module's `_REVL_EXTERN_CONFIG` for the host body to read as `_revl_config`.

    Required-ness was already preflighted by :func:`_required_config_problem`;
    this only materializes the resolved dicts (defaults + supplied)."""
    resolved: dict[str, dict] = {}
    for ext in _config_externs(ir):
        supplied = config.get(ext["name"]) or {}
        merged = {
            field["name"]: field.get("default")
            for field in ext.get("config") or []
            if field.get("default") is not None
        }
        merged.update(supplied)
        resolved[ext["name"]] = merged
    return resolved


def _secret_rows(ir: dict) -> list[dict]:
    """The bound secrets a composition declares (item 256): name + capability
    rows, never a value (the value lives only in the driver's `_REVL_SECRETS` at
    run time). Empty for every composition that binds none, so the driver's
    secret path is byte-identical there."""
    return list(ir.get("secrets") or [])


def _resolve_secrets(ir: dict, source: dict | None = None) -> dict:
    """Resolve each bound secret's VALUE once at plug (item 256, §3), keyed by the
    secret NAME. Sourced from `source` (a caller-supplied name->value store) or,
    by default, the environment variable `REVL_SECRET_<NAME>` (NAME upper-cased),
    so a secret store or a `--secret-file` seam can front it later without moving
    the injection point.

    Fail-loud, no defaults: a declared secret with no resolvable value RAISES here
    at plug, naming the secret but NEVER its value - there is no silent fallback
    for a provider key (unlike config's defaults path). The resolved value is
    installed into the emitted module's `_REVL_SECRETS` and never logged, never
    echoed by `--plan`, and never serialized into any trace."""
    rows = _secret_rows(ir)
    if not rows:
        return {}
    resolved: dict[str, str] = {}
    for row in rows:
        name = row["name"]
        if source is not None and name in source:
            resolved[name] = source[name]
            continue
        env_key = "REVL_SECRET_" + name.upper()
        if env_key in os.environ:
            resolved[name] = os.environ[env_key]
            continue
        # never name the value or a guessed default - a bound key has none.
        raise RuntimeError(
            f"capability-bound secret `{name}` has no value at plug: set the "
            f"environment variable `{env_key}` (or supply it through the driver's "
            f"secret source). There is no default for a secret (item 256).")
    return resolved


def _load_order(ir: dict) -> list[str]:
    manifest = ir.get("manifest") or {}
    return manifest.get("loadOrder") or [c["name"] for c in _components(ir)]


def _key_to_service(ir: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for comp in _components(ir):
        for key, service in (comp.get("provides") or {}).items():
            out[key] = service
    return out


def _print_plan(ir: dict, config: dict, backend: str) -> None:
    order = _load_order(ir)
    by_name = {c["name"]: c for c in _components(ir)}
    print(f"backend: {backend}")
    print(f"load order (providers first): {' -> '.join(order) or '(none)'}")
    for name in order:
        comp = by_name[name]
        requires = ", ".join(comp.get("requires") or {}) or "-"
        provides = ", ".join(comp.get("provides") or {}) or "-"
        cfg = config.get(name)
        print(f"\ncomponent {name}")
        print(f"  requires: {requires}")
        print(f"  provides: {provides}")
        print(f"  config:   {cfg if cfg else '(none)'}")
    # item 379: document-global config externs are configured at the same seam,
    # so list them alongside components when a composition declares any.
    for ext in _config_externs(ir):
        cfg = config.get(ext["name"])
        fields = ", ".join(f["name"] for f in ext.get("config") or [])
        print(f"\nextern {ext['name']} (config: {fields})")
        print(f"  config:   {cfg if cfg else '(none)'}")
    keys = _key_to_service(ir)
    services = ir.get("services") or {}
    print("\ncallable in the REPL:")
    if not keys:
        print("  (this composition provides no services)")
    for key in sorted(keys):
        methods = list(((services.get(keys[key]) or {}).get("methods") or {}).keys())
        print(f"  {key}: {keys[key]}  ->  {', '.join(methods) or '(no methods)'}")


_REPLAY_COMMANDS = (":timeline", ":inspect", ":back", ":forward", ":bisect")


def _replay_command(line: str):
    """Parse a REPL replay command (docs/replay.md §6).

    Returns ``(op, at, force, component)`` or ``None`` when the line is not a
    replay command. Kept free of any runtime so it can be tested on its own.

        :timeline [component]
        :inspect <k> [component]
        :back <k> [!] [component]      -- `!` forces across uncompensated emissions
        :forward <k> [component]
        :bisect [@component] <predicate expression>

    For ``:bisect`` the ``at`` slot carries the predicate string (a free-form
    expression, not a step index), the component comes from an optional leading
    ``@name`` token, and ``force`` is unused.
    """
    parts = line.split()
    if not parts or parts[0] not in _REPLAY_COMMANDS:
        return None
    if parts[0] == ":bisect":
        rest = line[len(":bisect"):].strip()
        component = None
        if rest.startswith("@"):
            head, _, rest = rest.partition(" ")
            component = head[1:] or None
            rest = rest.strip()
        if not rest:
            raise ValueError(":bisect needs a predicate expression "
                             "(e.g. `:bisect emissionsSoFar`)")
        return "bisect", rest, False, component
    op, rest = parts[0][1:], parts[1:]
    force = "!" in rest
    rest = [part for part in rest if part != "!"]
    at = None
    if op != "timeline":
        if not rest:
            raise ValueError(f":{op} needs a step index (-1 means "
                             f"'before every step')")
        try:
            at = int(rest[0])
        except ValueError:
            raise ValueError(f":{op} needs an integer step index, "
                             f"got {rest[0]!r}") from None
        rest = rest[1:]
    return op, at, force, (rest[0] if rest else None)


# --------------------------------------------------------------------------
# runtime routing — the load-balancer pattern (docs/distribution-model.md)
# --------------------------------------------------------------------------


class NoLiveWorker(RuntimeError):
    """Every realm a router fans out to has withdrawn — the route has no live
    leg left to serve. A *runtime* exhaustion of a pool that verified cleanly
    (G2 held at link time; the workers have since all gone down), not a
    compile-time diagnostic — hence a plain ``RuntimeError``, surfaced through
    the REPL's error channel like any other live-call failure."""


class _Router:
    """The runtime realization of a multi-realm bind (item 162's ``routes`` IR)
    for one routed key — the ``Router`` component of docs/distribution-model.md,
    py-tier reference.

    A component that ``requires <key> in realms("w1"…"wN") strategy(...)`` and
    ``provides <key>`` fans one required key out across N worker realms while
    still presenting **one** provider downstream (G2 holds: consumers resolve
    exactly this proxy). The proxy holds no worker handle of its own — it
    **re-resolves the live per-realm handle on every call** off the same
    committed-view lookup a normal ``requires`` uses
    (``ctx.isolate(key, realm(w)).reflect.get(key)``), which returns ``None`` for
    any realm whose provider is not ACTIVE. So a withdrawn worker (peer-death →
    provider-withdrawal → the R2/R3 reactive coeffect the runtime already runs)
    simply drops out of the live set and its calls go to the survivors — reactive
    failover with no extra machinery, exactly the resolution model in
    docs/distribution-model.md §3. A replacement that re-provides the key in that
    realm re-enters the rotation on its next turn, for free.

    Strategies (a closed set, ``lower.KNOWN_STRATEGIES``):

    * ``round_robin`` (the default when ``strategy`` is omitted) — rotate across
      the realms in declaration order, skipping any that are not currently live;
    * ``least_loaded`` — route to the live realm this proxy has served the fewest
      calls (a local served-count proxy for load; a real load signal off a worker
      coeffect is a follow-up — see the docs note).

    The proxy is duck-typed against the routed service: ``proxy.get(k)`` picks a
    worker and forwards ``get(k)`` to it. ``__getattr__`` refuses ``_``-prefixed
    names so cordis's traceable probe (``__cordis_tracker__``) sees no tracker and
    passes the proxy through unwrapped."""

    def __init__(self, root, runtime_mod, key, realms, strategy, log=None):
        self._root = root
        self._runtime = runtime_mod
        self._key = key
        self._realms = list(realms)
        self._strategy = strategy or "round_robin"
        self._cursor = 0
        self._served = {realm: 0 for realm in self._realms}
        self._log = log

    def _handle(self, realm):
        """The live provider handle for ``key`` in ``realm``, or ``None`` when
        that realm has no ACTIVE provider (cordis's strict ``reflect.get``)."""
        scoped = self._root.isolate(self._key, self._runtime.realm_label(realm))
        return scoped.reflect.get(self._key)

    def _live(self):
        """(realm, handle) for every realm with a live provider, declaration
        order preserved."""
        out = []
        for realm in self._realms:
            handle = self._handle(realm)
            if handle is not None:
                out.append((realm, handle))
        return out

    def _select(self):
        live = self._live()
        if not live:
            raise NoLiveWorker(
                f"router for `{self._key}` has no live worker: all "
                f"{len(self._realms)} realm(s) "
                f"({', '.join(self._realms)}) have withdrawn")
        if self._strategy == "least_loaded":
            realm, handle = min(live, key=lambda rh: self._served[rh[0]])
        else:  # round_robin — next live realm in declaration order
            n = len(self._realms)
            realm = handle = None
            for offset in range(n):
                cand = self._realms[(self._cursor + offset) % n]
                match = next((rh for rh in live if rh[0] == cand), None)
                if match is not None:
                    self._cursor = (self._cursor + offset + 1) % n
                    realm, handle = match
                    break
        self._served[realm] += 1
        return realm, handle

    def __getattr__(self, method):
        # `_`-prefixed lookups (notably cordis's `__cordis_tracker__` probe) are
        # never routed service ops — refuse them so the proxy is passed through
        # raw rather than wrapped as a traceable service.
        if method.startswith("_"):
            raise AttributeError(method)

        def call(*args):
            realm, handle = self._select()
            if self._log is not None:
                self._log(realm, method)
            return getattr(handle, method)(*args)

        return call


# --------------------------------------------------------------------------
# the runtime driver (py tier)
# --------------------------------------------------------------------------


class _Driver:
    """Boots and holds one composition on a cordis.Context, with a compact
    trace, an interactive REPL, and a no-residue teardown. Modeled on
    demo/live.py, generalized off the IR manifest."""

    def __init__(self, ir, config, emit, runtime_mod, Context, FiberState,
                 record: bool = False, trace_path: str | None = None,
                 withdraw: str | None = None, wal_path: str | None = None,
                 root_dirs: list | None = None, secrets: dict | None = None):
        self.ir = ir
        self.config = config
        # item 256 Slice 1: an optional caller-supplied secret store (name ->
        # value), consulted before the environment at plug. `None` means "source
        # every bound secret from the environment" (`REVL_SECRET_<NAME>`). Never
        # logged, never echoed by `--plan`.
        self.secrets = secrets
        self.emit = emit
        # item 396 option B: the composition's root compile directories, the
        # import roots a `= @py ref` file must be reachable through at deploy.
        # `_emit_module` APPENDS them to sys.path (never prepends) and hash-checks
        # each ref, only when the IR carries refs — a ref-free run never touches
        # sys.path (byte-identical driver behaviour).
        self.root_dirs = list(root_dirs or [])
        self.runtime = runtime_mod
        self.FiberState = FiberState
        self.root = Context()
        self.fibers: dict[str, object] = {}
        # runtime routing (docs/distribution-model.md): a component carrying the
        # `routes` IR (item 162's multi-realm bind) is realized here, not as a
        # plugged fiber — the driver resolves its N per-realm handles and
        # installs a `_Router` provision. Its provide-effect disposers are kept
        # per component so teardown withdraws them at the component's own LIFO
        # position (keeping the no-residue proof intact).
        self.routers: dict[tuple, _Router] = {}
        self._route_disposers: dict[str, list] = {}
        self.generation = 0
        self.emitted: tuple = ("", "")  # (filename, source) of the last emit
        # crash-recovery WAL (roadmap item 47): --wal implies recording, since
        # the accumulator it persists is what recording captures.
        self.wal_path = wal_path
        self.recorder = self._make_recorder() if (record or wal_path) else None

        # -- causal trace (docs/why-runtime.md) ----------------------------
        # `trace_path`/`withdraw` turn on the causal-trace recorder: every
        # settled lifecycle transition is written with the cause chain behind
        # it, so `revl why <c> --trace run.jsonl` can explain the run post
        # hoc, and the withdrawal oracle can diff prediction vs actuality.
        self.trace_path = trace_path
        self.withdraw = withdraw
        self.tracing = trace_path is not None or withdraw is not None
        self._events: list[dict] = []
        self._seq = 0
        # while non-None, `_on_fiber` records the transitions it observes into
        # this list (name, from_state, to_state), in the order they settle
        self._observing: dict | None = None
        self._settled: list[tuple] = []

        self.runtime.set_trace(self._on_host)
        self.root.on("internal/status", self._on_fiber)
        self._baseline_hooks = self._hooks()
        self._baseline_disposables = self.root.fiber._disposables.length

    # -- backwards replay (docs/replay.md) ---------------------------------

    def _make_recorder(self):
        from .mcp.session import replay_module  # noqa: PLC0415 — lazy, no cordis

        return replay_module().Recorder(self.ir)

    def _commit_wal(self) -> None:
        """Activation finished cleanly — stamp the WAL's `activation-complete`
        marker (its presence is what a restart reads as roll-forward)."""
        if self.recorder is not None and self.wal_path is not None:
            self.recorder.commit_wal([c["name"] for c in _components(self.ir)])
            self._log("wal", "activation-complete",
                      f"{self.wal_path} — recoverable (docs/crash-recovery.md)")

    # -- trace -------------------------------------------------------------

    def _log(self, channel: str, subject: str, detail: str = "") -> None:
        print(f"  {channel:<7}| {subject:<18}| {detail}".rstrip(), flush=True)

    def _on_host(self, event: str) -> None:
        head, _, rest = event.partition(" ")
        self._log("host", head, rest)

    def _on_fiber(self, _this, fiber, old) -> None:
        new = self.FiberState(fiber.state).name
        self._log("fiber", fiber.name, f"{self.FiberState(old).name} -> {new}")
        # observation window (open during a `--withdraw`): capture each fiber
        # the moment it comes to rest in a down state. `from` is the fiber's
        # state at the START of the window, so a UNLOADING waypoint collapses
        # into the single ACTIVE -> DISPOSED/PENDING move the trace records.
        if self._observing is not None and new in _SETTLED_DOWN:
            frm = self._observing.get(fiber.name, self.FiberState(old).name)
            # capture the fiber's error only for a FAILED settle — it is what
            # the failure-code classification (v2) reads. `_error` is cordis's
            # own attribute (fiber.py); a non-FAILED settle carries none.
            err = getattr(fiber, "_error", None) if new == "FAILED" else None
            self._settled.append((fiber.name, frm, new, err))

    # -- causal trace ------------------------------------------------------

    def _record(self, event: str, component: str, transition: str,
                cause: dict) -> None:
        # ts (v2): a monotonic reading, meaningful for durations within the run.
        self._events.append(why_runtime.make_event(
            self._seq, self.generation, event, component, transition, cause,
            ts=time.monotonic()))
        self._seq += 1

    def _record_emit(self, component: str, capability: str, key: str,
                     cause: dict, *, llm: dict | None = None,
                     activation_id: str | None = None) -> None:
        """Record one ``emit`` event (v2): an emission crossed an irreversible
        boundary at runtime (the ``emissionsCrossed`` site in a step-back).

        `llm`/`activation_id` (item 121, both additive and omit-by-default) are
        supplied ONLY for a model-completion crossing, from
        :meth:`_model_crossing_payload`. A non-model crossing passes neither, so
        its record is byte-identical to a pre-121 v2 emit — the same additive
        discipline `make_emit_event` already documents."""
        self._events.append(why_runtime.make_emit_event(
            self._seq, self.generation, component, capability, key, cause,
            ts=time.monotonic(), llm=llm, activation_id=activation_id))
        self._seq += 1

    def _model_crossing_payload(self, *, activation_id: str | None = None,
                                args=None, arg_origins=None,
                                taint_engaged: bool = False,
                                verified_by=None) -> dict | None:
        """Item 121: assemble the `llm` payload for a model-completion crossing,
        or ``None`` when this crossing is not one.

        The completion fires at exactly one runtime seam — ``validate_retry``
        (`backends/python/runtime.py`) — which stashes a fiber-local observation
        ``(latencySeconds, attempts, attemptCeiling, rawReturn)``. This reads
        (and consumes) it via ``revl_take_model_call()``: a present observation
        IS the model-hop discriminator, so a NON-model crossing (nothing stashed)
        returns ``None`` and its record stays byte-identical.

        From the observation this threads the revl-OWNED numbers (the latency
        bracket, the attempt count against the item-257 static ``N + 1`` ceiling)
        straight through; the HOST-reported model/tokens/cost are a best-effort
        passthrough off the return (:func:`_host_usage`). The salted, suppressible
        `promptDigest` is computed over the crossing's revl-typed `args` (never
        the host string) and the `producedSeq` value-flow edge is resolved via
        the fiber-local token — present ONLY when the token's activation id
        matches (`revl_produced_seq`), OMITTED (never adjacency-guessed) else.
        Digest/token/host-usage all honest-degrade to absent; the assembly and
        every provenance tag live in `revl_model_hop`."""
        take = getattr(self.runtime, "revl_take_model_call", None)
        obs = take() if take is not None else None
        if obs is None:
            return None
        latency, attempts, ceiling, raw = obs
        model, tokens_in, tokens_out, cost = _host_usage(raw)
        digest = None
        if args is not None:
            digest = self.runtime.revl_prompt_digest(
                args, arg_origins, taint_engaged)
        produced = self.runtime.revl_produced_seq(activation_id)
        return self.runtime.revl_model_hop(
            model=model, tokens_in=tokens_in, tokens_out=tokens_out, cost=cost,
            latency_seconds=latency, attempts=attempts, attempt_ceiling=ceiling,
            verified_by=verified_by or [], produced_seq=produced,
            prompt_digest=digest)

    def _crossing_taint(self, component) -> tuple:
        """Item 444: the compile-side taint facts a crossing in `component`
        hands to the item-121 digest gate — ``(arg_origins, taint_engaged)``.

        This IS the compile-to-runtime taint-origin channel. The checker's
        per-crossing origins (`comp["taint"]["reaches"]`, item 249/256) and the
        composition's `Secret[T]` / bound-secret declarations both already ride
        the IR document this driver holds, so :class:`taint.OriginIndex` reads
        them back with no new IR key and no golden churn; see its docstring for
        why the `taint_engaged` certificate is whole-program rather than
        per-crossing.

        FAIL-CLOSED on every path this does not prove: no IR, an IR shape the
        index cannot read, or any declared confidentiality surface all yield
        ``(None, False)``, which `revl_prompt_digest` suppresses. The index is
        rebuilt whenever `self.ir` is replaced (a `--watch` generation), keyed
        on the document's identity."""
        ir = getattr(self, "ir", None)
        cached = getattr(self, "_origin_index", None)
        if cached is None or cached[0] is not ir:
            cached = (ir, taint.OriginIndex(ir))
            self._origin_index = cached
        index = cached[1]
        return index.origins_for(component), index.engaged

    @staticmethod
    def _failure_code(err) -> str | None:
        """The diagnostic code for a FAILED settle's error, or ``None`` when it
        carries no classifiable :class:`RevlError` (a bare crash) — in which
        case the code is *omitted*, never fabricated, and the consumer buckets
        the failure as unclassified. Uses ``diagnostics.classify`` read-only."""
        if not isinstance(err, RevlError):
            return None
        return diagnostics.classify(err).get("code")

    @classmethod
    def _withdraw_cause(cls, name: str, component: str, to: str, err,
                        cascade_causes: dict) -> dict:
        """The cause a settled withdrawal carries (v2-aware, pure).

        The withdrawn `component` roots at the operator `trigger`; every
        dependent keeps its `provider-withdrawn` edge (read from the static
        prediction, so the oracle can never disagree with it). When the settle
        is into ``FAILED`` and its error classifies, the diagnostic `code` is
        attached to that cause *without changing its kind* — the causal edge is
        unchanged, the code is extra detail on how it went down. A FAILED with
        no classifiable error omits `code` (never fabricated)."""
        code = cls._failure_code(err) if to == "FAILED" else None
        if name == component:
            return why_runtime.cause_trigger(
                f"withdrawn by operator (revl run --withdraw {component})",
                code=code)
        base = cascade_causes.get(name)
        if base is not None:
            cause = dict(base)  # keep the provider-withdrawn edge intact
            if code is not None:
                cause["code"] = code
            return cause
        return why_runtime.cause_provider_withdrawn(component, "?", code=code)

    def _hooks(self) -> dict:
        return {name: len(cbs) for name, cbs in self.root.events._hooks.items() if cbs}

    async def _flush(self) -> None:
        for _ in range(20):
            await asyncio.sleep(0)

    # -- emit + load -------------------------------------------------------

    def _emit_module(self, ir: dict) -> types.ModuleType:
        self.generation += 1
        # item 416d: reset the per-run trace state at the ONE place a generation
        # begins. `revl_reset_run_trace_state`'s contract has always read
        # "called at run start"; until now only tests called it, so the item-121
        # digest nonce and the fiber-local model-hop registers survived for the
        # process lifetime. Under `--watch` that is a real leak across
        # generations: a completion observed in gen N and never consumed by a
        # crossing is still stashed when gen N+1 boots, and the first emit
        # crossing of the NEW program consumes it and is recorded as a model
        # hop it never made. Resetting per generation also gives each generation
        # its own digest salt, so two runs of two different programs cannot be
        # correlated by digest equality. Guarded: a custom runtime module need
        # not carry the seam.
        reset = getattr(self.runtime, "revl_reset_run_trace_state", None)
        if reset is not None:
            reset()
        source = self.emit.emit(ir)
        module = types.ModuleType(f"revl_run_gen{self.generation}")
        filename = f"<revl-run gen{self.generation}>"
        # kept so a replay recorder can quote the emitted line a step came
        # from — exec'd modules are invisible to linecache
        self.emitted = (filename, source)
        # item 396 option B: at plug of a ref-carrying IR, prepare the host-import
        # boundary BEFORE the module executes any extern — append the import
        # root(s) to sys.path (append-only, so a project file cannot shadow a
        # trusted runtime module), hash-check each ref's file against the IR pin
        # (refuse a swap/shadow), and evict stale sys.modules entries so a replug
        # runs the NEW code. No-op (and no sys.path touch) when the IR has no ref.
        _hostref.plug_refs(ir, self.root_dirs)
        # register before exec: emitted record types are @dataclass, and
        # dataclasses resolves fields via sys.modules[cls.__module__]
        sys.modules[module.__name__] = module
        exec(compile(source, filename, "exec"), module.__dict__)
        # item 379 / option (b) of docs/design/378-sync-extern-service-reach.md:
        # install the resolved config for each document-global config extern into
        # the module's `_REVL_EXTERN_CONFIG` map, so a host body reads typed
        # `_revl_config` at the sanctioned plug seam — resolved ONCE here, not per
        # call. A composition with no config extern leaves the map (which the
        # emitter only defines when one exists) untouched.
        extern_config = _resolve_extern_config(ir, self.config)
        if extern_config and hasattr(module, "_REVL_EXTERN_CONFIG"):
            module._REVL_EXTERN_CONFIG.update(extern_config)
        # item 256 Slice 1: resolve each bound secret's value once at plug and
        # install it into the module's `_REVL_SECRETS` map, so a bound extern body
        # reads the key as its first local at the sanctioned seam - resolved ONCE
        # here, never per call, never logged. A composition with no bound secret
        # leaves the map (which the emitter only defines when one exists)
        # untouched. Sourced from `self.secrets` (the caller-supplied store) or the
        # environment; fail-loud when a declared secret has no value.
        secrets = _resolve_secrets(ir, getattr(self, "secrets", None))
        if secrets and hasattr(module, "_REVL_SECRETS"):
            module._REVL_SECRETS.update(secrets)
        if self.recorder is not None:
            # between emit and plugin: recording replaces each component's
            # `apply`, and the fiber's context chain is fixed at plugin time
            self.recorder.register_source(filename, source)
            self.recorder.activation_origin()
            self.recorder.timelines.clear()
            self.recorder.instrument(module, ir)
            if self.wal_path is not None:
                # open the WAL before activation, so each effect is written
                # ahead of it mattering (docs/crash-recovery.md)
                self.recorder.open_wal(self.wal_path, self.generation)
        return module

    async def _load(self, ir: dict, module: types.ModuleType) -> None:
        by_name = {c["name"]: c for c in _components(ir)}
        load_causes = why_runtime.load_causes(ir) if self.tracing else {}
        for name in _load_order(ir):
            comp = by_name[name]
            requires = ", ".join(comp.get("requires") or {}) or "-"
            provides = ", ".join(comp.get("provides") or {}) or "-"
            self._log("load", name, f"requires {requires} · provides {provides}")
            if comp.get("routes"):
                # a router (item 162 multi-realm bind) is realized by the driver,
                # not plugged as a fiber: its routed require has no single-realm
                # provider (the workers live in the named realms), so an emitted
                # body would sit PENDING forever. Install the `_Router` provision
                # instead — it provides the key downstream (G2) and fans calls out
                # across the worker realms (docs/distribution-model.md).
                self._install_router(name, comp)
                if self.tracing:
                    self._record(why_runtime.LOAD, name, "PENDING -> ACTIVE",
                                 load_causes.get(name, why_runtime.cause_boot()))
                await self._flush()
                continue
            # realm-aware plug: apply each `isolate <key> in realm(...)` placement
            # before ctx.plugin (runtime.plug), so a worker's provision lands in
            # its named realm — the routes above resolve against exactly those
            # realms. For a component with no placements this is byte-identical to
            # a bare ctx.plugin.
            fiber = self.runtime.plug(
                self.root, getattr(module, name), self.config.get(name, {}))
            self.fibers[name] = fiber
            await self._flush()
            # roadmap item 372: DRIVE the deferred activation to completion and
            # refuse to count the key loaded unless its provision actually
            # landed — "loaded means loaded". Loud on cancel/failure.
            await self._drive_activation(name, comp, fiber, requires)
            if self.tracing:
                # a component comes up because its providers were already up
                # (load order is providers-first); a component with no
                # resolved injection roots at boot. The cause is read from the
                # linked provider graph, the transition from what actually
                # happened to the fiber.
                self._record(why_runtime.LOAD, name,
                             f"PENDING -> {self.FiberState(fiber.state).name}",
                             load_causes.get(name, why_runtime.cause_boot()))
        await self._flush()

    async def _drive_activation(self, name: str, comp: dict, fiber,
                                requires: str) -> None:
        """Drive one component's deferred activation to completion, LOUDLY
        (roadmap item 372).

        `ctx.plugin()` schedules the apply on the event loop as a `_LazyTask`;
        the fiber can report ACTIVE while its `provide` has not yet run, and a
        closing/offloaded loop can cancel the deferred activation with an empty-
        string `CancelledError` and no diagnostic. This:

        1. drives the `_LazyTask`/activation body to settlement (pumps the
           fiber's `inertia`, then the loop, until the provisions resolve);
        2. converts a cancelled activation — whose bare `CancelledError` renders
           as the empty string — into a typed, named :class:`ActivationError`;
        3. enforces the FIBERS/ROOT invariant: an ACTIVE fiber whose provision
           never landed in ROOT is the false-loaded contradiction, refused here.

        A component whose requirements are genuinely unmet stays PENDING — it
        provides nothing and is reported, not counted loaded, and never raised.
        A mid-body FAILED activation is likewise honest and observable (its keys
        are simply absent from `resolved_keys()`); only the ACTIVE-but-unpublished
        contradiction and a cancellation are raised.
        """
        provided = list((comp.get("provides") or {}).keys())
        try:
            await self._settle(fiber, comp, provided)
        except asyncio.CancelledError as exc:
            # the offloaded/closing loop cancelled the deferred activation.
            # `str(CancelledError)` is empty — name it instead of losing it.
            raise ActivationError(
                name, provided,
                "the event loop closed or the activation was cancelled before "
                "it completed") from exc
        if fiber.state == self.FiberState.PENDING:
            # legitimately not up yet: its injected requirement has no provider.
            # It publishes nothing, so there is no false-loaded key to guard.
            self._log("note", name, f"PENDING — unmet requirement ({requires})")
            return
        # A FAILED activation is an honest, observable loaded-state: its `provide`
        # never ran, so its keys are simply absent from `resolved_keys()` — the
        # fiber is reported FAILED, not as a provider of a key ROOT lacks. That is
        # NOT the false-loaded bug and must not abort the load (the replay
        # mid-body-failure contract). The contradiction item 372 forbids is a
        # fiber that reports ACTIVE while its provision never landed:
        if fiber.state == self.FiberState.ACTIVE:
            isolate = comp.get("isolate") or {}
            missing = [key for key in provided
                       if self._resolve_provision(key, isolate.get(key)) is None]
            if missing:
                raise ActivationError(
                    name, missing,
                    "the fiber is ACTIVE but its provision(s) never landed in "
                    f"ROOT — 'loaded' would be a lie for {', '.join(missing)}")

    async def _settle(self, fiber, comp: dict, provided: list) -> None:
        """Drive a fiber's deferred activation to a settled state.

        Mirrors cordis's own `fiber.await_()` — pump the fiber's `inertia` (its
        `_LazyTask` reload) to `None` — but WITHOUT re-raising the fiber's own
        `_error`: a mid-body activation failure lands the fiber FAILED, an
        honest observable state the replay recorder and `revl run` both surface,
        not a reason to abort the load. A provision published by a *synchronous*
        body is in ROOT once the reload settles; an *asynchronous* body
        publishes from a detached effect task, so pump a bounded number of loop
        turns until the keys resolve. The bound is a safety net, not a timeout —
        a well-behaved body resolves in the first turn (zero for a sync body),
        and one that never resolves falls through to the loud FIBERS/ROOT
        consistency check in the caller. A `CancelledError` (the loop closing
        underneath the deferred task) propagates to be named there."""
        while getattr(fiber, "inertia", None) is not None:
            await fiber.inertia
        if not provided:
            return
        isolate = comp.get("isolate") or {}
        for _ in range(1000):
            if all(self._resolve_provision(key, isolate.get(key)) is not None
                   for key in provided):
                return
            await asyncio.sleep(0)

    def _resolve_provision(self, key: str, realm):
        """Resolve one provided key the way the composition publishes it: an
        isolated key lives in its placement realm (a worker's provision lands in
        `realm(w)`, not the shared root realm), so it is read through
        `root.isolate(key, realm(w)).reflect.get(key)` — exactly how `_Router`
        and the emitted routed-require resolve it. `realm` is the providing
        component's own placement for `key` (`None` = the shared root realm), so
        the SAME key provided by different workers in different realms resolves
        per provider. Returns `None` when no live provider has published it."""
        if realm is None:
            return self.root.get(key)
        return self.root.isolate(
            key, self.runtime.realm_label(realm)).reflect.get(key)

    def resolved_keys(self) -> set:
        """The keys this composition actually provides — those with a live
        provider (roadmap item 372), resolved in each provider's placement realm.

        The FIBERS/ROOT consistency surface: a key appears here IFF it actually
        resolves to a live provider (in root, or in the realm a worker was
        isolated into), so a fiber that reports ACTIVE — or was left so by a
        cancelled deferred activation — while its provision never landed can
        never be reported as providing that key. `state()`'s `providedKeys`
        derives from this, so "listed loaded but ROOT is None" is unreachable
        through the report."""
        resolved: set = set()
        for comp in _components(self.ir or {}):
            isolate = comp.get("isolate") or {}
            for key in (comp.get("provides") or {}):
                if key in resolved:
                    continue
                if self._resolve_provision(key, isolate.get(key)) is not None:
                    resolved.add(key)
        return resolved

    def _install_router(self, name: str, comp: dict) -> None:
        """Realize a router component's ``routes`` IR: for each routed key, build
        a :class:`_Router` over its worker realms and provide it in the router's
        realm. The provision is a real cordis effect (its disposer withdraws the
        key on teardown), so the router keeps the no-residue guarantee — and,
        being the sole provider of the key in the parent realm, keeps G2."""
        disposers = self._route_disposers.setdefault(name, [])
        for key, route in (comp.get("routes") or {}).items():
            realms = route["realms"]
            strategy = route.get("strategy")

            def _leg(realm, method, _key=key):
                self._log("route", _key, f"-> realm({realm}).{method}")

            router = _Router(self.root, self.runtime, key, realms, strategy,
                             log=_leg)
            self.routers[(name, key)] = router
            # provide the routing proxy under `key` in the router's realm; a
            # consumer requiring `key` resolves exactly this one provider (G2).
            disposer = self.root.reflect.provide(key, router)
            disposers.append(disposer)
            self._log("route", name,
                      f"provides {key} — routing across realms("
                      f"{', '.join(realms)}) strategy({strategy or 'round_robin'})")

    async def _dispose_all(self, ir: dict) -> None:
        for name in reversed(_load_order(ir)):  # consumers before providers
            disposers = self._route_disposers.pop(name, None)
            if disposers is not None:
                # a router: withdraw its routing provision(s) at its own LIFO
                # position (after the consumers above it, before the workers
                # below) — the no-residue proof needs the provide-effect gone.
                self._log("swap", name, "withdraw route provisions (LIFO)")
                for disposer in reversed(disposers):
                    result = disposer()
                    if hasattr(result, "__await__") or asyncio.iscoroutine(result):
                        await result
                self.routers = {k: v for k, v in self.routers.items()
                                if k[0] != name}
                await self._flush()
                continue
            fiber = self.fibers.pop(name, None)
            if fiber is None:
                continue
            self._log("swap", name, "dispose -> inverses replay (LIFO)")
            await fiber.dispose()
            await self._flush()
        await self._flush()

    # -- withdrawal + the prediction-vs-actuality oracle -------------------

    async def _perform_withdrawal(self, component: str) -> dict | None:
        """Withdraw one live component and record the causal cascade the
        runtime *actually* produces, then run the oracle against the static
        prediction. Returns the oracle report (or ``None`` on a bad name)."""
        fiber = self.fibers.get(component)
        if fiber is None:
            self._log("error", "withdraw",
                      f"no live component named {component!r} "
                      f"(have: {', '.join(sorted(self.fibers)) or 'none'})")
            return None

        self._log("withdraw", component, "operator withdraws — observe the cascade")
        # observation window: snapshot every fiber's pre-state, then dispose
        # the target and let the reactive graph settle. What comes down, and
        # in what order, is the runtime's answer — not the prediction's.
        self._observing = {n: self.FiberState(f.state).name
                           for n, f in self.fibers.items()}
        self._settled = []
        cascade_causes = why_runtime.withdrawal_causes(self.ir, component)
        await fiber.dispose()
        await self._flush()
        self._observing = None

        for name, frm, to, err in self._settled:
            cause = self._withdraw_cause(name, component, to, err, cascade_causes)
            self._record(why_runtime.WITHDRAW, name, f"{frm} -> {to}", cause)

        report = why_runtime.oracle(self.ir, component, why_runtime.Trace(self._events))
        return report

    # -- REPL --------------------------------------------------------------

    def _namespace(self) -> dict:
        return {key: self.root.get(key) for key in _key_to_service(self.ir)}

    def _print_keys(self) -> None:
        keys = _key_to_service(self.ir)
        services = self.ir.get("services") or {}
        if not keys:
            print("  (this composition provides no services — nothing to call)")
            return
        for key in sorted(keys):
            methods = list(((services.get(keys[key]) or {}).get("methods") or {}).keys())
            print(f"  {key}: {keys[key]}  ->  {', '.join(methods) or '(no methods)'}")

    async def _eval(self, line: str) -> None:
        # tag whatever this line accumulates with the line itself, so
        # `:forward` can re-run it (docs/replay.md §4.4)
        if self.recorder is not None:
            self.recorder.set_origin({"phase": "repl", "line": line})
        try:
            result = eval(line, {"__builtins__": builtins}, self._namespace())  # noqa: S307
            if hasattr(result, "__await__"):
                result = await result
            await self._flush()
            if result is not None:
                self._log("call", "=>", repr(result))
        except Exception as exc:  # noqa: BLE001 — a REPL reports, never crashes
            self._log("error", type(exc).__name__, str(exc))
        finally:
            if self.recorder is not None:
                self.recorder.activation_origin()

    # -- replay REPL commands ----------------------------------------------

    async def _replay(self, op: str, at, force: bool, component) -> None:
        """Run one `:timeline` / `:inspect` / `:back` / `:forward` command."""
        if self.recorder is None:
            self._log("error", "replay",
                      "not recording — restart with `revl run ... --record` "
                      "(recording is installed before activation, so it cannot "
                      "be turned on now)")
            return
        from .mcp.session import replay_module  # noqa: PLC0415 — lazy, no cordis

        replay = replay_module()
        try:
            if op == "timeline":
                for name, timeline in self.recorder.timelines.items():
                    if component and name != component:
                        continue
                    self._log("replay", name,
                              f"{len(timeline.steps)} recorded step(s)")
                    for step in timeline.steps:
                        mark = "x" if step.undone else (
                            "!" if step.kind == replay.KIND_EMISSION else " ")
                        self._log("step", f"{step.index:>3} {mark} {step.kind}",
                                  f"{step.label}   {step.source or ''}".rstrip())
                self._log("note", "guarantee", replay.GUARANTEE)
                return
            timeline = self.recorder.timeline(component)
            if op == "bisect":
                report = timeline.bisect(at)  # `at` carries the predicate here
                if not report.get("flipped"):
                    self._log("bisect", "no flip", report["reason"])
                    self._log("note", "guarantee", report["guarantee"])
                    return
                rec = report["record"]
                self._log("bisect", "found",
                          f"step {report['found']}  {rec['label']} "
                          f"({report['reduction']})")
                self._log("bisect", "whoRan", rec["whoRan"])
                self._log("bisect", "touched", repr(rec["touched"]) or "-")
                self._log("bisect", "realm", rec["realm"])
                verified = report["verified"]
                self._log("bisect", "effects",
                          f"{verified['status'].upper()} — "
                          f"{verified['verifiedOnPath']}/{verified['effectsOnPath']} "
                          "on the path verified")
                self._log("note", "guarantee", report["guarantee"])
                return
            if op == "inspect":
                view = timeline.inspect(at)
                self._log("replay", "at", f"{view['at']} {view['atLabel']}")
                self._log("replay", "provisions", ", ".join(view["activeProvisions"]) or "-")
                for step in view["accumulated"]:
                    self._log("acc", f"{step['index']:>3} {step['kind']}", step["label"])
                return
            if op == "back":
                report = await timeline.step_back(at, force=force)
                for step in report["inversesRan"] + report["compensationsRan"]:
                    self._log("undo", step["kind"], step["label"])
                for step in report["emissionsCrossed"]:
                    self._log("CROSSED", step["kind"], f"{step['label']} — irreversible")
                    # v2 emit event: one per crossing. The emission is scoped to
                    # a capability (the target service) and labelled `key.method`.
                    detail = step.get("detail") or {}
                    # item 121: if this crossing is a model completion, the
                    # `validate_retry` seam stashed a fiber-local observation;
                    # thread its `llm` payload + `activationId` onto the record.
                    # A non-model crossing yields None here, so its record stays
                    # byte-identical to a pre-121 v2 emit. Item 444: the digest
                    # gate reads the compile-side taint facts off the IR
                    # (`_crossing_taint`) instead of being hard-wired shut — a
                    # certified-clean composition now emits the digest, and every
                    # unproven path still fails closed (§4, the fail-closed
                    # default): the hop is recorded either way, only the digest
                    # is suppressed.
                    activation_id = f"{timeline.component}#g{self.generation}"
                    arg_origins, taint_engaged = self._crossing_taint(
                        timeline.component)
                    llm = self._model_crossing_payload(
                        activation_id=activation_id,
                        args=detail.get("args"),
                        arg_origins=arg_origins,
                        taint_engaged=taint_engaged)
                    self._record_emit(
                        timeline.component,
                        detail.get("service") or "",
                        step.get("label") or "",
                        why_runtime.cause_trigger(
                            f"crossed by step-back to {at} "
                            f"(an emission has no inverse)"),
                        llm=llm,
                        activation_id=(activation_id if llm is not None
                                       else None))
                for step in report["failed"]:
                    self._log("FAIL", step["label"], step["error"] or "")
                await self._flush()
                self._log("note", "guarantee", report["guarantee"])
                return
            plan = timeline.forward_plan(at)
            for blocked in plan["notReplayable"]:
                self._log("skip", blocked["label"], blocked["reason"])
            for item in plan["replay"]:
                if item["kind"] == "repl":
                    self._log("redo", "repl", item["line"])
                    await self._eval(item["line"])
                else:
                    line = (f"{item['key']}.{item['method']}"
                            f"({', '.join(repr(a) for a in item['args'])})")
                    self._log("redo", "call", line)
                    await self._eval(line)
        except replay.ReplayError as exc:
            self._log("refused", type(exc).__name__, str(exc))

    async def hold_repl(self) -> int:
        module = self._emit_module(self.ir)
        print("== load composition ==")
        await self._load(self.ir, module)
        self._commit_wal()
        print("\n== live — call provided services (`:keys` to list, `:q` or Ctrl-D to quit) ==")
        if self.recorder is not None:
            print("   recording — `:timeline`, `:inspect k`, `:back k [!]`, "
                  "`:forward k`, `:bisect <expr>` (see docs/replay.md)")
        self._print_keys()
        loop = asyncio.get_running_loop()
        try:
            while True:
                try:
                    line = (await loop.run_in_executor(None, input, "revl> ")).strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not line:
                    continue
                if line in (":q", ":quit", ":exit"):
                    break
                if line in (":keys", ":help"):
                    self._print_keys()
                    continue
                try:
                    command = _replay_command(line)
                except ValueError as exc:
                    self._log("error", "usage", str(exc))
                    continue
                if command is not None:
                    await self._replay(*command)
                    continue
                await self._eval(line)
        finally:
            await self._teardown()
        return 0

    # -- one-shot withdrawal (records the causal trace + runs the oracle) --

    async def withdraw_once(self) -> int:
        """Load, withdraw the named component while recording the causal
        cascade, run the oracle, tear down. A conformance defect is reported
        loudly but does not fail the run — the withdrawal itself succeeded;
        the oracle's verdict is the product (docs/why-runtime.md)."""
        module = self._emit_module(self.ir)
        print("== load composition ==")
        await self._load(self.ir, module)
        print(f"\n== withdraw {self.withdraw} — record the causal cascade ==")
        report = await self._perform_withdrawal(self.withdraw)
        if report is not None:
            print("\n" + why_runtime.render_oracle(report))
        await self._teardown()
        return 0

    # -- watch -------------------------------------------------------------

    async def watch(self, files: list[str]) -> int:
        module = self._emit_module(self.ir)
        print("== load composition ==")
        await self._load(self.ir, module)
        paths = [Path(f).resolve() for f in files]
        print(f"\n== watching {', '.join(p.name for p in paths)} — edit a source; Ctrl-C to quit ==")
        seen = {p: p.stat().st_mtime_ns for p in paths if p.exists()}
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        try:
            loop.add_signal_handler(signal.SIGINT, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover — non-unix
            pass
        try:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), 0.35)
                    break
                except asyncio.TimeoutError:
                    pass
                current = {p: p.stat().st_mtime_ns for p in paths if p.exists()}
                if any(current.get(p) != seen.get(p) for p in paths):
                    seen = current
                    await self._reload(files)
        finally:
            await self._teardown()
        return 0

    async def _reload(self, files: list[str]) -> None:
        print("\n== change detected — recompile ==")
        try:
            ir = compile_files(files)
            refuse_admission(ir)  # a hole may not reach a live generation
            problem = _required_config_problem(ir, self.config)
            if problem is not None:
                # a new/changed requirement the host's config cannot meet is
                # an edit the running composition refuses, not a boot failure.
                # (filename, line, message) — a one-argument `RevlError` is a
                # `TypeError` the `except RevlError` below would not catch,
                # which would kill the watch loop on a bad edit instead of
                # rejecting it.
                raise RevlError(files[0] if files else "<composition>", 0,
                                problem)
        except RevlError as exc:
            for i, text in enumerate(str(exc).splitlines()):
                self._log("reject", "REJECTED" if i == 0 else "", text)
            self._log("note", "", "running composition untouched — a bad edit cannot deploy (that is the point)")
            return
        await self._dispose_all(self.ir)  # tear down the old generation first
        self.ir = ir
        await self._load(ir, self._emit_module(ir))

    # -- teardown + residue report -----------------------------------------

    async def _teardown(self) -> None:
        print("\n== teardown — unload everything, then prove no residue ==")
        await self._dispose_all(self.ir)
        checks = [
            ("registry", self.root.registry.size == 0,
             f"registry.size={self.root.registry.size}"),
            ("provisions", self.root.reflect.store == {},
             f"reflect.store={self.root.reflect.store}"),
            ("effects", self.root.fiber._disposables.length == self._baseline_disposables,
             f"disposables={self.root.fiber._disposables.length} (baseline {self._baseline_disposables})"),
            ("listeners", self._hooks() == self._baseline_hooks,
             "event hooks back to baseline"),
        ]
        for name, ok, detail in checks:
            self._log("ok" if ok else "FAIL", name, detail)
        print("  no residue — the composition left nothing behind"
              if all(ok for _, ok, _ in checks)
              else "  RESIDUE LEFT — see FAILs above")
        self.runtime.set_trace(None)
        if self.trace_path is not None:
            why_runtime.write_trace(self._events, self.trace_path)
            self._log("trace", "written",
                      f"{len(self._events)} causal event(s) -> {self.trace_path}")


# --------------------------------------------------------------------------
# entry point (called from __main__.py)
# --------------------------------------------------------------------------


def run_command(args) -> int:
    if getattr(args, "placement", None):
        # `--placement` splits the composition across processes, each with its
        # own tier; a top-level `--backend` would name one tier for the whole
        # composition, which the placement contradicts. Refuse loudly instead
        # of silently ignoring `--backend` (item 363).
        if getattr(args, "backend", "py") != "py":
            print("error: --backend and --placement are mutually exclusive — a "
                  "placement assigns a tier per component (or per process), so "
                  "there is no single --backend for the whole composition; drop "
                  "--backend, or run without --placement", file=sys.stderr)
            return 2
        from .placement import run_placement  # noqa: PLC0415 — lazy: no cordis needed to orchestrate
        return run_placement(args.files, args.placement, once=getattr(args, "once", False))

    backend = getattr(args, "backend", "py")
    if backend not in KNOWN_BACKENDS:
        print(f"error: unknown backend {backend!r} (known: {', '.join(KNOWN_BACKENDS)})",
              file=sys.stderr)
        return 2

    try:
        ir = compile_files(args.files)
        # booting is admission: a draft with open obligations may not become a
        # running composition, however it was compiled (docs/holes.md)
        refuse_admission(ir)
        config = _load_config(getattr(args, "config", None))
    except RevlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: cannot read config: {exc}", file=sys.stderr)
        return 1

    if getattr(args, "plan", False):
        _print_plan(ir, config, backend)
        return 0

    if not _components(ir):
        print("nothing to run: no components in the composition", file=sys.stderr)
        return 0

    problem = _required_config_problem(ir, config)
    if problem is not None:
        print(f"error: {problem}", file=sys.stderr)
        return 1

    if backend in ("rust", "java", "ts", "wasm", "go"):
        # each non-py tier boots as a separate process over the bridge seam, not
        # in-process — the same driver contract, a different address space (and a
        # different runtime): cordis-rs, cordis4j on a JVM, cordis v4 on node,
        # cordis-wasm on wasmtime, or stc-go (docs/interop-bridge.md,
        # docs/swap.md). --once is wired on all five, each behind its own
        # runtime-availability gate.
        once = bool(getattr(args, "once", False))
        interactive = sys.stdin.isatty()
        if backend == "rust":
            from .run_rust import run_rust  # noqa: PLC0415 — lazy: no cargo needed to compile/plan
            return run_rust(ir, config, args.files, once=once, interactive=interactive)
        if backend == "java":
            from .run_java import run_java  # noqa: PLC0415 — lazy: no JDK needed to compile/plan
            return run_java(ir, config, args.files, once=once, interactive=interactive)
        if backend == "ts":
            from .run_ts import run_ts  # noqa: PLC0415 — lazy: no node needed to compile/plan
            return run_ts(ir, config, args.files, once=once, interactive=interactive)
        if backend == "go":
            from .run_go import run_go  # noqa: PLC0415 — lazy: no go toolchain needed to compile/plan
            return run_go(ir, config, args.files, once=once, interactive=interactive)
        # item 289: with a boundary policy in force, the wasm tier enforces the
        # full least-authority chain (host imports subset-of declared caps
        # subset-of policy-allowed) before booting; all three sets are
        # statically decidable for a wasm module.
        policy = None
        if getattr(args, "policy", None):
            from .policy import load_policy  # noqa: PLC0415
            try:
                policy = load_policy(args.policy)
            except (RevlError, OSError) as exc:
                print(f"error: cannot load policy: {exc}", file=sys.stderr)
                return 1
        from .run_wasm import run_wasm  # noqa: PLC0415 — lazy: no wasmtime needed to compile/plan
        return run_wasm(ir, config, args.files, once=once,
                        interactive=interactive, policy=policy)

    backend_dir = backends_root() / "python"
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    try:
        import emit  # noqa: PLC0415 — backend import after path setup
        import runtime as runtime_mod  # noqa: PLC0415
        from cordis import Context  # noqa: PLC0415
        from cordis.fiber import FiberState  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        venv = backend_dir / ".venv" / "bin" / "python"
        print(f"error: the cordis-py runtime is not installed ({exc.name!r} missing).\n"
              f"       set it up:  sh {backend_dir / 'setup.sh'}\n"
              f"       then run under that interpreter:  {venv} -m revl run ...",
              file=sys.stderr)
        return 3

    withdraw = getattr(args, "withdraw", None)
    # item 396 option B: the import roots a `@py ref` file resolves through are
    # the root compile files' own directories (the deploy contract's "import
    # root is the root compile file's directory"). The driver appends them only
    # when the IR carries refs.
    root_dirs = [os.path.dirname(os.path.abspath(f)) for f in args.files]
    driver = _Driver(ir, config, emit, runtime_mod, Context, FiberState,
                     record=bool(getattr(args, "record", False)),
                     trace_path=getattr(args, "trace", None),
                     withdraw=withdraw,
                     wal_path=getattr(args, "wal", None),
                     root_dirs=root_dirs)
    try:
        if withdraw is not None:
            return asyncio.run(driver.withdraw_once())
        if getattr(args, "watch", False):
            return asyncio.run(driver.watch(args.files))
        return asyncio.run(driver.hold_repl())
    except ActivationError as exc:
        # item 372: a component's deferred activation did not complete — report
        # it loudly and named, rather than dropping into a REPL over a
        # composition whose "loaded" would be a lie.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover — signal handler covers unix
        return 130
