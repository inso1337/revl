"""`value_float` (issue #3343, stdlib/value.rvl).

The Float accessor beside `value_str`/`value_int`/`value_bool`, on the tiers
that carry `value_int` (py, ts, rust). Total like the others: a float reads
back as itself, an integer as the Float of the same value, anything else as
`0.0`. The same revl test blocks run on every tier; each assertion pins an
exact Float, so a pass on two tiers is the same answer on both.

The integer case is the one an author gets wrong by hand on ts, where an Int
is a `bigint` and a Float a `number`: `json_parse("3")` and
`json_parse("3.0")` must read back equal.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.test import run_py, run_rust, run_ts  # noqa: E402

CONSUMER = """\
use "stdlib/json.rvl" { json_parse }
use "stdlib/value.rvl" { value_float, value_kind }

fn f(s: Str) -> Float { return value_float(json_parse(s)) }

test "value_float of a float reads back the float" {
  assert f("3.5") == 3.5
  assert f("-0.25") == -0.25
  assert f("1e3") == 1000.0
}

test "value_float of an integer is the Float of the same value" {
  assert value_kind(json_parse("3")) == "int"
  assert f("3") == 3.0
  assert f("-7") == -7.0
  assert f("0") == 0.0
}

test "value_float reads 3 and 3.0 back equal" {
  assert f("3") == f("3.0")
}

test "value_float of an integer past 2**53 rounds to the nearest Float" {
  assert f("9007199254740993") == 9007199254740992.0
}

test "value_float of a string is 0.0" {
  assert f("\\"3.5\\"") == 0.0
}

test "value_float of null is 0.0" {
  assert f("null") == 0.0
}

test "value_float of a record is 0.0" {
  assert f("{\\"x\\": 2.5}") == 0.0
}

test "value_float of a list and of a bool is 0.0" {
  assert f("[1.5]") == 0.0
  assert f("true") == 0.0
}
"""


@pytest.fixture(scope="module")
def consumer_ir(tmp_path_factory):
    d = tmp_path_factory.mktemp("value_float")
    (d / "stdlib").mkdir()
    for name in ("value.rvl", "json.rvl"):
        (d / "stdlib" / name).write_text(
            (ROOT / "stdlib" / name).read_text(encoding="utf-8"),
            encoding="utf-8")
    main = d / "main.rvl"
    main.write_text(CONSUMER, encoding="utf-8")
    return compile_files([str(main)])


def test_value_float_is_exported_on_every_value_int_tier():
    ir = compile_files([str(ROOT / "stdlib" / "value.rvl")])
    bodies = {e["name"]: set(e["bodies"]) for e in ir["externs"]}
    assert "value_float" in bodies
    assert bodies["value_float"] == bodies["value_int"]


@pytest.mark.parametrize("run", [run_py, run_ts, run_rust],
                         ids=["py", "ts", "rust"])
def test_value_float_on_tier(consumer_ir, run):
    verdict, detail = run(consumer_ir)
    if verdict == "skip":
        pytest.skip(str(detail))
    assert verdict == "pass", detail
