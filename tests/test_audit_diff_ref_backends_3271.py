"""Issue #3271: `audit --diff` lists an extern's backends from its ref tier too.

The baseline entry (`revl audit --json`) unions the ref tier into `backends`;
the new side of `--diff` was built from `bodies` only, so a newly added ref tier
was not reported as `backends_added`. Both entries now share one helper.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.audit_diff import audit_report, evaluate  # noqa: E402

BEFORE = 'extern pure fn engine(x: Str) -> Str = @py { return x }\n'
AFTER = ('extern pure fn engine(x: Str) -> Str\n'
         '    = @py { return x }\n'
         '    = @ts ref do_engine from "host/engine.ts"\n')
REF_ONLY = ('extern pure fn engine(x: Str) -> Str\n'
            '    = @py ref do_engine from "host/engine.py"\n')


def _write(tmp_path, name, text):
    for rel in ("host/engine.py", "host/engine.ts"):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("def do_engine(x):\n    return x\n"
                     if rel.endswith(".py") else
                     "export function do_engine(x) { return x }\n",
                     encoding="utf-8")
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _ir(path):
    return compile_files([str(path)])


def _cli_audit(path, capsys):
    assert main(["audit", str(path), "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_entries_agree_key_for_key_on_a_ref_tier(tmp_path, capsys):
    src = _write(tmp_path, "after.rvl", AFTER)
    cli = _cli_audit(src, capsys)
    lib = audit_report(_ir(src))
    cli_ext = {e["name"]: e for e in cli["externs"]}["engine"]
    lib_ext = {e["name"]: e for e in lib["externs"]}["engine"]
    assert cli_ext["backends"] == lib_ext["backends"] == ["py", "ts"]


def test_added_ref_tier_is_reported_as_backend_added(tmp_path, capsys):
    before = _write(tmp_path, "before.rvl", BEFORE)
    prev = _cli_audit(before, capsys)
    after = _write(tmp_path, "after.rvl", AFTER)
    result = evaluate(prev, audit_report(_ir(after)))
    assert result["backends_added"] == ["backend:engine:ts"]
    assert result["widened"]


def test_ref_only_extern_backend_is_not_dropped(tmp_path, capsys):
    src = _write(tmp_path, "ref.rvl", REF_ONLY)
    lib = audit_report(_ir(src))
    assert lib["externs"][0]["backends"] == ["py"]
