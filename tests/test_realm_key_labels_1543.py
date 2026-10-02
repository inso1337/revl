"""Issue #1543: two keys isolated into one realm each resolve to their own provider.

cordis keys its provision store by the isolation LABEL: `ctx.isolate(name,
label)` records `{name: label}`, and `reflect.provide`/`get` read and write
`store[label]`. The runtime's own loader therefore mints one label per
`(realm, key)` (cordis-py `loader.Realm.access`, one symbol per key inside a
realm). revl's runtime shims minted one label per realm STRING
(`runtime.realm_label("wa")` on the py tier, `realmLabel("wa")` on the TS
tier), so `isolate db in realm("wa")` and `isolate api in realm("wa")` wrote
to the same slot: the second provider failed with "service ... has been
registered", and resolving `api` returned the `db` provider.

The fix keys the label registry by `(realm, key)` on both tiers. Equal realm
strings still share (the same `(realm, key)` always gets the same label), so
the G2 "same realm is the conflict" direction is unchanged.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_BACKEND = ROOT / "backends" / "python"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from revl import compile_source  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="resolving a live composition needs cordis-py "
           "(`sh backends/python/setup.sh`, run under backends/python/.venv)",
)

# `db` and `api` both isolated into realm `wa`; `Front` consumes both there.
TWO_KEYS_ONE_REALM = """
service Database { fn ping() -> Str }
service Api { fn up() -> Str }
component Db provides db: Database {
  isolate db in realm("wa")
  provide db { fn ping() = "db" }
}
component App requires db: Database provides api: Api {
  isolate db in realm("wa")
  isolate api in realm("wa")
  provide api { fn up() { return "api:" + db.ping() } }
}
component Front requires db: Database, api: Api {
  isolate db in realm("wa")
  isolate api in realm("wa")
}
"""


# --------------------------------------------------------------------------- #
# cordis-py (the reference tier), through the session driver
# --------------------------------------------------------------------------- #

@needs_cordis
def test_py_two_keys_in_one_realm_each_resolve_their_own_provider():
    """On main: `App` FAILED ("service \"api\" has been registered at <Db>"),
    and `call("api", "up")` resolved the `Db` object."""
    from revl.mcp.session import Session  # noqa: PLC0415

    session = Session()
    try:
        session.load(compile_source(TWO_KEYS_ONE_REALM, "k.rvl"))
        driver = session._driver
        states = {name: driver.FiberState(fiber.state).name
                  for name, fiber in driver.fibers.items()}
        assert states == {"Db": "ACTIVE", "App": "ACTIVE", "Front": "ACTIVE"}
        assert session.call("db", "ping", [])["result"] == "db"
        assert session.call("api", "up", [])["result"] == "api:db"
    finally:
        session.unload()


@needs_cordis
def test_py_resolved_keys_agrees_with_what_resolves():
    """`resolved_keys()` lists a key only when resolving it in its realm
    reaches THAT key's provider: every listed key's value answers its own
    service's methods. On main `api` was listed while it resolved to `Db`."""
    from revl.mcp.session import Session  # noqa: PLC0415

    session = Session()
    try:
        session.load(compile_source(TWO_KEYS_ONE_REALM, "k.rvl"))
        driver = session._driver
        assert driver.resolved_keys() == {"db", "api"}
        assert callable(getattr(driver.resolve_key("db"), "ping", None))
        assert callable(getattr(driver.resolve_key("api"), "up", None))
        assert getattr(driver.resolve_key("api"), "ping", None) is None
    finally:
        session.unload()


def test_py_label_registry_is_keyed_by_realm_and_key():
    import runtime  # noqa: PLC0415 — the py backend shim

    assert runtime.realm_label("wa", "db") is runtime.realm_label("wa", "db")
    assert runtime.realm_label("wa", "db") is not runtime.realm_label("wa", "api")
    assert runtime.realm_label("wa", "db") is not runtime.realm_label("wb", "db")


# --------------------------------------------------------------------------- #
# cordis (TypeScript), emitted code on the real runtime
# --------------------------------------------------------------------------- #

def _ts_node_modules() -> Path | None:
    """A `node_modules` with cordis installed: this worktree's, else the main
    checkout's beside it. Only the dependency is borrowed; the runtime shim
    and the emitter are always THIS tree's."""
    candidates = [ROOT / "backends" / "typescript" / "node_modules"]
    common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    if common.returncode == 0:
        git_dir = Path(common.stdout.strip())
        if not git_dir.is_absolute():
            git_dir = (ROOT / git_dir).resolve()
        candidates.append(git_dir.parent / "backends" / "typescript" / "node_modules")
    for path in candidates:
        if (path / "cordis" / "package.json").exists():
            return path
    return None


_TS_RUNNER = """
import { Context, FiberState } from 'cordis'
import { plug } from '__RUNTIME__'
import { Db, App, Front } from './app.ts'

const ctx = new Context()
const db = await plug(ctx, Db)
const app = await plug(ctx, App)
const front = await plug(ctx, Front)
await new Promise((resolve) => setTimeout(resolve, 0))
const name = (s: number) => (FiberState as any)[s] ?? String(s)
const out = {
  states: { Db: name(db.state), App: name(app.state), Front: name(front.state) },
  ping: front.state === FiberState.ACTIVE ? await front.ctx.db.ping() : null,
  up: front.state === FiberState.ACTIVE ? await front.ctx.api.up() : null,
}
console.log('RC_JSON ' + JSON.stringify(out))
"""


def test_ts_two_keys_in_one_realm_each_resolve_their_own_provider(tmp_path):
    """On main the TS shim's `realmLabel("wa")` was one symbol for both keys,
    so `App`'s `api` provision collided with `Db`'s `db` provision."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("cordis (TS): node not installed")
    modules = _ts_node_modules()
    if modules is None:
        pytest.skip("cordis (TS): node_modules/cordis not installed "
                    "(run npm ci in backends/typescript)")
    spec = importlib.util.spec_from_file_location(
        "revl_typescript_emit_1543", ROOT / "backends" / "typescript" / "emit.py")
    emit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emit)

    runtime_ts = str((ROOT / "backends" / "typescript" / "runtime.ts").resolve())
    ir = compile_source(TWO_KEYS_ONE_REALM, "k.rvl")
    (tmp_path / "app.ts").write_text(emit.emit(ir, runtime_import=runtime_ts),
                                     encoding="utf-8")
    (tmp_path / "runner.ts").write_text(
        _TS_RUNNER.replace("__RUNTIME__", runtime_ts), encoding="utf-8")
    os.symlink(modules, tmp_path / "node_modules")

    proc = subprocess.run([node, str(tmp_path / "runner.ts")], cwd=tmp_path,
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = next((ln for ln in proc.stdout.splitlines()
                 if ln.startswith("RC_JSON ")), None)
    assert line is not None, proc.stdout + proc.stderr
    result = json.loads(line[len("RC_JSON "):])
    assert result["states"] == {"Db": "ACTIVE", "App": "ACTIVE",
                                "Front": "ACTIVE"}, result
    assert result["ping"] == "db", result
    assert result["up"] == "api:db", result


# --------------------------------------------------------------------------- #
# cordis-rs: the same defect, in the emitter, NOT fixed here
# --------------------------------------------------------------------------- #

_RUST_SCENARIO = """
use revl_scenarios::{_revl_isolate_ctx, app, db, front};

#[test]
fn two_keys_in_one_realm_each_activate() {
    let root = cordis::Context::new();
    let d = _revl_isolate_ctx(&root, "db").plugin(db(), ());
    d.try_wait().unwrap();
    assert_eq!(d.state(), cordis::FiberState::Active, "Db");
    let a = _revl_isolate_ctx(&root, "app").plugin(app(), ());
    let r = a.try_wait();
    assert!(r.is_ok(), "App failed: {:?}", r.err());
    assert_eq!(a.state(), cordis::FiberState::Active, "App");
    let f = _revl_isolate_ctx(&root, "front").plugin(front(), ());
    f.try_wait().unwrap();
    assert_eq!(f.state(), cordis::FiberState::Active, "Front");
}
"""


@pytest.mark.xfail(
    strict=True,
    reason="cordis-rs keys `implementations` by `Isolation` alone and the "
           "emitted `_revl_realm(label)` mints one Isolation per realm string, "
           "so `api` collides with `db` (DuplicateService). The fix changes "
           "the emitted helper in backends/rust/emit.py AND "
           "selfhost/emit_rust.rvl, which is in the gate crate's digest, so it "
           "needs a sequenced crates/revl-gate regeneration (issue #1543).")
def test_rust_two_keys_in_one_realm_each_resolve_their_own_provider(tmp_path):
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("cordis-rs: cargo not installed")
    spec = importlib.util.spec_from_file_location(
        "revl_rust_emit_1543", ROOT / "backends" / "rust" / "emit.py")
    emit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emit)
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "lib.rs").write_text(
        emit.emit(compile_source(TWO_KEYS_ONE_REALM, "k.rvl")), encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text(emit.cargo_toml("revl_scenarios"),
                                         encoding="utf-8")
    (tmp_path / "tests" / "keys.rs").write_text(_RUST_SCENARIO, encoding="utf-8")
    proc = subprocess.run([cargo, "test", "--offline"], cwd=tmp_path,
                          capture_output=True, text=True, timeout=900)
    blob = proc.stdout + proc.stderr
    if proc.returncode != 0 and "test result" not in blob:
        pytest.skip("cordis-rs: the crate did not build offline (cold "
                    "~/.cargo?): " + blob[-400:])
    assert proc.returncode == 0, blob[-3000:]


# --------------------------------------------------------------------------- #
# stc-go: keyed by (realm, key) already; a guard, passes on main too
# --------------------------------------------------------------------------- #

def test_go_two_keys_in_one_realm_all_activate(tmp_path):
    """stc-go keys provisions by `provKey{realm, key}`, so the go tier never
    had this defect. Guard: all three components reach active."""
    from revl.run_go import go_runtime_reason  # noqa: PLC0415

    reason = go_runtime_reason()
    if reason is not None:
        pytest.skip(f"needs a resolvable stc-go toolchain: {reason}")
    source = tmp_path / "k.rvl"
    source.write_text(TWO_KEYS_ONE_REALM, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    proc = subprocess.run(
        [sys.executable, "-m", "revl", "run", str(source), "--backend", "go",
         "--once"], capture_output=True, text=True, input="", env=env,
        timeout=900)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    loaded = [line for line in out.splitlines() if "] load  |" in line]
    for name in ("Db", "App", "Front"):
        assert any(f"| {name} " in line and "state=active" in line
                   for line in loaded), out
