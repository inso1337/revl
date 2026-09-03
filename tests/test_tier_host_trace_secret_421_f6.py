"""A declared `Secret[T]` must not reach a tier's host trace verbatim
(roadmap item 421 F6, the sibling-tier half).

F6 was closed on the py tier by putting the scrub at `runtime._record` — the one
choke point every trace event passes through — and by having the emitted program
register the declared marking itself, at both ends: the RECEIVER (a provide
method whose service declares that parameter `Secret[T]`) and the ORIGIN (an
extern whose declared return was `Secret[T]`, where the value enters the value
world).

The same shape of sink exists on three sibling tiers, and none of them had any
notion of a declared `Secret[T]` at runtime:

  * typescript — `runtime.record` interpolates a `Map` key and value, a
    `pool.query`/`pool.execute` sql, a stream item, a job name and a component's
    resolved config into `hostLog`, the tier's exported observability channel
    (`onHostEvent` forwards it to any subscriber a host installs);
  * go — the emitted `hostRecord` interpolates a `pool.query`/`pool.execute` sql
    into `_hostLog`, which `HostMarks` hands back.

The other three tiers have no such sink to close: the java and wasm host objects
record nothing at all, and the rust host objects record nothing a program value
reaches (its `Pool::execute` takes the sql without tracing it, and its stream
marks are fed by the host side, which no component body can reach).

Both tiers with a sink now carry the same funnel at the same kind of choke
point, driven by the same IR markings the py emitter reads
(`params[i]["secret"]`, `externs[i]["secret_return"]`, and a config field's
`secret`). This file pins BOTH halves of that claim:

  * the marking is emitted where the declaration says it should be, and the
    funnel is present, for a composition that declares a `Secret[T]`;
  * a composition that declares none is emitted BYTE-IDENTICALLY to before, so
    the funnel costs a secret-free program nothing and no golden moves.

The end-to-end assertion — that the trace a running composition produces carries
the placeholder and not the value — lives with each tier's own runtime
(`backends/typescript/tests/host_trace_secret.test.ts` for the ts tier, and the
go tier's own exec test for `HostMarks`).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Spelled out rather than imported, so this file can RUN against a tree with no
# redaction in it and fail on the leak instead of on an import.
REDACTED_SECRET = "<redacted:secret>"


def _backend(name: str):
    """Import one backend's `emit` module under its own name.

    The six backends each ship a module called `emit`; importing more than one
    into a single interpreter by bare name would alias them. Load each by path
    under a distinct module name instead.
    """
    import importlib.util  # noqa: PLC0415

    alias = f"_revl_{name}_emit"
    if alias in sys.modules:
        return sys.modules[alias]
    path = ROOT / "backends" / name / "emit.py"
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(module)
    return module


# The audit's shape: a `Secret[Str]` ORIGIN (item 256 §7a) whose value is used
# as a host key, plus a `Secret[Str]` RECEIVER on a provide method.
SECRETFUL = """
extern emission[vault.mint] fn mint_token(u: Str) -> Secret[Str]
  = @py { return "TOKEN" }
  = @ts { return "TOKEN" }
  = @go { return "TOKEN" }

service Cache {
  emission fn put(u: Str) -> Str
  emission fn store(token: Secret[Str]) -> Str
}

component UserCache provides cache: Cache {
  let pool = effect Pool.open("pg://main", 2) undo pool.close()

  provide cache {
    fn put(u) {
      let t = emit mint_token(u)
      effect pool.execute(t)
      undo   pool.close()
      return "ok"
    }
    fn store(token) {
      effect pool.execute(token)
      undo   pool.close()
      return "ok"
    }
  }
}
"""

# The same composition with the two `Secret[...]` qualifiers removed, and
# nothing else changed. Emitting this must be byte-identical to emitting it
# against the tree before the funnel existed.
SECRETLESS = SECRETFUL.replace("Secret[Str]", "Str")


@pytest.fixture(scope="module")
def secretful_ir():
    from revl import compile_source  # noqa: PLC0415

    return compile_source(SECRETFUL)


@pytest.fixture(scope="module")
def secretless_ir():
    from revl import compile_source  # noqa: PLC0415

    return compile_source(SECRETLESS)


# ---------------------------------------------------------------------------
# the markings the emitters read
# ---------------------------------------------------------------------------


def test_the_ir_carries_both_ends_of_the_declaration(secretful_ir, secretless_ir):
    """`taint.py` strips the qualifier before lowering, so these two stamps are
    the ONLY surviving record of it. Every tier's marking is driven by them, so
    if this changes shape, all four emitters go quiet at once."""
    externs = {ext["name"]: ext for ext in secretful_ir["externs"]}
    assert externs["mint_token"]["secret_return"] is True
    assert externs["mint_token"]["returns"] == "Str"
    store = secretful_ir["services"]["Cache"]["methods"]["store"]
    assert store["params"][0]["secret"] is True

    # ...and the control: absent unless the author wrote the qualifier.
    plain = {ext["name"]: ext for ext in secretless_ir["externs"]}
    assert "secret_return" not in plain["mint_token"]
    plain_store = secretless_ir["services"]["Cache"]["methods"]["store"]
    assert "secret" not in plain_store["params"][0]


# ---------------------------------------------------------------------------
# typescript
# ---------------------------------------------------------------------------


def test_ts_emits_both_markings(secretful_ir):
    code = _backend("typescript").emit(secretful_ir)
    # the ORIGIN: the declared name becomes a marking wrapper over the body
    assert "host.secretResult(_revl_secret_mint_token(u))" in code
    # the RECEIVER: registered at the head of the declared method
    assert "host.markSecret(token)" in code
    # ...and not on the method that declares no secret parameter
    assert "host.markSecret(u)" not in code


def test_ts_secretless_document_is_untouched(secretless_ir):
    code = _backend("typescript").emit(secretless_ir)
    assert "secretResult" not in code
    assert "markSecret" not in code
    # the extern still renders as the plain exported function it always did
    assert "export function mint_token(u: string): string {" in code


def test_ts_runtime_scrubs_at_the_one_choke_point():
    """The scrub is at `record`, not at each call site: a sink added tomorrow
    reads an already-redacted line instead of having to remember to redact."""
    source = (ROOT / "backends" / "typescript" / "runtime.ts").read_text("utf-8")
    assert f"export const REDACTED_SECRET = '{REDACTED_SECRET}'" in source
    assert "const scrubbed = redactText(entry)" in source
    assert "hostLog.push(scrubbed)" in source
    # a declared `Secret[T]` config field is named but not spelled out
    assert "secretFields.has(key) ? JSON.stringify(REDACTED_SECRET)" in source


# ---------------------------------------------------------------------------
# go
# ---------------------------------------------------------------------------


def test_go_emits_the_funnel_and_both_markings(secretful_ir):
    code = _backend("go").emit_placement(secretful_ir)
    assert f'const RevlRedactedSecret = "{REDACTED_SECRET}"' in code
    assert "\top = revlRedactText(op)\n" in code
    assert "return revlSecretResult(revlSecret_mint_token(u))" in code
    assert "revlMarkSecret(token)" in code
    assert "revlMarkSecret(u)" not in code


def test_go_registers_a_secret_config_field_at_load():
    """The THIRD feeder of the funnel: an operator-supplied credential declared
    on the component rather than on an extern return or a parameter.

    It arrives once, at `Load<Comp>`, the single door both the lifecycle path
    and the placement runner's `RevlLoad` go through — so the registration sits
    there, not at each `config.<field>` read. The py tier registers its config
    values in `ConfigSchema._park` and the ts tier in its config binder; go read
    the marking only to switch the funnel ON, so the same declaration used to
    mean different things on different tiers."""
    from revl import compile_source  # noqa: PLC0415

    src = (
        'extern pure fn forget() -> Unit = @go { return }\n'
        'service Vault { fn lookup(name: Str) -> Str }\n'
        'component Keeper provides vault: Vault {\n'
        '  config { url: Str = "pg://x", api_key: Secret[Str] = "k" }\n'
        '  let pool = effect Pool.open(config.url, 2) undo pool.close()\n'
        '  provide vault {\n'
        '    fn lookup(name) {\n'
        '      effect pool.execute(config.api_key)\n'
        '      undo   forget()\n'
        '      return "ok"\n'
        '    }\n  }\n}\n'
    )
    code = _backend("go").emit_placement(compile_source(src))
    assert "func LoadKeeper(target *stc.Context, cfg KeeperConfig) *stc.Fiber {" in code
    assert "revlMarkSecret(cfg.ApiKey)" in code
    # the false-positive control: an ordinary field is not remembered, or the
    # funnel would erase a DSN out of every trace line for no gain
    assert "cfg.Url" not in code.split("revlMarkSecret(cfg.ApiKey)")[1].split("\n")[0]
    assert "revlMarkSecret(cfg.Url)" not in code


def test_go_config_registration_is_absent_without_the_qualifier():
    """A component whose config declares no `Secret[T]` emits the `Load` it
    always did."""
    from revl import compile_source  # noqa: PLC0415

    src = (
        'service Vault { fn lookup(name: Str) -> Str }\n'
        'component Keeper provides vault: Vault {\n'
        '  config { url: Str = "pg://x" }\n'
        '  provide vault { fn lookup(name) = "ok" }\n}\n'
    )
    code = _backend("go").emit_placement(compile_source(src))
    assert "revlMarkSecret" not in code


def test_go_secretless_document_is_untouched(secretless_ir):
    code = _backend("go").emit_placement(secretless_ir)
    assert "revlRedactText" not in code
    assert "revlSecretResult" not in code
    assert "revlMarkSecret" not in code
    assert "RevlRedactedSecret" not in code
    # the trace choke point is exactly the three lines it always was
    assert ("func hostRecord(op string) {\n\t_hostMu.Lock()\n"
            "\t_hostLog = append(_hostLog, op)\n\t_hostMu.Unlock()\n}") in code
