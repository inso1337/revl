"""Runtime ceiling observations in the versioned audit interchange (#2794)."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.__main__ import main  # noqa: E402
from revl.interchange import validate  # noqa: E402


SOURCE = '''\
service Net { emission[net(requests=5, time="2s")] fn call(u: Str) -> Int }
service Svc { emission[net] fn run() -> Int }
component C requires net: Net provides svc: Svc {
  provide svc { fn run() -> Int { emit net.call("a"); return 0 } }
}
'''


def test_audit_json_carries_budget_outcome_and_wal_reference(tmp_path, capsys):
    source = tmp_path / "budgeted.rvl"
    source.write_text(SOURCE)
    wal = tmp_path / "run.wal"
    ceiling = {"net(calls=5,time=2000)":
               {"calls": 1, "bytes": 3, "elapsed": 7}}
    refused = {"net(calls=5,time=2000)":
               {"calls": 2, "bytes": 6, "elapsed": 9}}
    wal.write_text("\n".join((
        json.dumps({"record": "header", "walVersion": 1, "generation": 1}),
        json.dumps({"record": "emission", "seq": 1,
                    "component": "C", "label": "net.call"}),
        json.dumps({"record": "emission-complete", "seq": 1,
                    "ceiling": ceiling}),
        json.dumps({"record": "emission-complete", "seq": 2}),
        json.dumps({"record": "model-decision", "seq": 3,
                    "ceiling": {"ignored": {"calls": 99}}}),
        json.dumps({"record": "emission-refused", "seq": 4,
                    "ceiling": refused,
                    "error": {"message": "private host detail"}}),
        json.dumps({"record": "activation-complete"}),
        "",
    )))

    assert main(["audit", str(source), "--json", "--wal", str(wal)]) == 0
    doc = json.loads(capsys.readouterr().out)

    assert doc["boundary"]["C"]["capabilities"]["net.call"] == [
        "net(calls=5,time=2000)"]
    assert doc["runtime_ceilings"] == {
        "wal": str(wal),
        "activation_complete": True,
        "torn": False,
        "records": [
            {"record": "emission-complete", "seq": 1, "ceiling": ceiling},
            {"record": "emission-refused", "seq": 4, "ceiling": refused},
        ],
    }
    assert validate(doc) == []


def test_audit_wal_requires_plain_json_report(tmp_path, capsys):
    source = tmp_path / "budgeted.rvl"
    source.write_text(SOURCE)

    assert main(["audit", str(source), "--wal", str(tmp_path / "run.wal")]) == 2
    assert "--wal` requires the plain `revl audit --json` report" in \
        capsys.readouterr().err
