"""Issue #1660: a hole whose crossings span two capabilities is split.

`emission[db, log] fn record(entry) = hole[Int]` puts three decisions behind
one obligation: what crosses through `db`, what crosses through `log`, and
what is returned. A single expression hole cannot even hold two `emit`
steps. The rule: a provide-method's WHOLE-body hole whose `crossing.calls`
span two or more capability tokens carries `split`, one `let
<token>_step = hole[...]` per token with only that token's calls, then a
result hole; `revl scaffold` writes the split directly. Never by operation,
never in a pure position.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.mcp import fillspec  # noqa: E402
from revl.scaffold import build_spec, scaffold_document  # noqa: E402

HEAD = """service Db { emission fn put(k: Str, v: Str) -> Int
             emission fn drop(k: Str) -> Int }
service Log { emission fn write(msg: Str) -> Int }
service Audit { emission[db, log] fn record(entry: Str) -> Int
                emission[db] fn store(entry: Str) -> Int
                fn peek(entry: Str) -> Int }
"""


def _program(record: str, store: str = 'hole[Int] "store it"',
             peek: str = 'hole[Int] "peek"') -> str:
    return HEAD + f"""component A requires db: Db, log: Log provides audit: Audit {{
  provide audit {{
    fn record(entry) {record}
    fn store(entry) = {store}
    fn peek(entry) = {peek}
  }}
}}
"""


WHOLE = _program('= hole[Int] "store the entry and log it"')


def _specs(source: str) -> dict:
    return {ob["message"]: ob["fillSpec"]
            for ob in fillspec.enrich(compile_source(source))}


def _bind(write: str, value: str) -> str:
    return re.sub(r"<[^>]*>", value, write)


def test_a_two_capability_whole_body_hole_is_split_per_token():
    split = _specs(WHOLE)["store the entry and log it"]["split"]
    assert [p["token"] for p in split] == ["db", "log", None]
    db, log, result = split
    assert db["write"].startswith('let db_step = hole[Int] "the crossing '
                                  'through db')
    assert [c["write"] for c in db["calls"]] == [
        "emit db.drop(<k: Str>)", "emit db.put(<k: Str>, <v: Str>)"]
    # one call through `log`: the step takes that call's return type
    assert log["write"].startswith("let log_step = hole[Int]")
    assert [c["write"] for c in log["calls"]] == ["emit log.write(<msg: Str>)"]
    assert result["write"] == ('return hole[Int] "the result of record, from '
                               'the steps above"')
    # negative controls, in the same program: two operations through ONE
    # boundary are one sentence (no split by operation), and a pure
    # position has nothing to cross
    specs = _specs(WHOLE)
    assert "split" not in specs["store it"]
    assert "split" not in specs["peek"]
    # and a hole that is not the whole body is not split
    body = _program('{\n      let n = 1\n      return hole[Int] "the rest"\n    }')
    assert "split" not in _specs(body)["the rest"]


def _split_source() -> str:
    """WHOLE with the record hole replaced by the split it carries."""
    split = _specs(WHOLE)["store the entry and log it"]["split"]
    body = "{\n" + "\n".join("      " + p["write"] for p in split) + "\n    }"
    return _program(body)


def test_each_split_hole_lists_only_its_tokens_calls():
    specs = _specs(_split_source())
    db = next(v for k, v in specs.items() if k.startswith("the crossing through db"))
    log = next(v for k, v in specs.items() if k.startswith("the crossing through log"))
    assert [c["capabilities"] for c in db["crossing"]["calls"]] == [["db"], ["db"]]
    assert [c["write"] for c in log["crossing"]["calls"]] == [
        "emit log.write(<msg: Str>)"]
    # and none of the parts is split again
    assert not [v for v in specs.values() if "split" in v]


def test_a_fill_of_all_parts_compiles():
    """Every part filled from its own spec: each step with its first listed
    crossing, the result from the steps. The program compiles hole-free for
    `record`, so the split is a decomposition the checker accepts."""
    source = _split_source()
    specs = _specs(source)
    for message, spec in specs.items():
        if not message.startswith("the crossing through"):
            continue
        call = spec["crossing"]["calls"][0]["write"]
        source = source.replace(f'hole[Int] "{message}"', _bind(call, "entry"))
    source = source.replace('hole[Int] "the result of record, from the steps above"',
                            "db_step + log_step")
    ir = compile_source(source)
    assert sorted(h["message"] for h in ir.get("holes") or []) == ["peek",
                                                                  "store it"]


def test_the_scaffold_writes_the_split_for_a_two_capability_method():
    spec = build_spec(service="Audit", requires=["db:Db", "log:Log"],
                      capabilities=["db", "log"],
                      emits=["record(entry: Str) -> Int"], effect=False)
    doc = scaffold_document(spec)
    assert "      let db_step = hole[Int]" in doc["source"]
    assert "      let log_step = hole[Int]" in doc["source"]
    assert ('      return hole[Int] "the result of record, from the steps '
            'above"') in doc["source"]
    assert doc["holeCount"] == 3
    # one capability: one sentence, one hole
    single = build_spec(service="Audit", requires=["db:Db"], capabilities=["db"],
                        emits=["record(entry: Str) -> Int"], effect=False)
    source = scaffold_document(single)["source"]
    assert "db_step" not in source
    assert "fn record(entry) = hole[Int]" in source
