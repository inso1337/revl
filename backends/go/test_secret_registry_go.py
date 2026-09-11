"""The go tier's declared `Secret[T]` registry (roadmap item 421 F6 and its
F6(d) follow-up, the go half).

A config field declared `Secret[T]` is handed to the component's own host
binding — the legitimate use the checker does not refuse. The host body then
fails with a message that quotes its arguments, the plain shape a driver error
takes, and the emitted program registers the declared value at load
(`revlMarkSecret(cfg.ApiKey)`) so `revlRedactText` can scrub it out of the
runner's console, its probe channel and the seam wire. The py, ts and java
tiers carry the same registry; the java and rust halves of F6(d) live beside
this file, in `backends/java/test_secret_registry.py` and
`backends/rust/test_rust_secret_registry.py`.

F6(d) is the half this file's registry test did not cover: both redaction
stages match a value EXACTLY against text that has already been RENDERED. A
value holding a `"`, a `\\` or a control character comes back ESCAPED from
every encoder the tier renders it with, so the raw bytes match nothing and the
value crossed verbatim — while the identical value without the quote was
scrubbed everywhere. `revlRenderings` closes that by registering the encoder's
own body next to the raw value.

F6(e) is the half F6(d) left open, and it is the reason `revlRenderings` takes
a STRING. The string it was handed used to be `fmt.Sprintf("%v", v)`, which is
the whole story for a scalar — `%v` of a string IS the string — and no story at
all for a container, where `%v` is Go's DEBUG rendering (`[a b]`) while the
probe channel, the seam wire and the durable WAL all marshal the VALUE
(`["a","b"]`). The registered needle was then the json body of the debug form,
which is not a substring of the text any of those sinks writes, so a
`Secret[List[Str]]` — and further apart still a `Secret[Bytes]`, `%v` writing
`[104 101 …]` where `json.Marshal` writes base64 — crossed verbatim.
`revlRegisterValue` walks the value and registers its own json body beside the
display form's, the way the py tier's `_needles` already did.

This file pins four things:

  * the emitted shape: the registry is present for a document that declares a
    `Secret[T]`, registers every face, and is absent — byte-identically — for
    one that declares none;
  * that the derived escape set cannot drift from the encoder the tier really
    renders with, by deriving it from `json.Marshal` rather than restating it;
  * the runtime behaviour, by COMPILING and RUNNING the emitted registry
    against a canary that holds a quote and a backslash, through the same
    `json.Marshal` the runner's probe channel uses;
  * that the value is walked, not just its `%v` form, so a container and a byte
    string register the face the runner actually writes.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

# Load this backend's emitter under a unique module name — a bare `import emit`
# collides with the other backends' emitters when the suites run in one pytest
# invocation.
_spec = importlib.util.spec_from_file_location("revl_go_emit_secret", BACKEND / "emit.py")
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)

# Long enough that an exact match means something, and not a substring of
# anything else the run prints. The canary itself carries the two characters
# every encoder in play rewrites.
CANARY = 'SEC"RET\\GO-CANARY-421-F6'
# The container half: two leaves, each long enough to clear the bound on its
# own, so the test can tell "the container's own face was registered" apart
# from "only a leaf was".
CONTAINER_CANARY = ["CONTAINER-CANARY-421-F6-ALPHA", "CONTAINER-CANARY-421-F6-BETA"]
# The byte-string half: `%v` writes the byte VALUES where `json.Marshal` writes
# base64, so the two forms share no substring at all.
BYTES_CANARY = "BYTES-CANARY-421-F6"
# The ordinary value beside it: the control that the registry redacts what was
# declared and nothing else.
PUBLIC_URL = "pg://real-host-5432/app"
REDACTED_SECRET = "<redacted:secret>"

# The host body quotes its arguments, so the failure text a driver prints is
# the sink: nothing the author wrote interpolates the secret.
SCENARIO = """
extern emission[vault.connect] fn connect(key: Secret[Str], url: Str, user: Str) -> Unit
  = @go { return }

service Vault { emission fn open(user: Str) -> Str }

component Keeper provides vault: Vault {
  config { url: Str = "pg://main", api_key: Secret[Str] = "dev-vault-key" }
  provide vault {
    fn open(user) {
      emit connect(config.api_key, config.url, user)
      return "opened"
    }
  }
}
"""


def _compile(source: str) -> dict:
    """A literal default on a `Secret[T]` field warns (it is source, so it is in
    the IR); the scenario needs one so the load-time door exists."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return compile_source(source)


def _registry_block(code: str) -> str:
    """The emitted registry, sliced out of the generated program.

    The emitted unit is a whole stc-go package, which needs the pinned stc-go
    module to build. The registry itself is ordinary Go over `encoding/json`,
    `fmt`, `slices`, `strings` and `sync`, so slicing it out is what lets the
    runtime half below run on any box with a go toolchain and no module proxy.
    """
    start = code.index("// ---- declared Secret[T] confidentiality")
    tail = code.index("func RevlForgetSecrets()", start)
    end = code.index("\n}\n", tail) + len("\n}\n")
    return code[start:end]


# ---------------------------------------------------------------------------
# the emitted shape (runs everywhere; no toolchain needed)
# ---------------------------------------------------------------------------


def test_the_registry_is_emitted_for_a_document_that_declares_a_secret():
    code = emit.emit_placement(_compile(SCENARIO))
    assert f'const RevlRedactedSecret = "{REDACTED_SECRET}"' in code
    assert "revlMarkSecret(cfg.ApiKey)" in code
    # ...and only the declared field: the ordinary one beside it is not marked,
    # or the funnel would erase a DSN out of every trace line for no gain
    assert "revlMarkSecret(cfg.Url)" not in code


def test_a_secretless_document_is_byte_identical():
    """The registry is emitted only for a document that declares a `Secret[T]`,
    so every existing golden and the selfhost mirror stay untouched."""
    plain = SCENARIO.replace("api_key: Secret[Str]", "api_key: Str").replace(
        "key: Secret[Str]", "key: Str")
    code = emit.emit_placement(_compile(plain))
    assert "revlRedactText" not in code
    assert "revlMarkSecret" not in code
    assert "RevlRedactedSecret" not in code
    assert "revlRenderings" not in code


def test_the_registry_registers_the_escaped_face_too():
    """Item 421 F6(d). Registering only the raw bytes leaves a value holding a
    quote, a backslash or a control character unredacted at every sink that
    renders it, because the match is exact against already-rendered text."""
    code = emit.emit_placement(_compile(SCENARIO))
    assert "func revlRenderings(text string) []string {" in code
    # the remember path registers what revlRenderings hands back, not the raw
    # string alone
    assert "for _, face := range revlRenderings(text) {" in code


def test_the_escaped_face_is_derived_from_the_encoder_that_writes_it():
    """A hand-kept copy of an escape table drifts the moment the encoder moves.
    The face is taken from `json.Marshal` itself — the same call the placement
    runner's probe channel writes a container with — so it cannot."""
    code = emit.emit_placement(_compile(SCENARIO))
    block = _registry_block(code)
    assert "if encoded, err := json.Marshal(text); err == nil && len(encoded) >= 2 {" in block
    assert "body := string(encoded[1 : len(encoded)-1])" in block
    # the encoder has to be in scope wherever the registry is: both import
    # blocks that can carry a marking pull `encoding/json` in
    assert '\t"encoding/json"\n' in code


def test_the_bound_gates_the_raw_value_only():
    """`revlMinMarkable` is checked against the raw value, before any face is
    derived: an escape can only ever EXPAND, so a value that cleared the bound
    clears it in every escaped face too, and one that did not cannot be rescued
    by escaping."""
    block = _registry_block(emit.emit_placement(_compile(SCENARIO)))
    remember = block[block.index("func revlRememberSecret("):]
    assert remember.index("len(text) < revlMinMarkable") < remember.index("revlRenderings(text)")


def test_the_registry_is_the_legitimate_use():
    """The composition compiles: handing a declared `Secret[T]` config field to
    the component's own host binding is not a disclosure crossing."""
    ir = _compile(SCENARIO)
    keeper = next(c for c in ir["components"] if c["name"] == "Keeper")
    assert [f["name"] for f in keeper["config"] if f.get("secret")] == ["api_key"]


def test_the_value_itself_is_walked_not_just_its_display_form():
    """Item 421 F6(e). `revlRenderings` derives the escaped face of a STRING,
    and the string it was handed was `fmt.Sprintf("%v", v)` — for a container,
    Go's debug rendering. The probe channel, the seam wire and the durable WAL
    all marshal the VALUE instead, so a `Secret[List[Str]]` and a
    `Secret[Bytes]` wore a face nothing had registered."""
    code = emit.emit_placement(_compile(SCENARIO))
    block = _registry_block(code)
    assert "func revlRegisterValue(rv reflect.Value, depth int)" in block
    # the walk is gated on the value actually being a container, so the scalar
    # path F6(d) pinned stays byte-for-byte what it was
    assert "if rv := reflect.ValueOf(v); revlIsContainer(rv) {" in block
    assert "revlRegisterValue(rv, 0)" in block
    # the VALUE's own json body, beside the display form's...
    assert "json.Marshal(rv.Interface())" in block
    # ...while the scalar path F6(d) pinned still derives from the `%v` string
    assert "json.Marshal(text)" in block
    # and the byte-string arm, whose two forms share no substring
    assert 'rv.Type().Elem().Kind() == reflect.Uint8' in block
    # reflect has to be in scope wherever the registry is emitted
    assert '\t"reflect"\n' in code


def test_each_rendering_is_bounded_on_its_own_text():
    """The bound is applied per rendering, exactly as the py tier's `_needles`
    bounds each needle, so a container of short leaves contributes no short
    needle and a two-character item cannot start erasing ordinary text."""
    block = _registry_block(emit.emit_placement(_compile(SCENARIO)))
    walk = block[block.index("func revlRegisterText("):]
    assert walk.index("len(text) < revlMinMarkable") < walk.index("revlRenderings(text)")


# A live-component document, not a module: `_emit` returns the pure typed-core
# path early for a module-only document (that is why the emitted
# `emit_go_corpus/secrets.rvl` has no import block at all), so the F6(e) guard
# below is only reachable from a document that declares a component.
LIVE_SECRET = """
service Vault {
  fn store(token: Secret[Str]) -> Int
}

component Box provides vault: Vault {
  provide vault {
    fn store(token) = 0
  }
}
"""


def test_a_live_component_reaches_the_reflect_import_without_a_lifecycle():
    """Item 421 F6(e). `revlRegisterValue` walks a declared value with reflect,
    so a live component holding one needs the import even when the document has
    no lifecycle — the branch that already pulled reflect in (`testing`/`time`)
    is not taken. Go rejects a repeated import, so the guard is what keeps the
    secretless and lifecycle-free cases byte-identical.

    This is the module emitter's own entry point, which the self-host coverage
    oracle drives once per corpus document: the corpus cannot carry this
    document (a component document is outside what the port mirrors), so the
    import is pinned here instead of through a corpus case.
    """
    code = emit.emit(_compile(LIVE_SECRET))
    imports = code[code.index("import ("):code.index(")", code.index("import ("))]
    assert '\t"reflect"\n' in imports
    # from the F6(e) guard and not the lifecycle branch: neither is imported
    assert '"testing"' not in imports
    assert '"time"' not in imports
    assert "func revlRememberSecret(" in code
    # and the import is load-bearing — dropping the guard emits a unit whose
    # registry calls reflect with no import in scope, which does not compile
    assert "reflect." in code


# ---------------------------------------------------------------------------
# run the emitted registry, through the encoder the runner renders with
# ---------------------------------------------------------------------------

needs_go = pytest.mark.skipif(shutil.which("go") is None, reason="go not installed")

_GO_MOD = "module revl_go_registry_probe\n\ngo 1.21\n"

_GO_HEADER = """package main

import (
\t"encoding/json"
\t"fmt"
\t"reflect"
\t"slices"
\t"strings"
\t"sync"
)

var _ = json.Marshal
var _ = reflect.ValueOf

"""

# Token-substituted rather than `%`-formatted: the driver itself contains
# `fmt.Sprintf("%v", …)`, which Python's `%` operator would try to read as a
# conversion. Substitution also keeps the four canaries independent of order.
_GO_DRIVER = """
func main() {
\tRevlForgetSecrets()
\trevlMarkSecret(canary)
\trevlMarkSecret(containerCanary)
\trevlMarkSecret(bytesCanary)
\t// the probe channel: the runner marshals a container before the line
\t// reaches the funnel
\tcontainer, _ := json.Marshal(map[string]string{"api_key": canary})
\tfmt.Println("container:", revlRedactText("probe "+string(container)))
\tfmt.Println("raw:", revlRedactText("trace key=" + canary))
\tfmt.Println("ordinary:", revlRedactText(ordinary) == ordinary)
\t// a Secret[List[Str]] argument: what the runner writes is the VALUE's json
\t// body, which shares no substring with the `%v` form the registry used to
\t// derive its face from
\titems, _ := json.Marshal(containerCanary)
\tfmt.Println("items:", revlRedactText("probe "+string(items)))
\tfmt.Println("items-fmt:", revlRedactText("trace "+fmt.Sprintf("%v", containerCanary)))
\tfmt.Println("items-leaf:", revlRedactText("item=" + containerCanary[0]))
\t// a Secret[Bytes] argument rides the wire base64-encoded, and the decoded
\t// text is what a sink printing the payload writes
\tpayload, _ := json.Marshal(bytesCanary)
\tfmt.Println("bytes:", revlRedactText("probe "+string(payload)))
\tfmt.Println("bytes-decoded:", revlRedactText("payload "+string(bytesCanary)))
}

var canary = @CANARY@

var ordinary = @ORDINARY@

var containerCanary = @CONTAINER@

var bytesCanary = @BYTES@
"""


def _go_string_slice(values: list[str]) -> str:
    return "[]string{" + ", ".join(json.dumps(v) for v in values) + "}"


def _render_driver() -> str:
    return (_GO_DRIVER
            .replace("@CANARY@", json.dumps(CANARY))
            .replace("@ORDINARY@", json.dumps(PUBLIC_URL))
            .replace("@CONTAINER@", _go_string_slice(CONTAINER_CANARY))
            .replace("@BYTES@", "[]byte(" + json.dumps(BYTES_CANARY) + ")"))


def _run_registry(tmp_path: Path, block: str) -> str:
    """Compile the sliced registry with a driver and run it."""
    (tmp_path / "go.mod").write_text(_GO_MOD, encoding="utf-8")
    (tmp_path / "main.go").write_text(
        _GO_HEADER + block + _render_driver(), encoding="utf-8")
    result = subprocess.run(["go", "run", "."], cwd=tmp_path, text=True,
                            capture_output=True, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@needs_go
def test_no_sink_carries_the_escaped_value_when_run(tmp_path):
    block = _registry_block(emit.emit_placement(_compile(SCENARIO)))
    trace = _run_registry(tmp_path, block)
    # the container render is the escaped face, and it is scrubbed
    assert f"container: probe {{\"api_key\":\"{REDACTED_SECRET}\"}}" in trace, trace
    assert CANARY not in trace, trace
    # the raw face still matches, so the fix is additive
    assert f"raw: trace key={REDACTED_SECRET}" in trace, trace
    # no over-redaction: an ordinary value beside it is verbatim
    assert "ordinary: true" in trace, trace


@needs_go
def test_a_container_registers_the_face_the_runner_writes(tmp_path):
    """Item 421 F6(e), at runtime. The runner writes the VALUE's json body; the
    registry used to register the json body of the value's `%v` form, which is
    a different string for any container and longer than the value itself, so
    no needle matched anything."""
    block = _registry_block(emit.emit_placement(_compile(SCENARIO)))
    trace = _run_registry(tmp_path, block)
    for leaf in CONTAINER_CANARY:
        assert leaf not in trace, trace
    assert BYTES_CANARY not in trace, trace
    # the value's own json body — what the probe channel writes — is scrubbed
    assert f"items: probe {json.dumps(CONTAINER_CANARY, separators=(',', ':'))}" not in trace, trace
    assert "items: probe [" + REDACTED_SECRET + "]" in trace, trace
    # the `%v` form the trace line writes is scrubbed too, so the walk is
    # additive to F6(d) rather than a replacement for it. The whole container
    # is one registered needle, so its brackets are consumed with it.
    assert f"items-fmt: trace {REDACTED_SECRET}" in trace, trace
    # a leaf, for a sink that prints one item
    assert f"items-leaf: item={REDACTED_SECRET}" in trace, trace
    # Bytes: both the base64 the wire carries and the decoded payload
    assert f"bytes: probe \"{REDACTED_SECRET}\"" in trace, trace
    assert f"bytes-decoded: payload {REDACTED_SECRET}" in trace, trace


@needs_go
def test_with_the_escaped_face_stripped_the_value_leaks(tmp_path):
    """Non-vacuity: `revlRenderings` is what stands between the probe channel
    and the value. Register the raw face alone — the pre-F6(d) shape — and the
    same run prints the canary."""
    block = _registry_block(emit.emit_placement(_compile(SCENARIO)))
    raw_only = block.replace("range revlRenderings(text)", "range []string{text}")
    assert raw_only != block, "the registration no longer reads through revlRenderings"
    trace = _run_registry(tmp_path, raw_only)
    # what leaks is the ESCAPED face, which is the whole point: the raw bytes
    # are not what the probe channel writes
    escaped = json.dumps(CANARY)[1:-1]
    assert escaped != CANARY
    assert f"container: probe {{\"api_key\":\"{escaped}\"}}" in trace, trace
    # the raw face is still covered, which is why the fix is additive and not
    # a replacement: only the escaped face was missing
    assert f"raw: trace key={REDACTED_SECRET}" in trace, trace


@needs_go
def test_with_the_value_walk_stripped_a_container_leaks(tmp_path):
    """Non-vacuity for F6(e): the walk is what stands between a container and
    the probe channel. Drop it — the shape #894 landed — and the same run
    prints the value's own json body, which is exactly what the runner writes."""
    block = _registry_block(emit.emit_placement(_compile(SCENARIO)))
    walk = (
        "\tif rv := reflect.ValueOf(v); revlIsContainer(rv) {\n"
        "\t\trevlRegisterValue(rv, 0)\n"
        "\t}\n"
    )
    stripped = block.replace(walk, "\t// the value walk is what this test removes\n")
    assert stripped != block, "revlRememberSecret no longer walks the value"
    trace = _run_registry(tmp_path, stripped)
    # the display form was covered all along, which is why the gap survived a
    # green suite: the runner does not write that form
    assert f"items-fmt: trace {REDACTED_SECRET}" in trace, trace
    # ...and these are what leaked. The container's own json body, and with it
    # every leaf, because the only needle registered is the json body of the
    # `%v` string — a longer, differently punctuated string that is not a
    # substring of what the runner writes.
    assert f"items: probe {json.dumps(CONTAINER_CANARY, separators=(',', ':'))}" in trace, trace
    assert f"items-leaf: item={CONTAINER_CANARY[0]}" in trace, trace
    # Bytes are further apart still: `%v` writes the byte values, so the
    # registered needle shares nothing at all with either the base64 the wire
    # carries or the decoded payload a sink would print.
    assert f"bytes: probe \"{base64.b64encode(BYTES_CANARY.encode()).decode()}\"" in trace, trace
    assert f"bytes-decoded: payload {BYTES_CANARY}" in trace, trace
    # the escaped scalar face is unaffected by the walk being gone
    assert f"raw: trace key={REDACTED_SECRET}" in trace, trace
