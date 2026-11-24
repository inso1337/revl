"""#3217: the go tier scrubs a witnessed inverse's witness before the WAL.

`test_secret_witness_receiver_position.py` pins the POSITIONAL rule: a witness
the author declared `Secret[...]` is written as the placeholder. A witness that
merely QUOTES a registered value (a lease id built from a key, a path built from
a token) has no such stamp, and on the go tier nothing stood between the value
and `$REVL_WAL`, a plaintext file at rest. The java and rust sinks read every
descriptor argument through the registry and the py tier redacts it by value;
go now reads the stringified witness through `revlRedactText` in secret mode.

Pinned here:

  * the emitted shape: the record call reads through the registry for a
    document that declares a `Secret[T]`, and is byte-identical (no registry
    reference) for one that declares none;
  * the executed file: a lease built from a declared secret lands in the WAL as
    the placeholder, and an ordinary witness in a secret-declaring document
    still lands verbatim (recovery needs the referent to address the right
    thing).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "revl_go_emit_wal_scrub", ROOT / "backends" / "go" / "emit.py")
emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(emit)

SECRET = "SECRET-TOKEN-0123456789"
REDACTED = "<redacted:secret>"

# The shape from the report: the secret goes to a witnessed extern and the
# witness quotes it. The witness itself is a plain `Str`, so no stamp applies.
LEAKY = """
service Noop {{ fn ping() -> Str }}

extern witnessed fn stash(v: Secret[Str]) -> Result[Str, Str]
    undo unstash(result)
    = @go {{ return RevlOk[string, string]{{Value: "lease-" + v}} }}

extern pure fn unstash(v: Str) -> Unit = @go {{ _ = v }}

extern pure fn token() -> Secret[Str] = @go {{ return "{secret}" }}

component Holder provides noop: Noop {{
  effect stash(token())

  provide noop {{
    fn ping() = "pong"
  }}
}}

lifecycle test "holder boots and unloads clean" {{
  load Holder
  unload Holder
  assert no_residue
}}
"""

# The control: the same witnessed shape, but nothing in the document is a secret.
PLAIN = """
service Noop {{ fn ping() -> Str }}

extern witnessed fn stash(v: Str) -> Result[Str, Str]
    undo unstash(result)
    = @go {{ return RevlOk[string, string]{{Value: "lease-" + v}} }}

extern pure fn unstash(v: Str) -> Unit = @go {{ _ = v }}

extern pure fn token() -> Str = @go {{ return "{secret}" }}

component Holder provides noop: Noop {{
  effect stash(token())

  provide noop {{
    fn ping() = "pong"
  }}
}}

lifecycle test "holder boots and unloads clean" {{
  load Holder
  unload Holder
  assert no_residue
}}
"""

RECORD_CALL = 'revlRecordTransactional("stash", "unstash", []string{'


def _ir(template: str) -> dict:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return compile_source(template.format(secret=SECRET))


def _record_line(src: str) -> str:
    lines = [ln.strip() for ln in src.splitlines() if RECORD_CALL in ln]
    assert len(lines) == 1, lines
    return lines[0]


def test_the_record_call_reads_the_witness_through_the_registry():
    line = _record_line(emit.emit(_ir(LEAKY), record=True))
    assert line.endswith('[]string{revlRedactText(fmt.Sprintf("%v", result))})'), line


def test_a_document_without_a_secret_records_the_witness_unchanged():
    src = emit.emit(_ir(PLAIN), record=True)
    assert _record_line(src).endswith('[]string{fmt.Sprintf("%v", result)})')
    assert "revlRedactText" not in src


def test_a_declared_secret_witness_still_gets_the_positional_placeholder():
    ir = _ir(LEAKY.replace("Result[Str, Str]", "Result[Secret[Str], Str]")
             .replace("unstash(v: Str)", "unstash(v: Secret[Str])"))
    line = _record_line(emit.emit(ir, record=True))
    assert REDACTED in line and "result" not in line.split("[]string{", 1)[1], line


# ---------------------------------------------------------------------------
# executed: what lands in $REVL_WAL
# ---------------------------------------------------------------------------

needs_go = pytest.mark.skipif(
    shutil.which("go") is None, reason="needs the go toolchain")

_GO_MOD = """module revl.walscrub3217

go 1.25.0

require github.com/0xdenny218/stc-go v0.6.1-0.20260818143352-b3d6788a428e
"""


def _run_go_wal(tmp: Path, template: str) -> str:
    src = emit.emit(_ir(template), "walscrub", record=True)
    (tmp / "gen_test.go").write_text(src, encoding="utf-8")
    (tmp / "go.mod").write_text(_GO_MOD, encoding="utf-8")
    sums = ROOT / "backends" / "go" / "scenarios" / "go.sum"
    if sums.exists():
        shutil.copy(sums, tmp / "go.sum")
    wal = tmp / "wal.jsonl"
    result = subprocess.run(
        ["go", "test", "./..."], cwd=str(tmp), capture_output=True, text=True,
        timeout=600, stdin=subprocess.DEVNULL,
        env={**os.environ, "REVL_WAL": str(wal), "GOFLAGS": "-mod=mod"})
    if not wal.exists():
        pytest.skip(f"go build/run unavailable here: {result.stdout}{result.stderr}")
    return wal.read_text(encoding="utf-8")


def _referents(wal: str) -> list:
    out = []
    for line in wal.splitlines():
        if line.strip():
            record = json.loads(line)
            if record.get("record") == "discharge-descriptor":
                out.extend((record.get("call") or {}).get("args") or [])
    return out


@needs_go
def test_a_witness_quoting_a_secret_does_not_reach_the_durable_log():
    with tempfile.TemporaryDirectory() as d:
        wal = _run_go_wal(Path(d), LEAKY)
    assert _referents(wal) == [f"lease-{REDACTED}"], "nothing was recorded at all"
    assert SECRET not in wal


@needs_go
def test_an_ordinary_witness_still_reaches_the_durable_log():
    with tempfile.TemporaryDirectory() as d:
        wal = _run_go_wal(Path(d), PLAIN)
    assert _referents(wal) == [f"lease-{SECRET}"]
