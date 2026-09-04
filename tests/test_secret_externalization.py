"""`Secret[T]` values must not be EXTERNALISED (roadmap item 256 Slice 3, §7b).

`tests/test_secret_flow.py` covers the static half: where a confidential value
is allowed to go. This file covers what happens to it once it legitimately gets
there. A declared `Secret[T]` receiver authorises disclosure TO THE RECEIVER; it
does not authorise revl to keep a plaintext copy. Two leaks are pinned here:

* an argument crossing at a declared `Secret[T]` service receiver was written
  verbatim into the durable WAL (0644, plaintext for the life of the log) and
  replayed back out of `revl_timeline` and `revl_fork` — straight into a model's
  context, the sink §7b exists to fence;
* a `Secret[Str]` CONFIG field was echoed to stdout by `revl run` and into the
  `revl_load` MCP response.

The redaction is placed at CAPTURE, not at each printer: the raw value never
enters a `Step` or a WAL record, so every renderer downstream — including one
added tomorrow — reads an already-redacted record. The false-positive tests below
are the other half of that claim: an ordinary argument is still recorded verbatim,
so the timeline stays worth reading.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "backends" / "python"))

from revl import compile_source  # noqa: E402
from revl.mcp.server import handle  # noqa: E402
from revl.recovery import DictWorld, recover  # noqa: E402

CANARY = "SEKRIT-CANARY-123"
PUBLIC = "PUBLIC-NOTE-456"

# The placeholder a confidential value is rendered as, spelled out here rather
# than imported: these tests must be able to RUN against a tree that has no
# redaction in it, and fail on the leak rather than on an import.
REDACTED_SECRET = "<redacted:secret>"

# A composition with both leak surfaces in it: a `Secret[Str]` config field, and
# an emission whose receiver declares `Secret[Str]` at position 0. `vault.note`
# is the control — same receiver, same crossing machinery, no `Secret[T]` — so a
# redaction that blanket-erased the timeline would fail on it.
#
# Written so it compiles BEFORE the fix as well as after, which is what makes
# these regressions rather than compile checks: the confidential value comes from
# a `Secret[Str]`-returning extern (§7a) and lands on the declared receiver (§7b),
# both of which the checker already admitted.
LEAKY = """
extern emission[payment.charge] fn charge(a: Str) -> Secret[Str]
  = @py { return "SEKRIT-CANARY-123" }

service Vault { emission fn store(x: Secret[Str]) -> Int
                emission fn note(m: Str) -> Int }
service Ops { emission fn go(u: Str) -> Int }

component VaultImpl provides vault: Vault {
  provide vault { fn store(x) = 1
                  fn note(m) = 1 }
}

component Agent requires vault: Vault provides ops: Ops {
  config { api_token: Secret[Str]
           plain_note: Str = "PUBLIC-NOTE-456" }
  provide ops {
    fn go(u) {
      let t = emit charge(u)
      emit vault.store(t)
      emit vault.note("PUBLIC-NOTE-456")
      return 1
    }
  }
}

component Driver requires ops: Ops {
  emit ops.go("u1")
}
"""

CONFIG = {"Agent": {"api_token": CANARY}}

needs_runtime = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="these tests boot a composition — install the cordis-py runtime with "
           "`sh backends/python/setup.sh` and run under "
           "`backends/python/.venv/bin/pytest`",
)


def _replay():
    import replay  # noqa: PLC0415 — backend module, path set above

    return replay


def _call(tool: str, arguments: dict) -> dict:
    response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}})
    return response["result"]["structuredContent"]


def _load(deployed: Path) -> dict:
    loaded = _call("revl_load", {"files": [str(deployed)], "config": CONFIG,
                                 "record": True})
    assert loaded["ok"] is True, loaded
    return loaded


@pytest.fixture
def deployed(tmp_path):
    """`LEAKY` as the OPERATOR deployed it: a `.rvl` file in a directory the
    operator sanctioned when starting the server (`revl mcp serve --root`).

    The MCP server does not trust the driving agent as a host-code author, so
    inline `source` declaring `= @py { ... }` is refused at admission. That is
    the right default here rather than something to switch off: these tests are
    about a credential-holding composition leaking to the MODEL, and the model is
    the agent driving the session — an operator who would not hand it the
    `api_token` would certainly not hand it authorship of the extern that
    returns one. So the composition is operator-authored and on disk (exactly as
    the `run_once` fixture already treats it for the CLI half), and the agent
    only names it. Nothing about the redaction under test changes with the load
    input: the trace, timeline, fork and origin records are built by the runtime
    from the booted composition either way."""
    from revl.mcp import server as server_mod  # noqa: PLC0415

    path = tmp_path / "leaky.rvl"
    path.write_text(LEAKY, encoding="utf-8")
    before = server_mod.AUTHORING
    server_mod.set_authoring_trust(roots=(str(tmp_path),))
    yield path
    server_mod.AUTHORING = before


@pytest.fixture(autouse=True)
def _fresh_session():
    import confidential  # noqa: PLC0415 — backend module, path set above
    from revl.mcp import server as server_mod  # noqa: PLC0415

    confidential.forget_secret_values()
    yield
    if server_mod.SESSION.loaded:
        server_mod.SESSION.unload()
    confidential.forget_secret_values()


@pytest.fixture
def run_once(tmp_path):
    """`revl run --once --wal` over `LEAKY`, exactly as the audit reproduced it.

    Returns `(stdout, wal_path)`. Driven as a subprocess because the WAL and the
    stdout trace are the two things under test and both belong to the CLI."""
    source = tmp_path / "leak.rvl"
    source.write_text(LEAKY, encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(json.dumps(CONFIG), encoding="utf-8")
    wal = tmp_path / "run.wal"

    def go(*extra: str) -> tuple[str, Path]:
        proc = subprocess.run(
            [sys.executable, "-m", "revl", "run", str(source),
             "--config", str(config), "--wal", str(wal), "--once", *extra],
            capture_output=True, text=True, timeout=300,
            stdin=subprocess.DEVNULL, cwd=str(tmp_path),
            env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin",
                 "HOME": str(tmp_path)})
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return proc.stdout, wal

    return go


def _wal_records(wal: Path) -> list:
    return [json.loads(line) for line in
            wal.read_text(encoding="utf-8").splitlines() if line.strip()]


# ===========================================================================
# 0. the marking the runtime reads
# ===========================================================================

def test_the_placeholder_is_one_string_on_both_sides_of_the_seam():
    """The placeholder is part of the WAL's on-disk contract: `revl recover`
    reads it back to refuse a replay it cannot honestly perform. The frontend
    constant and the backend one must therefore be the same bytes."""
    import confidential  # noqa: PLC0415

    from revl.taint import REDACTED_SECRET as frontend  # noqa: PLC0415

    assert confidential.REDACTED == frontend == REDACTED_SECRET


def test_the_ir_carries_the_secret_receiver_position():
    """The runtime cannot re-derive confidentiality: `extract_and_normalize`
    strips `Secret[T]` off the declared type before lowering. The lowering-time
    marking on the IR parameter is the only surviving channel."""
    ir = compile_source(LEAKY, "leak.rvl")
    store = ir["services"]["Vault"]["methods"]["store"]["params"]
    note = ir["services"]["Vault"]["methods"]["note"]["params"]
    assert store[0]["type"] == "Str"          # the qualifier is still stripped
    assert store[0]["secret"] is True
    assert "secret" not in note[0]            # additive: absent unless declared


def test_a_composition_with_no_qualifier_carries_no_marking():
    """Byte-identity: a program that uses no `Secret[T]` gets no new IR key."""
    ir = compile_source(
        "service S { emission fn f(a: Str) -> Int }\n"
        "component C provides s: S { provide s { fn f(a) = 1 } }\n", "plain.rvl")
    assert ir["services"]["S"]["methods"]["f"]["params"] == [
        {"name": "a", "type": "Str"}]


# ===========================================================================
# 1. HIGH 3 — the durable WAL
# ===========================================================================

@needs_runtime
def test_the_wal_never_holds_a_secret_receiver_argument(run_once):
    """LEAK 1. The log is created 0644 and is plaintext at rest for the life of
    the run, so a confidential argument in it is a disclosure to every reader of
    the filesystem — which is not who the `Secret[T]` receiver was."""
    _, wal = run_once()
    text = wal.read_text(encoding="utf-8")
    assert CANARY not in text
    assert REDACTED_SECRET in text


@needs_runtime
def test_the_wal_still_records_a_non_secret_argument_verbatim(run_once):
    """FALSE POSITIVE. Redaction is positional, driven by the declaration — it
    must not blanket-erase the log. `vault.note` crosses the same boundary with
    an ordinary `Str` and stays legible, and so does `ops.go`."""
    _, wal = run_once()
    args = {record["label"]: record["boundary"]["detail"]["args"]
            for record in _wal_records(wal)
            if record.get("kind") == "emission"}
    assert args == {"ops.go": ["u1"],
                    "vault.store": [REDACTED_SECRET],
                    "vault.note": [PUBLIC]}


@needs_runtime
def test_the_redacted_record_keeps_its_shape(run_once):
    """The record stays structurally intact — same keys, same arity, same
    position — so replay, the timeline and every consumer of `detail.args` keep
    working. Only the confidential bytes are gone."""
    _, wal = run_once()
    store = next(r for r in _wal_records(wal) if r.get("label") == "vault.store")
    detail = store["boundary"]["detail"]
    assert set(detail) == {"key", "method", "service", "args"}
    assert detail["key"] == "vault"
    assert detail["method"] == "store"
    assert detail["service"] == "Vault"
    assert len(detail["args"]) == 1
    assert store["boundary"]["class"] == "emission"


# ===========================================================================
# 2. HIGH 4 — the config field, on stdout
# ===========================================================================

@needs_runtime
def test_revl_run_does_not_print_a_secret_config_field(run_once):
    """LEAK 2. `revl run` prints the resolved config of every component. A
    credential passed as config was echoed there verbatim. The field is still
    named and still in place — only its value is gone."""
    stdout, _ = run_once()
    assert CANARY not in stdout
    line = next(line for line in stdout.splitlines() if "Agent.config" in line)
    assert f'api_token="{REDACTED_SECRET}"' in line
    assert f'plain_note="{PUBLIC}"' in line       # the control field is intact


# ===========================================================================
# 3. the MCP surfaces built from those records
# ===========================================================================

@needs_runtime
def test_revl_load_does_not_echo_a_secret_config_field(deployed):
    """LEAK 3. The same trace line, captured into the `revl_load` response and
    handed to the agent driving the session."""
    loaded = _load(deployed)
    assert CANARY not in json.dumps(loaded)
    line = next(event["detail"] for event in loaded["trace"]
                if event.get("subject") == "Agent.config")
    assert line == f'{{api_token="{REDACTED_SECRET}", plain_note="{PUBLIC}"}}'


@needs_runtime
def test_revl_timeline_does_not_hand_the_secret_to_the_model(deployed):
    """LEAK 4. `revl_timeline` is a real MCP tool: its response lands in a
    model's context window, the exact sink §7b singles out."""
    _load(deployed)
    _call("revl_call", {"key": "ops", "method": "go", "args": ["u1"]})

    blob = json.dumps(_call("revl_timeline", {}))
    assert CANARY not in blob
    assert REDACTED_SECRET in blob
    assert PUBLIC in blob          # the control crossing is still readable


@needs_runtime
def test_revl_fork_report_does_not_hand_the_secret_to_the_model(deployed):
    """LEAK 5. `revl_fork` enumerates the emissions that already crossed, each
    with its recorded arguments — the same record, a second reader."""
    _load(deployed)
    _call("revl_call", {"key": "ops", "method": "go", "args": ["u1"]})

    blob = json.dumps(_call("revl_fork", {"component": "Agent", "at": -1}))
    assert CANARY not in blob
    assert REDACTED_SECRET in blob


@needs_runtime
def test_a_call_argument_landing_on_a_secret_receiver_is_redacted_in_the_origin(deployed):
    """The `origin` a call tags its steps with rides into the WAL and is
    rendered as `whoRan` by `:bisect`. `vault.store` is a provided operation
    whose parameter is declared `Secret[Str]`, so calling it directly puts a
    confidential value in that origin — redacted at the same capture point."""
    _load(deployed)
    _call("revl_call", {"key": "vault", "method": "store", "args": [CANARY]})
    blob = json.dumps(_call("revl_timeline", {}))
    assert CANARY not in blob


@needs_runtime
def test_the_component_still_receives_the_real_config_value(deployed):
    """FALSE POSITIVE, and the point of the whole exercise: the component was
    granted the credential, so it still gets it. Only the log and the agent are
    fenced out."""
    _load(deployed)
    assert _call("revl_call", {"key": "ops", "method": "go",
                               "args": ["u1"]})["result"] == 1

    import runtime as runtime_mod  # noqa: PLC0415 — backend module, path set above

    assert runtime_mod.RESOLVED_CONFIG["Agent"]["api_token"] == CANARY


def test_a_config_schema_with_no_secret_field_is_untouched():
    """The redaction engages only on a declared qualifier: an ordinary schema
    renders exactly as before."""
    import runtime as runtime_mod  # noqa: PLC0415

    events: list = []
    unsubscribe = runtime_mod.add_trace(events.append)
    try:
        runtime_mod.ConfigSchema(
            [("host", "Str", "localhost"), ("port", "Int", 8080)],
            name="Plain").resolve({})
    finally:
        unsubscribe()
    assert events[-1] == ('Plain.config {host="localhost", port=8080} '
                          '[defaults: host, port]')


# ===========================================================================
# 4. recovery over a redacted log — honest, not silently wrong
# ===========================================================================

def test_recovery_refuses_to_reissue_an_inverse_it_cannot_reconstruct(tmp_path):
    """What replay LOSES. `World.key` is `receiver:args[0]`, so re-issuing an
    inverse whose argument is the placeholder would address the wrong referent
    and let recovery report a miss as a clean rollback. It refuses instead: the
    referent is named, the residue is outstanding, and nothing was attempted."""
    replay = _replay()
    path = str(tmp_path / "run.wal")
    wal = replay.WriteAheadLog(path).open()
    wal.record_discharge_descriptor(
        "transactional", receiver="fs", method="release",
        args=[REDACTED_SECRET], origin={"phase": "activation", "key": "fs"},
        witness=REDACTED_SECRET)
    wal.close()

    world = DictWorld()
    world.seed(f"fs:{REDACTED_SECRET}")
    report = recover(path, world=world)

    assert report["verdict"] == "rolled-back"
    assert report["residue"]["clean"] is False
    entry = next(r for r in report["residue"]["outstanding"]
                 if r["kind"] == "redacted-residue")
    assert entry["attemptedFlag"] is False
    assert entry["outcome"] == "unknown"
    assert entry["error"]["type"] == "redacted-argument"
    # nothing was popped on a guess: the referent is left for a human
    assert world.present(f"fs:{REDACTED_SECRET}")


def test_recovery_over_an_unredacted_log_is_unchanged(tmp_path):
    """FALSE POSITIVE. The refusal keys on the placeholder, so an ordinary
    descriptor still replays and still clears its referent."""
    replay = _replay()
    path = str(tmp_path / "run.wal")
    wal = replay.WriteAheadLog(path).open()
    wal.record_discharge_descriptor(
        "transactional", receiver="fs", method="release", args=["token-42"],
        origin={"phase": "activation", "key": "fs"}, witness="token-42")
    wal.close()

    world = DictWorld()
    world.seed("fs:token-42")
    report = recover(path, world=world)

    assert report["residue"]["clean"] is True
    assert not world.present("fs:token-42")


# ===========================================================================
# 5. the value marking
# ===========================================================================

def test_a_marked_value_is_scrubbed_wherever_it_reappears():
    """Belt and braces for a capture point with no positional marking of its
    own. Exact-value membership, so it can only ever scrub the same bytes a
    declaration already redacted."""
    import confidential  # noqa: PLC0415

    replay = _replay()
    confidential.register_secret_value(CANARY)
    timeline = replay.Timeline("C")
    timeline.record_emission("bus", "send", (CANARY, PUBLIC), "Bus", ("<f>", 1))
    assert timeline.steps[0].detail["args"] == [REDACTED_SECRET, PUBLIC]


def test_an_unmarked_value_is_never_scrubbed():
    """FALSE POSITIVE. No marking, no redaction — the timeline is verbatim."""
    replay = _replay()
    timeline = replay.Timeline("C")
    timeline.record_emission("bus", "send", (CANARY, PUBLIC), "Bus", ("<f>", 1))
    assert timeline.steps[0].detail["args"] == [CANARY, PUBLIC]


def test_a_short_value_is_never_marked():
    """A value too short for an exact match to mean anything is not remembered:
    blanket-erasing every `""` or `"ok"` in the timeline would buy no
    confidentiality and cost the whole record. The positional marking still
    covers the declared receiver, which is where a short secret crosses."""
    import confidential  # noqa: PLC0415

    confidential.register_secret_value("ok")
    assert confidential.is_secret_value("ok") is False


# --------------------------------------------------------------------------
# THE ONE PLACE `Secret[T]` DOES NOT COVER, and the warning that says so
# (issue #192). Everything above is about a value that arrives at run time.
# A config field with a LITERAL DEFAULT is different in kind: the author wrote
# the value into the source, so it lowers into the IR and into the emitted
# ConfigSchema like any other default, with the `secret` marking beside it
# rather than in place of it. That is by construction — the canary above works
# because a real value can be put in place — so this is a WARNING, and the
# tests below pin that the lowering is unchanged.
#
# The value-free form is item 256's `secret NAME for CAP`: `parser.SecretDecl`
# carries `name`, `capability`, `line` and no value field at all, so there is
# nothing for the IR to hold. The warning has to name it, or it tells a reader
# their belief is wrong without telling them what to write instead.
# --------------------------------------------------------------------------
import warnings  # noqa: E402

from revl.taint import LiteralSecretDefaultWarning  # noqa: E402

_LITERAL_DEFAULT = """service Ops { emission fn go(u: Str) -> Int }

component Agent provides ops: Ops {
  config { api_key: Secret[Str] = "SEKRIT-CANARY-123" }
  provide ops { fn go(u) = 1 }
}
"""


def _config_entry(src: str, field: str) -> dict:
    ir = compile_source(src)
    for comp in ir["components"]:
        for entry in comp.get("config") or []:
            if entry["name"] == field:
                return entry
    raise AssertionError(f"no config field {field!r} in the IR")


def _compile_quietly(src: str):
    """Compile with the warning turned into an error, so a test that asserts
    silence fails on a warning rather than on an assertion about a list."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", LiteralSecretDefaultWarning)
        return compile_source(src)


def test_a_secret_config_field_with_a_literal_default_warns():
    with pytest.warns(LiteralSecretDefaultWarning) as caught:
        compile_source(_LITERAL_DEFAULT)
    message = str(caught[0].message)
    assert "Agent.api_key" in message
    assert "does NOT keep that literal out of the compiled artifact" in message


def test_the_warning_names_the_value_free_form_as_the_alternative():
    """A warning that only says 'you are wrong' costs a reader an
    investigation. This one has to name `secret NAME for CAP`."""
    with pytest.warns(LiteralSecretDefaultWarning) as caught:
        compile_source(_LITERAL_DEFAULT)
    assert "secret api_key for <capability>" in str(caught[0].message)


def test_the_literal_still_reaches_the_ir_verbatim():
    """NOT a refusal, and the warning changes no bytes. The canary fixtures in
    this file need a real value in place to detect a leak downstream, so the
    day this starts stripping the default is the day they stop measuring
    anything. This is also the fact the warning is reporting."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", LiteralSecretDefaultWarning)
        entry = _config_entry(_LITERAL_DEFAULT, "api_key")
    assert entry["default"] == CANARY
    assert entry["secret"] is True


def test_a_secret_config_field_with_no_default_is_silent():
    """The value-free path, and the shape every fixture in this file uses: the
    value arrives at load time, so there is no literal to be wrong about."""
    ir = _compile_quietly(_LITERAL_DEFAULT.replace(
        f'api_key: Secret[Str] = "{CANARY}"', "api_key: Secret[Str]"))
    assert ir is not None


def test_an_unqualified_field_with_a_literal_default_is_silent():
    """FALSE POSITIVE. `Secret[T]` is what makes a default worth a sentence;
    an ordinary default is an ordinary default."""
    _compile_quietly(_LITERAL_DEFAULT.replace("Secret[Str]", "Str"))


def test_a_null_default_is_silent():
    """`= null` lowers to the same `None` as no default at all, so it cannot be
    distinguished here, and it is not a credential either way."""
    _compile_quietly(_LITERAL_DEFAULT.replace(
        f'Secret[Str] = "{CANARY}"', "Secret[Str] = null"))
