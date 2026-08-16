#!/usr/bin/env python
"""revl live hot-swap demo — compiled components, swapped in a running system.

This is the *live* successor to ``backends/python/demo.py``: instead of
replaying a checked-in IR document, it compiles ``demo/components/*.rvl``
with the real frontend, emits cordis-py modules with the real backend, loads
them into one running ``cordis.Context``, and then watches the source
directory. Edit a ``.rvl`` file and the component it declares is recompiled
and swapped into the live system — with the reactive cascade, the derived
LIFO teardown, and the no-residue teardown printed as they happen.

Two modes::

    backends/python/.venv/bin/python demo/live.py            # interactive
    backends/python/.venv/bin/python demo/live.py --script   # CI, exits 1 on failure

See demo/README.md for what to watch for in the log.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import re
import signal
import sys
import time
import types

# --------------------------------------------------------------------------
# import roots: src/ for the compiler frontend, backends/python for the
# emitter *and* for the `runtime` module every emitted component imports
# --------------------------------------------------------------------------

DEMO = pathlib.Path(__file__).resolve().parent
ROOT = DEMO.parent
BACKEND = ROOT / "backends" / "python"
COMPONENTS = DEMO / "components"
VARIANTS = DEMO / "variants"
SERVICES_FILE = COMPONENTS / "services.rvl"

for path in (str(ROOT / "src"), str(BACKEND)):
    if path not in sys.path:
        sys.path.insert(0, path)

import emit as emitter  # noqa: E402  (backends/python/emit.py)
import runtime as runtime_mod  # noqa: E402  (backends/python/runtime.py)

from revl import RevlError, compile_files  # noqa: E402

try:
    from cordis import Context  # noqa: E402
    from cordis.fiber import FiberState  # noqa: E402
except ModuleNotFoundError as exc:  # pragma: no cover - environment guard
    raise SystemExit(
        f"{exc.name!r} is missing — run this demo under the backend venv:\n"
        f"  {BACKEND / '.venv/bin/python'} {pathlib.Path(__file__).name}"
    ) from exc


# --------------------------------------------------------------------------
# host configuration
#
# revl components are descriptions; supplying config and deciding *which*
# components to admit is the host application's job (DESIGN.md §2, non-goals).
# This dict is that host.
# --------------------------------------------------------------------------

HOST_CONFIG = {
    "PgDatabase": {"url": "postgres://primary:5432/app"},
}

POLL_SECONDS = 0.35


# --------------------------------------------------------------------------
# the log — the actual product of this demo
# --------------------------------------------------------------------------

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


CHANNELS = {
    "compile": "36",  # cyan   — frontend: parse / check / link
    "reject": "31",  # red    — a guarantee refused the edit
    "emit": "36",  # cyan   — backend: IR -> cordis-py module
    "load": "35",  # magenta— host admits a component
    "fiber": "33",  # yellow — runtime lifecycle transitions
    "host": "32",  # green  — host resources (pool/map): the residue we track
    "call": "34",  # blue   — traffic through provided services
    "swap": "35",  # magenta— swap orchestration
    "check": "1",  # bold   — assertions
    "note": "2",  # dim    — narration
}


class Log:
    """Aligned, timestamped event log. Every line is `time | # | channel |
    subject | detail`, so a reader can follow one column at a time."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.seq = 0
        self.host: list[str] = []  # raw runtime.set_trace strings, in order
        self.fibers: list[tuple[str, str, str]] = []  # (name, old, new)
        self.live_resources: dict[str, str] = {}  # pool#1 -> "open postgres://…"

    # -- emission ----------------------------------------------------------

    def line(self, channel: str, subject: str, detail: str = "") -> None:
        self.seq += 1
        stamp = _c("2", f"{time.monotonic() - self.t0:7.3f}s")
        num = _c("2", f"{self.seq:03}")
        chan = _c(CHANNELS.get(channel, "0"), f"{channel:<7}")
        bar = _c("2", "|")
        subj = f"{subject:<16}"
        print(f" {stamp} {bar} {num} {bar} {chan} {bar} {subj} {bar} {detail}", flush=True)

    def note(self, text: str) -> None:
        print(_c("2", f"           {text}"), flush=True)

    def banner(self, title: str, subtitle: str = "") -> None:
        rule = "=" * 78
        print()
        print(_c("1", rule))
        print(_c("1", f"  {title}"))
        if subtitle:
            print(_c("2", f"  {subtitle}"))
        print(_c("1", rule), flush=True)

    def rule(self, text: str = "") -> None:
        print(_c("2", f"  -- {text} " + "-" * max(0, 70 - len(text))), flush=True)

    # -- wiring into the runtime ------------------------------------------

    def on_host_event(self, event: str) -> None:
        """runtime.set_trace callback: one string per host-builtin operation."""
        self.host.append(event)
        head, _, rest = event.partition(" ")
        subject, _, op = head.rpartition(".")
        subject = subject or head
        detail = f"{op} {rest}".strip()
        if op in ("open", "new"):
            self.live_resources[subject] = detail
        elif op in ("close", "drop"):
            self.live_resources.pop(subject, None)
        self.line("host", subject, detail)

    def on_fiber_event(self, _this, fiber, old) -> None:
        """`internal/status` callback: the fiber state machine, live."""
        old_name = FiberState(old).name
        new_name = FiberState(fiber.state).name
        self.fibers.append((fiber.name, old_name, new_name))
        arrow = _c("1", "->")
        self.line("fiber", fiber.name, f"{old_name:<9} {arrow} {new_name}")

    # -- queries used by the assertions -----------------------------------

    def mark(self) -> int:
        return len(self.host)

    def since(self, mark: int) -> list[str]:
        return self.host[mark:]


def strip_serials(events: list[str]) -> list[str]:
    """'map#3.drop k' -> 'map.drop k' (the backend-test `ops` idiom)."""
    return [re.sub(r"#\d+", "", event) for event in events]


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------


class Checks:
    def __init__(self, log: Log) -> None:
        self.log = log
        self.failures: list[str] = []

    def that(self, name: str, ok: bool, detail: str = "") -> bool:
        verdict = _c("32", "PASS") if ok else _c("31", "FAIL")
        self.log.line("check", name, f"{verdict}  {detail}")
        if not ok:
            self.failures.append(f"{name}: {detail}")
        return ok


# --------------------------------------------------------------------------
# the composition under demo
# --------------------------------------------------------------------------


async def flush() -> None:
    """Settle pending event-loop work (the cordis test-suite idiom)."""
    for _ in range(20):
        await asyncio.sleep(0)


class CompileError(Exception):
    pass


class Composition:
    """One running cordis Context plus the bookkeeping the host needs to
    recompile and swap individual components."""

    def __init__(self, log: Log, checks: Checks) -> None:
        self.log = log
        self.checks = checks
        self.root = Context()
        self.fibers: dict[str, object] = {}  # component name -> fiber
        self.origin: dict[str, pathlib.Path] = {}  # component name -> source file
        self.running_ir: dict | None = None  # the admitted composition (manifest + services)
        self.generation = 0

        runtime_mod.set_trace(log.on_host_event)
        self.root.on("internal/status", log.on_fiber_event)

        self.baseline_hooks = self._hook_snapshot()
        self.baseline_disposables = self.root.fiber._disposables.length

    # -- runtime introspection --------------------------------------------

    def _hook_snapshot(self) -> dict:
        return {
            name: len(callbacks)
            for name, callbacks in self.root.events._hooks.items()
            if callbacks
        }

    # -- compile + emit ----------------------------------------------------

    def sources_for(self, changed: pathlib.Path) -> list[pathlib.Path]:
        """Which sources to compile together.

        The linker rejects two providers of one key inside a single
        composition (G2), so a swap never compiles old+new together: the
        changed file is compiled *as its own composition*, with the shared
        service declarations. Touching services.rvl recompiles every
        component, because the vocabulary itself moved.
        """
        if changed == SERVICES_FILE:
            return sorted(p for p in COMPONENTS.glob("*.rvl") if p != SERVICES_FILE)
        return [changed]

    def compile_and_emit(self, sources: list[pathlib.Path], why: str) -> tuple[dict, types.ModuleType]:
        # the runtime-admission gate (DESIGN §4): once a composition is
        # running, a changed file is compiled *alone* against the running
        # manifest — ambient services come from the manifest, same-name
        # components are implicit replacements, and G2/G3 span both
        admission = (
            self.running_ir is not None
            and SERVICES_FILE not in sources
        )
        if admission:
            files = list(sources)
            for path in files:
                self.log.line("compile", path.name,
                              f"admission against the running manifest   ({why})")
        else:
            files = [SERVICES_FILE] + [p for p in sources if p != SERVICES_FILE]
            for path in files:
                self.log.line("compile", path.name, f"parse -> check -> link   ({why})")
        try:
            if admission:
                ir = compile_files([str(p) for p in files], manifest=self.running_ir)
            else:
                ir = compile_files([str(p) for p in files])
        except RevlError as exc:
            for i, line in enumerate(str(exc).splitlines()):
                self.log.line("reject", "REJECTED" if i == 0 else "", line)
            raise CompileError(str(exc)) from exc
        self.running_ir = ir

        names = [component["name"] for component in ir["components"]]
        provided = {
            key: component["name"]
            for component in ir["components"]
            for key in component["provides"]
        }
        self.log.line(
            "compile",
            "link ok",
            f"G2 disjoint provisions {provided or '{}'} · G3 acyclic · {len(names)} component(s)",
        )

        source = emitter.emit(ir)
        self.generation += 1
        module = types.ModuleType(f"revl_demo_gen{self.generation}")
        exec(compile(source, f"<revl gen{self.generation}>", "exec"), module.__dict__)
        self.log.line(
            "emit",
            f"gen{self.generation}",
            f"{len(source.splitlines())} lines of cordis-py for {', '.join(names)}",
        )
        return ir, module

    # -- load / unload -----------------------------------------------------

    @staticmethod
    def _consumers_first(components: list[dict]) -> list[dict]:
        return sorted(components, key=lambda c: (-len(c["requires"]), c["name"]))

    def _signature(self, component: dict) -> str:
        requires = " ".join(f"{k}:{v}" for k, v in component["requires"].items()) or "-"
        provides = " ".join(f"{k}:{v}" for k, v in component["provides"].items()) or "-"
        return f"requires {requires}  ·  provides {provides}"

    async def load(self, ir: dict, module: types.ModuleType, source: pathlib.Path | None = None) -> None:
        for component in self._consumers_first(ir["components"]):
            name = component["name"]
            config = HOST_CONFIG.get(name, {})
            self.log.line("load", name, self._signature(component))
            fiber = self.root.plugin(getattr(module, name), config)
            self.fibers[name] = fiber
            if source is not None:
                self.origin[name] = source
            await flush()
            if fiber.state == FiberState.PENDING:
                missing = ", ".join(component["requires"]) or "?"
                self.log.note(f"{name} is PENDING — nobody provides `{missing}` yet (spatial composability)")
        for name, fiber in list(self.fibers.items()):
            if fiber.state == FiberState.LOADING:  # an async body (an `await` step) is in flight
                try:
                    await asyncio.wait_for(asyncio.shield(fiber), 2)
                except asyncio.TimeoutError:
                    self.log.line("load", name, "still LOADING after 2s — leaving it in flight")
        await flush()

    async def unload(self, names: list[str]) -> None:
        for name in names:
            fiber = self.fibers.pop(name, None)
            if fiber is None:
                continue
            self.log.line("swap", name, "dispose live fiber -> inverses replay, LIFO")
            await fiber.dispose()
            await flush()

    # -- traffic -----------------------------------------------------------

    def cache(self):
        return self.root.get("cache")

    async def traffic(self, pairs: list[tuple[str, str]], probe: str | None = None) -> object:
        cache = self.cache()
        if cache is None:
            self.log.line("call", "cache", "key `cache` is not provided right now — no traffic possible")
            return None
        for key, value in pairs:
            self.log.line("call", "cache.put", f"({key!r}, {value!r})")
            cache.put(key, value)
        result = None
        if probe is not None:
            result = cache.get(probe)
            self.log.line("call", "cache.get", f"({probe!r}) -> {result!r}")
        await flush()
        return result

    # -- the swap ----------------------------------------------------------

    async def swap(self, changed: pathlib.Path) -> bool:
        """Recompile `changed` from source and swap its components into the
        running system. Returns False when the edit was rejected."""
        self.log.banner(
            f"HOT-SWAP  ·  {changed.name} changed on disk",
            "recompile from source, unload the old component, admit the new one",
        )
        sources = self.sources_for(changed)
        try:
            ir, module = self.compile_and_emit(sources, why="swap")
        except CompileError:
            self.log.note("the edit never reached the runtime — the running composition is untouched")
            self.log.note("this is the point: a component that would break a guarantee cannot be deployed")
            return False

        self.log.rule("unload the old version (watch the cascade)")
        await self.unload([c["name"] for c in self._consumers_first(ir["components"])])

        self.log.rule("admit the new version (watch dependents reactivate)")
        # a services.rvl edit recompiles every component; each one keeps the
        # file that declares it as its origin
        await self.load(ir, module, source=None if changed == SERVICES_FILE else changed)
        return True

    # -- teardown ----------------------------------------------------------

    async def shutdown(self) -> None:
        self.log.banner(
            "TEARDOWN  ·  unload everything, then prove there is no residue",
            "consumers first; every inverse the runtime accumulated now replays",
        )
        order = sorted(self.fibers, key=lambda n: (n != "UserCache", n))
        await self.unload(order)
        await flush()

        checks = self.checks
        checks.that(
            "registry",
            self.root.registry.size == 0,
            f"root.registry.size == {self.root.registry.size} (no plugin runtimes left)",
        )
        checks.that(
            "provisions",
            self.root.reflect.store == {},
            f"root.reflect.store == {self.root.reflect.store} (no service bindings left)",
        )
        checks.that(
            "effects",
            self.root.fiber._disposables.length == self.baseline_disposables,
            f"root fiber holds {self.root.fiber._disposables.length} disposables "
            f"(baseline {self.baseline_disposables})",
        )
        checks.that(
            "listeners",
            self._hook_snapshot() == self.baseline_hooks,
            "event-hook snapshot is back to its baseline",
        )
        checks.that(
            "host-resources",
            not self.log.live_resources,
            f"live host resources: {self.log.live_resources or '{}'} (every pool closed, every map dropped)",
        )
        tail = strip_serials(self.log.host)[-6:]
        self.log.note(f"final host-operation tail: {tail}")
        runtime_mod.set_trace(None)


# --------------------------------------------------------------------------
# phase 1 — shared by both modes
# --------------------------------------------------------------------------


async def bootstrap(comp: Composition) -> dict:
    log = comp.log
    log.banner(
        "PHASE 1  ·  compile the composition from .rvl source and load it",
        f"{COMPONENTS} -> frontend IR -> cordis-py module -> running Context",
    )
    sources = sorted(p for p in COMPONENTS.glob("*.rvl") if p != SERVICES_FILE)
    ir, module = comp.compile_and_emit(sources, why="cold start")
    log.rule("load, consumers first — so you can see the reactive resolution")
    await comp.load(ir, module)
    # provenance is in the IR: each component names the file that declares
    # it, so a later edit (or deletion) knows which fibers it owns
    for component in ir["components"]:
        comp.origin[component["name"]] = pathlib.Path(component["source"]).resolve()
    return ir


async def phase_traffic(comp: Composition, title: str, pairs, probe: str):
    comp.log.banner(title, "provided services are ordinary calls; their effects are not")
    return await comp.traffic(pairs, probe)


# --------------------------------------------------------------------------
# scripted (CI) mode
# --------------------------------------------------------------------------


async def run_script() -> int:
    log = Log()
    checks = Checks(log)
    comp = Composition(log, checks)
    pg_source = COMPONENTS / "pg_database.rvl"
    original = pg_source.read_text(encoding="utf-8")

    try:
        # -- phase 1: cold start ------------------------------------------
        ir = await bootstrap(comp)
        provided = {
            key: c["name"] for c in ir["components"] for key in c["provides"]
        }
        checks.that("link", provided == {"db": "PgDatabase", "cache": "UserCache"},
                    f"provider map {provided}")
        checks.that(
            "activation",
            comp.fibers["UserCache"].state == FiberState.ACTIVE
            and ("UserCache", "PENDING", "LOADING") in log.fibers,
            "UserCache waited in PENDING, then activated when `db` appeared (R2)",
        )

        # -- phase 2: traffic ---------------------------------------------
        mark = log.mark()
        got = await phase_traffic(
            comp, "PHASE 2  ·  drive traffic through the running composition",
            [("alice", "42"), ("bob", "7")], "alice",
        )
        window = strip_serials(log.since(mark))
        checks.that("traffic", got == "42", f'cache.get("alice") -> {got!r}')
        checks.that(
            "emission",
            any("cache_log" in event for event in window),
            "cache.put's `emit db.execute(...)` reached the provider's pool",
        )
        checks.that(
            "method-effect",
            [e for e in window if e.startswith("map.insert")] == ["map.insert alice", "map.insert bob"],
            "each put accumulated a revertible insert while ACTIVE",
        )

        # -- phase 3: a rejected edit -------------------------------------
        log.banner(
            "PHASE 3  ·  a bad edit — the guarantee refuses the swap",
            "components/pg_database.rvl loses its `undo pool.close()` (G4)",
        )
        pg_source.write_text((VARIANTS / "pg_database.rejected.rvl").read_text(encoding="utf-8"),
                            encoding="utf-8")
        accepted = await comp.swap(pg_source)
        still = comp.cache()
        checks.that("rejected-edit", accepted is False, "compile failed: G4, effect without an inverse")
        checks.that(
            "still-serving",
            still is not None and still.get("alice") == "42"
            and comp.fibers["PgDatabase"].state == FiberState.ACTIVE,
            "the previous version is still ACTIVE and still answering",
        )

        # -- phase 4: the real edit + hot swap ----------------------------
        log.banner(
            "PHASE 4  ·  a good edit — recompile and swap it into the live system",
            "components/pg_database.rvl gains an advisory-lock acquisition, pool_size 10 -> 25",
        )
        pg_source.write_text((VARIANTS / "pg_database.hotswap.rvl").read_text(encoding="utf-8"),
                            encoding="utf-8")
        mark = log.mark()
        accepted = await comp.swap(pg_source)
        window = strip_serials(log.since(mark))
        checks.that("accepted-edit", accepted is True, "the new PgDatabase compiled, linked and loaded")

        # the cascade: every dependent inverse ran before the provider's own
        closes = [i for i, e in enumerate(window) if e.startswith("pool.close")]
        inverses = [
            i for i, e in enumerate(window)
            if e.startswith("map.remove") or e.startswith("map.drop")
        ]
        checks.that(
            "cascade-order",
            bool(closes) and bool(inverses) and max(inverses) < closes[0],
            f"dependent inverses {[window[i] for i in inverses]} all precede "
            f"{window[closes[0]] if closes else 'pool.close'} (R3)",
        )
        checks.that(
            "deactivate-reactivate",
            ("UserCache", "ACTIVE", "UNLOADING") in log.fibers[-14:]
            and comp.fibers["UserCache"].state == FiberState.ACTIVE,
            "UserCache deactivated with its provider and reactivated against the new one (R2)",
        )
        checks.that(
            "new-behavior-live",
            any("pg_advisory_lock" in event for event in window),
            "the acquisition that only exists in the *edited source* ran in the live system",
        )

        # -- phase 5: traffic against the swapped provider ----------------
        mark = log.mark()
        got = await phase_traffic(
            comp, "PHASE 5  ·  traffic again — same process, recompiled component",
            [("alice", "43")], "alice",
        )
        window = strip_serials(log.since(mark))
        checks.that("fresh-state", got == "43", f'the store was rebuilt by reactivation; get("alice") -> {got!r}')
        checks.that(
            "rebound-emission",
            any("cache_log" in event for event in window),
            "the cache's emission now goes through the *new* provider's pool",
        )

        # -- phase 6: teardown + no residue -------------------------------
        await comp.shutdown()
        # the whole teardown, newest-first, across two components: the cache's
        # method-time insert is undone, then its store, then — only then — the
        # provider's own acquisitions, in reverse acquisition order
        tail = strip_serials(log.host)[-4:]
        checks.that(
            "lifo-tail",
            tail == [
                "map.remove alice",
                "map.drop",
                "pool.query SELECT pg_advisory_unlock(42)",
                "pool.close postgres://primary:5432/app",
            ],
            f"teardown tail {tail}",
        )
    finally:
        pg_source.write_text(original, encoding="utf-8")

    log.banner("RESULT", f"{len(checks.failures)} failed check(s)" if checks.failures else "all checks passed")
    for failure in checks.failures:
        print(_c("31", f"  FAIL  {failure}"))
    if not checks.failures:
        print(_c("32", "  Compiled from source, hot-swapped live, torn down with no residue."))
    return 1 if checks.failures else 0


# --------------------------------------------------------------------------
# interactive mode
# --------------------------------------------------------------------------


def snapshot_sources() -> dict[pathlib.Path, float]:
    return {path: path.stat().st_mtime_ns for path in COMPONENTS.glob("*.rvl")}


async def run_interactive() -> int:
    log = Log()
    checks = Checks(log)
    comp = Composition(log, checks)

    await bootstrap(comp)
    await phase_traffic(
        comp, "PHASE 2  ·  drive traffic through the running composition",
        [("alice", "42"), ("bob", "7")], "alice",
    )

    log.banner(
        "PHASE 3  ·  watching demo/components/*.rvl  —  edit a file, watch the swap",
        "try: paste demo/variants/pg_database.hotswap.rvl over components/pg_database.rvl, "
        "or delete an `undo` clause.  Ctrl-C to tear down.",
    )
    log.note("(adding a new .rvl file loads a new component; deleting one unloads it)")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, stop.set)
    except NotImplementedError:  # pragma: no cover - non-unix
        pass

    seen = snapshot_sources()
    try:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), POLL_SECONDS)
                break
            except asyncio.TimeoutError:
                pass

            current = snapshot_sources()
            for path in sorted(set(current) - set(seen)):
                log.banner(f"NEW COMPONENT FILE  ·  {path.name}",
                           "a component joins a system that never stopped running")
                try:
                    ir, module = comp.compile_and_emit([path], why="new file")
                    await comp.load(ir, module, source=path)
                except CompileError:
                    pass
            for path in sorted(set(seen) - set(current)):
                names = [n for n, src in comp.origin.items() if src == path]
                log.banner(f"COMPONENT FILE REMOVED  ·  {path.name}",
                           "unload it and watch the environment come back to where it was")
                await comp.unload(names)
            for path in sorted(set(current) & set(seen)):
                if current[path] != seen[path]:
                    await comp.swap(path)
                    await comp.traffic([("alice", "42")], "alice")
            seen = current
    finally:
        await comp.shutdown()

    log.banner("RESULT", f"{len(checks.failures)} failed check(s)" if checks.failures else "no residue — clean exit")
    for failure in checks.failures:
        print(_c("31", f"  FAIL  {failure}"))
    return 1 if checks.failures else 0


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--script",
        action="store_true",
        help="non-interactive: run the whole sequence programmatically and exit nonzero on any failed check",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(run_script() if args.script else run_interactive())
    except KeyboardInterrupt:  # pragma: no cover - signal handler covers unix
        return 130


if __name__ == "__main__":
    sys.exit(main())
