"""Per-tier validation of *emitted* code — the conformance matrix's second half.

The matrix in `conformance.py` answers "did the emitter raise?". That question
has a documented blind spot: the rust backend failed to capture `requires`
bindings into its provider struct, emitted Rust referencing a free variable,
and reported `ok` here for months. Nothing in an emit-only sweep can catch
that. This module answers the second question — *does the emitted code hold
up when the target toolchain looks at it?*

Each tier reaches a different depth, and the depth is reported rather than
implied:

| tier       | validator                          | depth                        |
|------------|------------------------------------|------------------------------|
| python     | `compile()` + `symtable` scope walk| syntax + unbound names       |
| typescript | `tsc` via the compiler API         | full typecheck               |
| rust       | `cargo check`                      | full typecheck               |
| java       | `javac` against the cordis4j stubs | full typecheck               |
| wasm       | `wasmtime compile`                 | validation (types + locals)  |
| go         | `go build` against pinned stc-go   | full compilation             |

Every one of those stops at compile or typecheck depth, so the matrix they
feed proves a construct emits and compiles per tier and never what it
evaluates to (issue #244). `EXECUTORS` at the bottom of this module is the
third question — the tiers whose runtime already exists in CI actually RUN the
program and assert its answer:

| tier       | executor                           | depth                        |
|------------|------------------------------------|------------------------------|
| python     | in-process exec of the emitted test| answer asserted              |
| typescript | vitest over the emitted test       | answer asserted              |
| go         | `go test` over the emitted test    | answer asserted              |

A validator that cannot run says so (`unavailable`) with the reason. That is
deliberately distinct from a pass: "no toolchain" must never read as "clean".
"""

from __future__ import annotations

import builtins
import json
import os
import shutil
import subprocess
import symtable
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

OK = "ok"
FAIL = "fail"

_TIMEOUT = 900


class Validator:
    """One tier's answer to 'does this emitted code hold up?'."""

    tier = ""
    depth = ""

    def unavailable(self) -> str | None:
        """None when the validator can run, else why it cannot."""
        return None

    def check(self, items: list[tuple[str, object]]) -> dict[str, tuple[str, str]]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# python — stdlib only, so this tier is always validated
# ---------------------------------------------------------------------------

class PythonValidator(Validator):
    tier = "python"
    depth = "syntax + unbound names"

    @staticmethod
    def _unbound(source: str, label: str) -> list[str]:
        """Names read in some scope that resolve to a global nobody defines.

        This is the shape of the bug that made the blind spot concrete: a
        provider method referencing a binding the emitter forgot to capture.
        `symtable` resolves scoping properly, so closures over enclosing
        frames (which the emitted `_body` relies on) are not flagged.
        """
        top = symtable.symtable(source, label, "exec")
        module_level = {sym.get_name() for sym in top.get_symbols()}
        found: list[str] = []

        def walk(table: symtable.SymbolTable) -> None:
            for sym in table.get_symbols():
                name = sym.get_name()
                if (sym.is_global() and not sym.is_assigned()
                        and name not in module_level
                        and not hasattr(builtins, name)):
                    found.append(f"{table.get_name()}: {name}")
            for child in table.get_children():
                walk(child)

        walk(top)
        return found

    def check(self, items):
        results: dict[str, tuple[str, str]] = {}
        for label, source in items:
            assert isinstance(source, str)
            try:
                compile(source, label, "exec")
            except SyntaxError as error:
                results[label] = (FAIL, f"syntax: {error}")
                continue
            unbound = self._unbound(source, label)
            results[label] = ((FAIL, "unbound: " + "; ".join(unbound))
                              if unbound else (OK, ""))
        return results


# ---------------------------------------------------------------------------
# typescript — the deepest validator available without a network
# ---------------------------------------------------------------------------

class TypeScriptValidator(Validator):
    tier = "typescript"
    depth = "full typecheck (tsc)"

    BASE = ROOT / "backends" / "typescript"

    def unavailable(self) -> str | None:
        if shutil.which("node") is None:
            return "node not on PATH"
        if not (self.BASE / "node_modules" / "typescript").is_dir():
            return "backends/typescript/node_modules/typescript missing (npm install)"
        return None

    def check(self, items):
        # Each case must sit directly under backends/typescript so the
        # emitted `import ... from '../runtime.ts'` resolves, and each needs
        # its own program: every case augments `cordis`'s Context with the
        # same service key, so one shared program would collide on the
        # augmentation rather than on anything real.
        with tempfile.TemporaryDirectory(dir=self.BASE, prefix=".conformance-") as tmp:
            directory = Path(tmp)
            names: dict[str, str] = {}
            for index, (label, source) in enumerate(items):
                path = directory / f"case_{index}.ts"
                path.write_text(source, encoding="utf-8")
                names[path.name] = label
            payload = json.dumps({
                "dir": str(directory),
                "files": sorted(names),
                "tsconfig": str(self.BASE / "tsconfig.json"),
            })
            result = subprocess.run(
                ["node", str(ROOT / "tools" / "tscheck.mjs")],
                input=payload, capture_output=True, text=True,
                cwd=self.BASE, timeout=_TIMEOUT,
            )
            if result.returncode != 0:
                reason = (result.stderr or result.stdout).strip().splitlines()
                raise RuntimeError("tscheck failed: " + (reason[-1] if reason else "?"))
            diagnostics = json.loads(result.stdout)

        return {label: ((FAIL, "; ".join(diagnostics[name])) if diagnostics.get(name)
                        else (OK, ""))
                for name, label in names.items()}


# ---------------------------------------------------------------------------
# rust — resolves cordis-rs offline first, from the local cargo registry
# ---------------------------------------------------------------------------
#
# "No network" is not the same question as "can cordis-rs be resolved?". A
# machine with a warm ~/.cargo resolves it fine with no route to the index,
# and this validator used to answer `unavailable` there — reporting the rust
# tier as unchecked on exactly the machines that could check it.
#
# The policy below is the one `backends/rust/test_emit_rust.py` already runs
# (`_cargo` / `_is_offline_resolve_failure`), kept deliberately identical:
# resolve `--offline` first, fall back to the networked resolve ONLY when the
# offline attempt failed for a *resolution* reason, and never let a compile
# error be reclassified as retryable. Laundering a real failure into
# "unavailable" is the one outcome worse than a false red.

_CRATES_IO: bool | None = None


def _crates_io_reachable() -> bool:
    """Whether the index can be reached. Cached: probed once per run."""
    global _CRATES_IO
    if _CRATES_IO is None:
        import socket  # noqa: PLC0415

        try:
            socket.create_connection(("index.crates.io", 443), timeout=5).close()
            _CRATES_IO = True
        except OSError:
            _CRATES_IO = False
    return _CRATES_IO


# Phrases cargo uses when an *offline resolve* could not find a crate. They are
# emitted before any compilation starts, which is what makes them safe to
# distinguish from a real build failure.
_OFFLINE_RESOLVE_MARKERS = (
    "you're using offline mode",
    "without the offline flag",
    "--offline was specified",
    "registry index was not found",
    "no matching package",
    "failed to select a version",
)

# Phrases that mean cargo got far enough to actually build something. If any
# appears, the result is real and must be surfaced as-is.
_REAL_FAILURE_MARKERS = (
    "error[e",
    "could not compile",
    "test result: failed",
    "panicked at",
)


def _is_offline_resolve_failure(proc: subprocess.CompletedProcess) -> bool:
    blob = ((proc.stderr or "") + (proc.stdout or "")).lower()
    if any(m in blob for m in _REAL_FAILURE_MARKERS):
        return False
    return any(m in blob for m in _OFFLINE_RESOLVE_MARKERS)


_NO_REGISTRY = ("cordis-rs is not in the local cargo registry and "
                "index.crates.io is unreachable — run cargo once with network "
                "to populate ~/.cargo")


def _cargo(subcommand: str, cwd: Path,
           *extra: str) -> tuple[subprocess.CompletedProcess | None, str | None]:
    """`cargo <subcommand>` — offline first, networked resolve as fallback.

    Returns `(completed, reason)`. `reason` is non-None only when the crate
    could not be resolved at all (empty registry *and* no index); every other
    outcome — including a genuine compile error — comes back as a real
    `CompletedProcess` for the caller to read.
    """
    offline = subprocess.run(
        ["cargo", subcommand, "--offline", *extra], cwd=cwd, text=True,
        capture_output=True, timeout=_TIMEOUT,
    )
    if offline.returncode == 0 or not _is_offline_resolve_failure(offline):
        return offline, None
    # The crates are not in the local registry. Only now is the network worth
    # the wait; without it there is nothing to resolve against.
    if not _crates_io_reachable():
        return None, _NO_REGISTRY
    return subprocess.run(
        ["cargo", subcommand, *extra], cwd=cwd, text=True,
        capture_output=True, timeout=_TIMEOUT,
    ), None


class RustValidator(Validator):
    tier = "rust"
    depth = "full typecheck (cargo check)"

    def unavailable(self) -> str | None:
        if shutil.which("cargo") is None:
            return "cargo not on PATH"
        # Ask the resolver, not the network: `generate-lockfile` runs the same
        # resolution `check` needs and compiles nothing, so a warm cache
        # answers in milliseconds and a cold one with no index says so.
        return self._resolve_reason()

    _RESOLVE_REASON: str | None = ""  # "" = not probed yet

    @classmethod
    def _resolve_reason(cls) -> str | None:
        if cls._RESOLVE_REASON != "":
            return cls._RESOLVE_REASON
        sys.path.insert(0, str(ROOT / "tools"))
        from conformance import emitter  # noqa: PLC0415 — avoids a cycle at import

        with tempfile.TemporaryDirectory() as tmp:
            crate = Path(tmp)
            (crate / "src").mkdir()
            (crate / "src" / "lib.rs").write_text("", encoding="utf-8")
            (crate / "Cargo.toml").write_text(
                emitter("rust").cargo_toml("revl_conformance"), encoding="utf-8")
            completed, reason = _cargo("generate-lockfile", crate)
        if reason is None and completed is not None and completed.returncode != 0:
            detail = (completed.stderr or "").strip().splitlines()
            reason = f"cargo could not resolve cordis-rs: {detail[-1] if detail else '?'}"
        cls._RESOLVE_REASON = reason
        return reason

    def check(self, items):
        sys.path.insert(0, str(ROOT / "tools"))
        from conformance import emitter  # noqa: PLC0415 — avoids a cycle at import

        with tempfile.TemporaryDirectory() as tmp:
            crate = Path(tmp)
            source_dir = crate / "src"
            source_dir.mkdir()
            (crate / "Cargo.toml").write_text(
                emitter("rust").cargo_toml("revl_conformance"), encoding="utf-8")

            names: dict[str, str] = {}
            modules = []
            for index, (label, source) in enumerate(items):
                module = f"case_{index}"
                (source_dir / f"{module}.rs").write_text(source, encoding="utf-8")
                names[f"{module}.rs"] = label
                modules.append(module)
            # One crate, one resolve, one check: 48 separate crates would each
            # re-resolve cordis-rs.
            (source_dir / "lib.rs").write_text(
                "".join(f"pub mod {m};\n" for m in modules), encoding="utf-8")

            result, reason = _cargo(
                "check", crate, "--message-format=json", "--quiet")
        if reason is not None:
            # unavailable() cleared this path, so reaching it means the
            # registry went away mid-run. Say so; never report it as 47 fails.
            raise RuntimeError(reason)

        failures: dict[str, list[str]] = {}
        for line in result.stdout.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = record.get("message") or {}
            if message.get("level") != "error":
                continue
            for span in message.get("spans") or []:
                stem = Path(span.get("file_name", "")).name
                if stem in names:
                    failures.setdefault(stem, []).append(
                        message.get("message", "error"))
                    break

        results = {label: ((FAIL, "; ".join(failures[name][:3])) if name in failures
                           else (OK, ""))
                   for name, label in names.items()}
        if result.returncode != 0 and not failures:
            # An error nobody could attribute to a case (resolve failure, ICE):
            # report it against every case rather than silently passing.
            detail = (result.stderr or "cargo check failed").strip().splitlines()
            note = detail[-1] if detail else "cargo check failed"
            results = {label: (FAIL, f"crate-level: {note}") for label in results}
        return results


# ---------------------------------------------------------------------------
# java — offline, but needs a real JDK (macOS ships a javac shim that errors)
# ---------------------------------------------------------------------------

def _java_package(source: str) -> str | None:
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("package ") and stripped.endswith(";"):
            return stripped[len("package "):-1].strip()
    return None


class JavaValidator(Validator):
    tier = "java"
    depth = "full typecheck (javac + cordis4j stubs)"

    STUBS = ROOT / "backends" / "java" / "stubs"

    @staticmethod
    def _javac() -> str | None:
        """A `javac` that actually works.

        macOS ships a `/usr/bin/javac` shim that is always on PATH and always
        errors when no JDK is installed, so `shutil.which` alone finds a
        binary that cannot compile anything. Probe each candidate and fall
        back to the usual keg-only install locations, which are deliberately
        *not* on PATH.
        """
        candidates = [shutil.which("javac")]
        try:
            home = subprocess.run(["/usr/libexec/java_home"], capture_output=True,
                                  text=True, timeout=30)
            if home.returncode == 0:
                candidates.append(str(Path(home.stdout.strip()) / "bin" / "javac"))
        except (OSError, subprocess.TimeoutExpired):
            pass
        candidates += ["/opt/homebrew/opt/openjdk/bin/javac",
                       "/usr/local/opt/openjdk/bin/javac"]
        for exe in candidates:
            if not exe or not Path(exe).exists():
                continue
            try:
                probe = subprocess.run([exe, "-version"], capture_output=True,
                                       text=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if probe.returncode == 0:
                return exe
        return None

    RELEASE = "21"

    @classmethod
    def _major(cls, javac: str) -> int | None:
        """javac's major version, or None if it cannot be parsed."""
        try:
            probe = subprocess.run([javac, "-version"], capture_output=True,
                                   text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        text = (probe.stdout + probe.stderr).strip()
        for token in text.replace("javac", "").split():
            head = token.split(".")[0]
            if head.isdigit():
                return int(head)
        return None

    def unavailable(self) -> str | None:
        javac = self._javac()
        if javac is None:
            return "no working javac (a JDK, not the macOS shim)"
        if not self.STUBS.is_dir():
            return "backends/java/stubs missing"
        # The emitted java uses language features from the release the backend
        # targets (switch patterns, records). An older javac rejects
        # `--release 21` outright, and reporting that as 47 failing cases would
        # be a lie about the emitter: nothing was checked.
        major = self._major(javac)
        if major is not None and major < int(self.RELEASE):
            return (f"javac {major} is older than the java {self.RELEASE} the "
                    f"backend targets (`--release {self.RELEASE}` is rejected)")
        return None

    def check(self, items):
        javac = self._javac()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "out"
            out.mkdir()
            names: dict[str, str] = {}
            files = []
            for index, (label, source) in enumerate(items):
                # Every case emits `Components`, so each arrives in its own
                # package (see `_emit_kwargs`); the file must sit in a matching
                # directory or javac rejects the declaration.
                package = _java_package(source) or f"case_{index}"
                directory = root / package
                if directory.exists():
                    # Two cases in the same package would overwrite each other;
                    # give this one its own root so both are really compiled.
                    directory = root / f"case_{index}" / package
                directory.mkdir(parents=True)
                path = directory / "Components.java"
                path.write_text(source, encoding="utf-8")
                names[f"{package}/Components.java"] = label
                files.append(str(path))

            result = subprocess.run(
                [javac, "--release", self.RELEASE, "-d", str(out)]
                + [str(s) for s in sorted(self.STUBS.rglob("*.java"))] + files,
                capture_output=True, text=True, timeout=_TIMEOUT,
            )
            failures: dict[str, list[str]] = {}
            for line in result.stderr.splitlines():
                if ": error:" not in line:
                    continue
                path = line.split(":", 1)[0]
                for key in names:
                    if path.endswith(key):
                        failures.setdefault(key, []).append(
                            line.split(": error:", 1)[1].strip())
                        break

        results = {label: ((FAIL, "; ".join(failures[name][:3])) if name in failures
                           else (OK, ""))
                   for name, label in names.items()}
        if result.returncode != 0 and not failures:
            detail = result.stderr.strip().splitlines()
            note = detail[-1] if detail else "javac failed"
            results = {label: (FAIL, f"javac: {note}") for label in results}
        return results


# ---------------------------------------------------------------------------
# wasm — the one tier whose validator is also its runtime
# ---------------------------------------------------------------------------

def wasmtime_binary() -> str | None:
    """`wasmtime` from PATH, falling back to the default rustup-style install.

    The installer writes to ~/.wasmtime/bin and only edits interactive shell
    profiles, so a non-login shell can have a perfectly good wasmtime and not
    see it. Falling back here keeps the tier validated instead of skipped.
    """
    found = shutil.which("wasmtime")
    if found:
        return found
    fallback = Path(os.path.expanduser("~/.wasmtime/bin/wasmtime"))
    return str(fallback) if fallback.is_file() and os.access(fallback, os.X_OK) else None


class WasmValidator(Validator):
    tier = "wasm"
    depth = "module validation (wasmtime compile)"

    def unavailable(self) -> str | None:
        return None if wasmtime_binary() else "wasmtime not found (PATH or ~/.wasmtime/bin)"

    def check(self, items):
        binary = wasmtime_binary()
        results: dict[str, tuple[str, str]] = {}
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for index, (label, modules) in enumerate(items):
                assert isinstance(modules, dict)
                errors = []
                for order, (name, wat) in enumerate(sorted(modules.items())):
                    path = directory / f"case_{index}_{order}.wat"
                    path.write_text(wat, encoding="utf-8")
                    result = subprocess.run(
                        [binary, "compile", str(path),
                         "-o", str(path.with_suffix(".cwasm"))],
                        capture_output=True, text=True, timeout=120,
                    )
                    if result.returncode != 0:
                        first = [ln for ln in result.stderr.splitlines() if ln.strip()]
                        errors.append(f"{name}: {first[0] if first else 'compile failed'}")
                results[label] = (FAIL, "; ".join(errors[:3])) if errors else (OK, "")
        return results


# ---------------------------------------------------------------------------
# go — offline-first `go build` against pinned stc-go (the go.mod pin the
# scenarios already carry). `go build` is a full compile, which for Go is a
# full typecheck — the same depth as the rust/java tiers.
# ---------------------------------------------------------------------------

_GO_SCENARIOS = ROOT / "backends" / "go" / "scenarios"


def _go_require_line() -> str | None:
    """The `require github.com/0xdenny218/stc-go <pin>` line from the pinned
    scenarios go.mod — the single source of truth for the stc-go version."""
    gomod = _GO_SCENARIOS / "go.mod"
    if not gomod.is_file():
        return None
    for line in gomod.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("require ") and "stc-go" in stripped:
            return stripped
    return None


def _go_package(source: str) -> str | None:
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("package "):
            return stripped[len("package "):].split()[0].strip()
    return None


def _proxy_reachable() -> bool:
    import socket  # noqa: PLC0415

    try:
        socket.create_connection(("proxy.golang.org", 443), timeout=5).close()
        return True
    except OSError:
        return False


class GoValidator(Validator):
    tier = "go"
    depth = "compilation (go build against pinned stc-go)"

    MODULE = "revl_go_conformance"

    def _module_skeleton(self, root: Path) -> None:
        """go.mod (module + pinned stc-go require) and the pinned go.sum."""
        require = _go_require_line() or (
            "require github.com/0xdenny218/stc-go "
            "v0.6.1-0.20260818143352-b3d6788a428e")
        (root / "go.mod").write_text(
            f"module {self.MODULE}\n\ngo 1.25.0\n\n{require}\n", encoding="utf-8")
        gosum = _GO_SCENARIOS / "go.sum"
        if gosum.is_file():
            shutil.copyfile(gosum, root / "go.sum")

    def _go_build(self, cwd: Path):
        """`go build ./...`, offline first then a networked retry only if the
        module is not already cached. Returns (CompletedProcess|None, reason)."""
        env = dict(os.environ)
        offline = subprocess.run(
            ["go", "build", "./..."], cwd=cwd, text=True, capture_output=True,
            timeout=_TIMEOUT, env={**env, "GOFLAGS": "-mod=mod", "GOPROXY": "off"},
        )
        blob = (offline.stderr or "") + (offline.stdout or "")
        download_failed = ("cannot find module" in blob
                           or "missing go.sum entry" in blob
                           or "GOPROXY=off" in blob
                           or "no required module provides" in blob
                           # an older local Go than the go.mod directive
                           # wants: GOTOOLCHAIN tries to fetch the newer
                           # toolchain itself, which the offline attempt
                           # always refuses. Same shape as a cold module
                           # cache — worth the networked retry.
                           or "toolchain not available" in blob
                           or "download go" in blob.lower())
        if offline.returncode == 0 or not download_failed:
            return offline, None
        # The module is not cached; only now is the network worth the wait.
        if not _proxy_reachable():
            return None, ("stc-go is not in the local module cache and "
                          "proxy.golang.org is unreachable — run `go build` "
                          "once with network to populate the module cache")
        return subprocess.run(
            ["go", "build", "./..."], cwd=cwd, text=True, capture_output=True,
            timeout=_TIMEOUT, env=env,
        ), None

    _RESOLVE_REASON: str | None = ""  # "" = not probed yet

    @classmethod
    def _resolve_reason(cls) -> str | None:
        if cls._RESOLVE_REASON != "":
            return cls._RESOLVE_REASON
        self = cls()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._module_skeleton(root)
            pkg = root / "probe"
            pkg.mkdir()
            (pkg / "probe.go").write_text(
                'package probe\n\nimport stc "github.com/0xdenny218/stc-go"\n\n'
                "var _ = stc.NewKey[any]\n", encoding="utf-8")
            _, reason = self._go_build(root)
        cls._RESOLVE_REASON = reason
        return reason

    def unavailable(self) -> str | None:
        if shutil.which("go") is None:
            return "go not on PATH"
        # A resolve probe compiles nothing of the corpus but confirms the
        # pinned stc-go is obtainable; a cold cache with no network says so
        # rather than reporting 45 phantom build failures.
        return self._resolve_reason()

    def check(self, items):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._module_skeleton(root)
            names: dict[str, str] = {}
            for index, (label, source) in enumerate(items):
                package = _go_package(source) or f"case_{index}"
                directory = root / package
                if directory.exists():
                    directory = root / f"case_{index}" / package
                directory.mkdir(parents=True)
                (directory / "gen.go").write_text(source, encoding="utf-8")
                names[package] = label

            result, reason = self._go_build(root)
        if reason is not None:
            # unavailable() cleared this path; reaching it means the cache went
            # away mid-run. Say so — never report it as N build failures.
            raise RuntimeError(reason)

        failures: dict[str, list[str]] = {}
        # `go build` errors read `<package>/gen.go:line:col: message`.
        for line in ((result.stderr or "") + "\n" + (result.stdout or "")).splitlines():
            stripped = line.strip()
            if ".go:" not in stripped:
                continue
            path = stripped.split(":", 1)[0]
            package = path.split("/", 1)[0]
            if package in names:
                failures.setdefault(package, []).append(
                    stripped.split(":", 3)[-1].strip() if stripped.count(":") >= 3
                    else stripped)

        results = {label: ((FAIL, "; ".join(failures[pkg][:3])) if pkg in failures
                           else (OK, ""))
                   for pkg, label in names.items()}
        if result.returncode != 0 and not failures:
            detail = (result.stderr or "go build failed").strip().splitlines()
            note = detail[-1] if detail else "go build failed"
            results = {label: (FAIL, f"build-level: {note}") for label in results.values()}
        return results


# ---------------------------------------------------------------------------
# execution — the third question, and the only one about MEANING (issue #244)
# ---------------------------------------------------------------------------
#
# Every validator above stops at compile or typecheck depth. That certifies a
# construct EMITS and COMPILES per tier; it never certifies what the construct
# EVALUATES TO, so two tiers can be green on the same case and disagree about
# the answer. `tsc --noEmit` covered backends/typescript/demo.ts the whole time
# its runtime assertions were failing, for exactly this reason.
#
# The validators below close that for the tiers whose runtime already exists in
# CI: python (in-process), typescript (vitest) and go (`go test`). Each runs the
# conformance case augmented with `tools/conformance.py`'s probe and the ONE
# assertion literal the corpus declares for that case. Because every executed
# tier asserts the same literal, a pass here is a cross-tier agreement on the
# ANSWER and not a per-tier "it ran": the only way to fail is to build, run, and
# compute something else.
#
# A case with no probe is not run and not counted; it carries the weaker
# compile-only claim and the matrix says so per row.

_RUNNER_KEY = {"python": "py", "typescript": "ts", "go": "go"}

# A trivial document, used once per tier to ask whether that runtime is here at
# all. A runner that answers "skip" is unavailable with its own reason —
# never a pass, for the same reason the compile validators refuse to conflate
# the two.
_LIVENESS = ('pub fn probe() -> Int { return 1 }\n'
             'test "liveness" { assert probe() == 1 }\n')


def _revl():
    """`(compile_source, RUNNERS)` — imported lazily so a validate-only run
    never pays for the runtime drivers."""
    src = str(ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from revl import compile_source  # noqa: PLC0415 — lazy by design
    from revl.test import RUNNERS  # noqa: PLC0415

    return compile_source, RUNNERS


def _run_program(tier: str, source: str) -> tuple[str, str]:
    """Run one probe program on *tier*. Returns `(outcome, detail)` where
    outcome is 'pass' / 'skip' / 'fail'. The runners narrate to stdout; that
    narration is captured and folded into the detail, because a `go test` or
    vitest failure says what went wrong there and nowhere else."""
    import contextlib  # noqa: PLC0415 — local to keep the module top lean
    import io  # noqa: PLC0415

    compile_source, runners = _revl()
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        try:
            outcome, message = runners[_RUNNER_KEY[tier]](compile_source(source))
        except Exception as error:  # noqa: BLE001 — a crashed runner is a datum
            outcome, message = "fail", f"{type(error).__name__}: {error}"
    narration = " | ".join(line.strip() for line in buffer.getvalue().splitlines()
                           if line.strip())
    detail = message if outcome != "fail" else f"{message}: {narration}"
    return outcome, detail.strip()


class ExecutionValidator(Validator):
    """One executed tier: does the emitted program produce the right ANSWER?

    `items` are `(label, probe program)` from
    `tools/conformance.py::executable_cases`, not emitted artifacts — the
    tier's own runner does the emitting, so what executes is the same output
    the compile validators check.
    """

    depth = "execution (the program runs and its answer is asserted)"

    def __init__(self, tier: str):
        self.tier = tier

    def unavailable(self) -> str | None:
        outcome, detail = _run_program(self.tier, _LIVENESS)
        if outcome == "pass":
            return None
        return detail or f"{self.tier} runtime did not run the liveness probe"

    def check(self, items):
        results: dict[str, tuple[str, str]] = {}
        for label, source in items:
            outcome, detail = _run_program(self.tier, source)
            if outcome == "skip":
                # unavailable() cleared this tier, so a skip here means the
                # runtime went away mid-run. Say so; never record it as ok.
                raise RuntimeError(f"{self.tier} stopped executing mid-run: {detail}")
            results[label] = (OK, "") if outcome == "pass" else (FAIL, detail[:400])
        return results


EXECUTORS: dict[str, ExecutionValidator] = {
    tier: ExecutionValidator(tier) for tier in ("python", "typescript", "go")
}


VALIDATORS: dict[str, Validator] = {
    "python": PythonValidator(),
    "typescript": TypeScriptValidator(),
    "rust": RustValidator(),
    "java": JavaValidator(),
    "wasm": WasmValidator(),
    "go": GoValidator(),
}
