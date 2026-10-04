"""A provider's emission made inside a caller's crossing is counted once.

`emit tickets.file("T1") compensate tickets.withdraw("T1")` is ONE crossing in
the world. The caller records it (`tickets.file`, the record the compensation
names). The provider records the host emission its method makes to answer it
(`file_host`, recorded since #1603 for `return emit`, and always for an `emit`
statement). Recovery counted the two as two residues: a full recover that ran
every compensation still exited 1 on the provider's record, which no
compensation names.

The recorder now stamps a step recorded while a required-service crossing is
being answered with `within: {seq, component, label}` of that crossing
(`replay._ENCLOSING`, set by `_ServiceProxy`), and recovery counts a nested
emission with its enclosing record (`recovery._split_nested`): offset, settled
or residue as that crossing is, never a second residue. The record stays in
the WAL. Placement seams are out of scope: the two records sit in different
process WALs and neither is folded.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.recovery import _split_nested, recover, render  # noqa: E402

needs_cordis = pytest.mark.skipif(
    importlib.util.find_spec("cordis") is None,
    reason="the WAL is written by a live cordis-py composition; install the "
           "pinned fork with `sh backends/python/setup.sh`")


def _host(line: str, ret: str = "None") -> str:
    return (f"    import os\n"
            f"    with open(os.environ['REVL_R_LOG'], 'a', encoding='utf-8') as f:\n"
            f"        f.write({line} + chr(10))\n"
            f"    return {ret}\n")


def _composition(file_body: str, call: str = "emit tickets.file(\"T1\")") -> str:
    return f"""
extern emission fn file_host(t: Str) -> Str = @py {{
{_host("'file:' + t", "'id-' + t")}}}
extern emission fn note(t: Str) -> Unit = @py {{
{_host("'note:' + t")}}}
extern emission fn withdraw_host(t: Str) -> Unit = @py {{
{_host("'withdraw:' + t")}}}
service Tickets {{
  emission fn file(t: Str) -> Str
  emission fn withdraw(t: Str)
}}
component Desk provides tickets: Tickets {{
  provide tickets {{
    fn file(t) {{ {file_body} }}
    fn withdraw(t) {{ emit withdraw_host(t) }}
  }}
}}
extern emission fn crash() -> Unit = @py {{
    import os
    os._exit(3)
}}
component Agent requires tickets: Tickets {{
  {call} compensate tickets.withdraw("T1")
  emit crash()
}}
"""


def _crash(tmp_path: Path, source: str) -> Path:
    (tmp_path / "app.rvl").write_text(source, encoding="utf-8")
    code = ("import sys\nfrom revl.__main__ import main\n"
            "sys.exit(main(['run', 'app.rvl', '--wal', 'run.wal']))\n")
    path = os.pathsep.join(p for p in (str(ROOT / "src"),
                                       os.environ.get("PYTHONPATH")) if p)
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=str(tmp_path),
        env=dict(os.environ, PYTHONPATH=path,
                 REVL_R_LOG=str(tmp_path / "world.log")),
        capture_output=True, text=True, timeout=600)
    assert proc.returncode == 3, proc.stdout[-3000:] + proc.stderr[-3000:]
    return tmp_path / "run.wal"


def _effects(wal: Path) -> dict:
    return {r["label"]: r for r in map(json.loads, wal.read_text().splitlines())
            if r.get("record") == "effect"}


@needs_cordis
@pytest.mark.parametrize("body, inner", [
    ("return emit file_host(t)", "file_host"),
    ('emit note(t)\n    return "ok"', "note"),
], ids=["return-emit", "emit-statement"])
def test_the_providers_emission_names_the_crossing_it_was_made_inside(
        tmp_path, body, inner):
    wal = _crash(tmp_path, _composition(body))
    effects = _effects(wal)

    outer = effects["tickets.file"]
    assert effects[inner]["component"] == "Desk"
    assert effects[inner]["within"] == {
        "seq": outer["seq"], "component": "Agent", "label": "tickets.file"}
    # the caller's own crossing is not inside anything
    assert "within" not in outer

    report = recover(str(wal))
    assert [n["label"] for n in report["nested"]] == [inner]
    # one crossing, one record in the residue: the caller's
    labels = sorted(e["label"] for e in report["unreconstructible"])
    assert labels == ["crash", "tickets.file"]
    assert f"nested   {inner}" in render(report)


# ---------------------------------------------------------------------------
# hand-made WALs


def _effect(seq: int, component: str, label: str, within=None) -> dict:
    record = {"record": "effect", "seq": seq, "component": component,
              "stepIndex": seq, "kind": "emission", "label": label,
              "site": None, "source": None, "origin": {"phase": "activation"},
              "boundary": {"class": "emission", "referent": "process-crossing",
                           "detail": {"key": label, "method": label,
                                      "service": None, "args": []}},
              "inverse": {"reconstructible": False,
                          "reason": "an emission is a one-way crossing"}}
    if within is not None:
        record["within"] = within
    return record


HEADER = {"record": "header", "walVersion": 1, "generation": 1, "guarantee": "g"}


def _write(path: Path, records: list) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def test_only_an_emission_inside_a_crossing_on_this_wal_is_nested():
    outer = _effect(1, "Agent", "tickets.file")
    inner = _effect(2, "Desk", "file_host",
                    {"seq": 1, "component": "Agent", "label": "tickets.file"})
    elsewhere = _effect(3, "Desk", "note",
                        {"seq": 40, "component": "Agent", "label": "x.y"})
    own, nested = _split_nested([outer, inner, elsewhere], [outer, inner, elsewhere])
    assert nested == [inner]
    assert own == [outer, elsewhere]


def test_a_steady_state_crash_counts_the_nested_emission_once(tmp_path):
    wal = _write(tmp_path / "s.wal", [
        HEADER,
        {"record": "activation-complete", "generation": 1, "components": []},
        _effect(1, "Agent", "tickets.file"),
        _effect(2, "Desk", "file_host",
                {"seq": 1, "component": "Agent", "label": "tickets.file"})])

    report = recover(str(wal))

    steady = report["steadyState"]
    assert [e["label"] for e in steady["crossed"]] == ["tickets.file"]
    assert [e["label"] for e in steady["nested"]] == ["file_host"]
    assert len(report["residue"]["outstanding"]) == 1
