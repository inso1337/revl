"""`revl run`: boot a compiled composition on a Cordis runtime.

Three modes, one command:

- ``revl run app.rvl``            load the composition, run activation, then
                                  hold it live with an interactive REPL over
                                  its provided services; tear down on exit and
                                  report no-residue.
- ``revl run app.rvl --watch``    load, then watch the sources and recompile on
                                  change. A rejected edit (a guarantee refused
                                  it) leaves the running composition untouched.
- ``revl run app.rvl --plan``     print the load plan (order, config, callable
                                  keys) and exit, without touching a runtime.

The frontend stays pure: the backend emitter and the cordis-py runtime are
imported lazily, inside ``run_command``, so ``compile``/``audit``/``test``/
``fmt`` and ``run --plan`` all work with no runtime installed. Only ``py`` is a
runnable tier today; ``--backend {ts,rust,java}`` is accepted but reports that
it is not wired yet (multi-runtime is a roadmap item, not a promise here).
"""

from __future__ import annotations

import asyncio
import builtins
import json
import signal
import sys
import types
from pathlib import Path

from .compiler import compile_files
from .errors import RevlError

KNOWN_BACKENDS = ("py", "ts", "rust", "java")
RUNNABLE_BACKENDS = ("py",)


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
        raise RevlError(
            f"config {path}: expected a table of `component-name = {{ ... }}` entries"
        )
    return data


def _components(ir: dict) -> list[dict]:
    return ir.get("components") or []


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
    print(f"backend: {backend}"
          f"{'' if backend in RUNNABLE_BACKENDS else '  (not runnable yet)'}")
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
    keys = _key_to_service(ir)
    services = ir.get("services") or {}
    print("\ncallable in the REPL:")
    if not keys:
        print("  (this composition provides no services)")
    for key in sorted(keys):
        methods = list(((services.get(keys[key]) or {}).get("methods") or {}).keys())
        print(f"  {key}: {keys[key]}  ->  {', '.join(methods) or '(no methods)'}")


# --------------------------------------------------------------------------
# the runtime driver (py tier)
# --------------------------------------------------------------------------


class _Driver:
    """Boots and holds one composition on a cordis.Context, with a compact
    trace, an interactive REPL, and a no-residue teardown. Modeled on
    demo/live.py, generalized off the IR manifest."""

    def __init__(self, ir, config, emit, runtime_mod, Context, FiberState):
        self.ir = ir
        self.config = config
        self.emit = emit
        self.runtime = runtime_mod
        self.FiberState = FiberState
        self.root = Context()
        self.fibers: dict[str, object] = {}
        self.generation = 0

        self.runtime.set_trace(self._on_host)
        self.root.on("internal/status", self._on_fiber)
        self._baseline_hooks = self._hooks()
        self._baseline_disposables = self.root.fiber._disposables.length

    # -- trace -------------------------------------------------------------

    def _log(self, channel: str, subject: str, detail: str = "") -> None:
        print(f"  {channel:<7}| {subject:<18}| {detail}".rstrip(), flush=True)

    def _on_host(self, event: str) -> None:
        head, _, rest = event.partition(" ")
        self._log("host", head, rest)

    def _on_fiber(self, _this, fiber, old) -> None:
        self._log("fiber", fiber.name,
                  f"{self.FiberState(old).name} -> {self.FiberState(fiber.state).name}")

    def _hooks(self) -> dict:
        return {name: len(cbs) for name, cbs in self.root.events._hooks.items() if cbs}

    async def _flush(self) -> None:
        for _ in range(20):
            await asyncio.sleep(0)

    # -- emit + load -------------------------------------------------------

    def _emit_module(self, ir: dict) -> types.ModuleType:
        self.generation += 1
        source = self.emit.emit(ir)
        module = types.ModuleType(f"revl_run_gen{self.generation}")
        # register before exec: emitted record types are @dataclass, and
        # dataclasses resolves fields via sys.modules[cls.__module__]
        sys.modules[module.__name__] = module
        exec(compile(source, f"<revl-run gen{self.generation}>", "exec"), module.__dict__)
        return module

    async def _load(self, ir: dict, module: types.ModuleType) -> None:
        by_name = {c["name"]: c for c in _components(ir)}
        for name in _load_order(ir):
            comp = by_name[name]
            requires = ", ".join(comp.get("requires") or {}) or "-"
            provides = ", ".join(comp.get("provides") or {}) or "-"
            self._log("load", name, f"requires {requires} · provides {provides}")
            fiber = self.root.plugin(getattr(module, name), self.config.get(name, {}))
            self.fibers[name] = fiber
            await self._flush()
            if fiber.state == self.FiberState.LOADING:  # an async (`await`) body in flight
                try:
                    await asyncio.wait_for(asyncio.shield(fiber), 2)
                except asyncio.TimeoutError:
                    self._log("note", name, "still LOADING after 2s")
            if fiber.state == self.FiberState.PENDING:
                self._log("note", name, f"PENDING — unmet requirement ({requires})")
        await self._flush()

    async def _dispose_all(self, ir: dict) -> None:
        for name in reversed(_load_order(ir)):  # consumers before providers
            fiber = self.fibers.pop(name, None)
            if fiber is None:
                continue
            self._log("swap", name, "dispose -> inverses replay (LIFO)")
            await fiber.dispose()
            await self._flush()
        await self._flush()

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
        try:
            result = eval(line, {"__builtins__": builtins}, self._namespace())  # noqa: S307
            if hasattr(result, "__await__"):
                result = await result
            await self._flush()
            if result is not None:
                self._log("call", "=>", repr(result))
        except Exception as exc:  # noqa: BLE001 — a REPL reports, never crashes
            self._log("error", type(exc).__name__, str(exc))

    async def hold_repl(self) -> int:
        module = self._emit_module(self.ir)
        print("== load composition ==")
        await self._load(self.ir, module)
        print("\n== live — call provided services (`:keys` to list, `:q` or Ctrl-D to quit) ==")
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
                await self._eval(line)
        finally:
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


# --------------------------------------------------------------------------
# entry point (called from __main__.py)
# --------------------------------------------------------------------------


def run_command(args) -> int:
    backend = getattr(args, "backend", "py")
    if backend not in KNOWN_BACKENDS:
        print(f"error: unknown backend {backend!r} (known: {', '.join(KNOWN_BACKENDS)})",
              file=sys.stderr)
        return 2

    try:
        ir = compile_files(args.files)
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

    if backend not in RUNNABLE_BACKENDS:
        print(f"error: `revl run --backend {backend}` is not wired yet — "
              f"only {'/'.join(RUNNABLE_BACKENDS)} runs today.\n"
              f"       the {backend} tier emits (see backends/{backend}/), but has no `run` driver.",
              file=sys.stderr)
        return 2

    if not _components(ir):
        print("nothing to run: no components in the composition", file=sys.stderr)
        return 0

    backend_dir = Path(__file__).resolve().parents[2] / "backends" / "python"
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

    driver = _Driver(ir, config, emit, runtime_mod, Context, FiberState)
    try:
        if getattr(args, "watch", False):
            return asyncio.run(driver.watch(args.files))
        return asyncio.run(driver.hold_repl())
    except KeyboardInterrupt:  # pragma: no cover — signal handler covers unix
        return 130
