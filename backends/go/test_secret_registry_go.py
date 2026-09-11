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

This file pins three things:

  * the emitted shape: the registry is present for a document that declares a
    `Secret[T]`, registers every face, and is absent — byte-identically — for
    one that declares none;
  * that the derived escape set cannot drift from the encoder the tier really
    renders with, by deriving it from `json.Marshal` rather than restating it;
  * the runtime behaviour, by COMPILING and RUNNING the emitted registry
    against a canary that holds a quote and a backslash, through the same
    `json.Marshal` the runner's probe channel uses.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# run the emitted registry, through the encoder the runner renders with
# ---------------------------------------------------------------------------

needs_go = pytest.mark.skipif(shutil.which("go") is None, reason="go not installed")

_GO_MOD = "module revl_go_registry_probe\n\ngo 1.21\n"

_GO_HEADER = """package main

import (
\t"encoding/json"
\t"fmt"
\t"slices"
\t"strings"
\t"sync"
)

var _ = json.Marshal

"""

_GO_DRIVER = """
func main() {
\tRevlForgetSecrets()
\trevlMarkSecret(canary)
\t// the probe channel: the runner marshals a container before the line
\t// reaches the funnel
\tcontainer, _ := json.Marshal(map[string]string{"api_key": canary})
\tfmt.Println("container:", revlRedactText("probe "+string(container)))
\tfmt.Println("raw:", revlRedactText("trace key=" + canary))
\tfmt.Println("ordinary:", revlRedactText(ordinary) == ordinary)
}

var canary = %s

var ordinary = %s
"""


def _run_registry(tmp_path: Path, block: str) -> str:
    """Compile the sliced registry with a driver and run it."""
    (tmp_path / "go.mod").write_text(_GO_MOD, encoding="utf-8")
    (tmp_path / "main.go").write_text(
        _GO_HEADER + block + _GO_DRIVER % (json.dumps(CANARY), json.dumps(PUBLIC_URL)),
        encoding="utf-8")
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
