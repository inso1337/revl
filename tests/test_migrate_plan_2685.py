"""`revl migrate`: the read-only version-change plan (issue #2685, slice 1).

The command joins facts the tree already derives: the structural diff, the
derived semver bump, `admission._service_compatible` against the REAL consumers
in a composition, the declared state hand-off, and the authority delta. These
tests pin the definition of done from the issue:

  * a composition with two consumers of `cache` where only one is compatible
    with v2 lists the first movable and the second with the failing difference;
  * a v2 that adds a capability classifies `needs-approval`, never `safe`;
  * the command changes nothing: a hash of every input file is identical
    before and after, and no file is written.

They also pin the rest of the plan: the adapter and state-hand-off classes, the
old-version removability verdict, rollback feasibility, and the refusal to
invent a consumer shape from an audit/interchange document.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.migrate import (  # noqa: E402
    BLOCKED,
    MOVABLE,
    NEEDS_ADAPTER,
    NEEDS_APPROVAL,
    NEEDS_CANARY,
    NEEDS_HANDOFF,
    REFUSED,
    SAFE,
    plan,
)


# A provider whose `Cache` keeps `get` and `bump`. v2 removes `bump`.
OLD = """
service Cache {
  fn get(key: Str) -> Str
  fn bump(n: Int) -> Int
}
component CacheImpl provides cache: Cache {
  provide cache {
    fn get(key) = key
    fn bump(n) = n
  }
}
"""

NEW = """
service Cache {
  fn get(key: Str) -> Str
}
component CacheImpl provides cache: Cache {
  provide cache {
    fn get(key) = key
  }
}
"""

# One consumer pinned to the whole v1 shape (`get` + `bump`) and one pinned to
# the part v2 keeps. `bump` is the failing difference for the first.
COMPOSITION = """
service Cache {
  fn get(key: Str) -> Str
  fn bump(n: Int) -> Int
}
service NeedGet {
  fn get(key: Str) -> Str
}
service NeedBump {
  fn get(key: Str) -> Str
  fn bump(n: Int) -> Int
}
component CacheImpl provides cache: Cache {
  provide cache {
    fn get(key) = key
    fn bump(n) = n
  }
}
component A requires cache: NeedGet { }
component B requires cache: NeedBump { }
"""


def _plan(old_src: str, new_src: str, against_src: str = COMPOSITION) -> dict:
    return plan(compile_source(old_src), compile_source(new_src),
                compile_source(against_src))


# --------------------------------------------------- the issue's acceptance

def test_one_consumer_moves_and_the_other_reports_the_failing_difference():
    result = _plan(OLD, NEW)
    consumers = {entry["component"]: entry for entry in result["consumers"]}
    assert consumers["A"]["verdict"] == MOVABLE
    assert consumers["A"]["difference"] is None
    assert consumers["B"]["verdict"] == BLOCKED
    assert consumers["B"]["difference"]["method"] == "bump"
    assert consumers["B"]["difference"]["kind"] == "removed"
    assert "bump" in consumers["B"]["difference"]["reason"]
    assert result["classification"] == REFUSED
    assert result["oldRemovable"] is False
    # nothing moves when the plan refuses, so there is nothing to roll back
    assert result["rollback"]["possible"] is True
    assert "nothing is applied" in result["rollback"]["reason"]
    # the derived bump is still a measurement, independent of real consumers
    assert result["bump"]["bump"] == "major"


def test_added_capability_is_needs_approval_not_safe():
    old = """
service Cache {
  fn get(key: Str) -> Str
}
component CacheImpl provides cache: Cache {
  provide cache {
    fn get(key) = key
  }
}
"""
    new = """
service Cache {
  fn get(key: Str) -> Str
  emission[db] fn put(key: Str, value: Str)
}
component CacheImpl provides cache: Cache {
  provide cache {
    fn get(key) = key
    fn put(key, value) { }
  }
}
"""
    against = """
service Cache {
  fn get(key: Str) -> Str
}
component CacheImpl provides cache: Cache {
  provide cache {
    fn get(key) = key
  }
}
component App requires cache: Cache { }
"""
    result = _plan(old, new, against)
    assert result["classification"] == NEEDS_APPROVAL
    assert "capability:Cache.put" in result["authority"]["widened"]
    assert result["consumers"][0]["verdict"] == MOVABLE
    # a widened capability is never "safe", even though no consumer breaks
    assert result["oldRemovable"] is True


def test_command_is_read_only(tmp_path, capsys):
    old = tmp_path / "old.rvl"
    new = tmp_path / "new.rvl"
    against = tmp_path / "against.rvl"
    old.write_text(OLD)
    new.write_text(NEW)
    against.write_text(COMPOSITION)

    def tree() -> dict[str, str]:
        return {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(tmp_path.iterdir())
        }

    before = tree()
    assert main(["migrate", str(old), str(new), "--against", str(against)]) == 0
    human = capsys.readouterr().out
    assert "classification:" in human
    assert "old version removable: no" in human
    assert tree() == before  # every input byte, and no new file

    assert main(["migrate", str(old), str(new), "--against", str(against),
                 "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["command"] == "migrate"
    assert doc["classification"] == REFUSED
    assert tree() == before


def test_cli_json_plan_names_the_failing_consumer(tmp_path, capsys):
    old = tmp_path / "old.rvl"
    new = tmp_path / "new.rvl"
    against = tmp_path / "against.rvl"
    old.write_text(OLD)
    new.write_text(NEW)
    against.write_text(COMPOSITION)

    assert main(["migrate", str(old), str(new), "--against", str(against),
                 "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    by_name = {entry["component"]: entry for entry in doc["consumers"]}
    assert by_name["A"]["verdict"] == MOVABLE
    assert by_name["B"]["verdict"] == BLOCKED
    assert by_name["B"]["difference"]["method"] == "bump"
    assert doc["rollback"]["possible"] is True


# ------------------------------------------------------- the other classes

def test_all_consumers_movable_but_interface_changed_needs_canary():
    # the new version only adds a method: no consumer breaks, but the interface
    # moved, so the plan asks for a canary slice rather than claiming "safe".
    new = """
service Cache {
  fn get(key: Str) -> Str
  fn bump(n: Int) -> Int
  fn size() -> Int
}
component CacheImpl provides cache: Cache {
  provide cache {
    fn get(key) = key
    fn bump(n) = n
    fn size() = 0
  }
}
"""
    result = _plan(OLD, new)
    assert result["classification"] == NEEDS_CANARY
    assert all(entry["verdict"] == MOVABLE for entry in result["consumers"])
    assert result["oldRemovable"] is True


def test_identical_versions_are_safe_and_removable():
    result = _plan(OLD, OLD)
    assert result["changed"] is False
    assert result["classification"] == SAFE
    assert result["consumers"] == []
    assert result["oldRemovable"] is True
    assert result["rollback"]["possible"] is True


def test_a_consumer_reachable_only_through_an_adapter_needs_one():
    old = """
service Cache {
  fn touch() -> Str
}
component CacheImpl provides cache: Cache {
  provide cache {
    fn touch() = "ok"
  }
}
"""
    new = """
service Cache {
  fn touch(o: Opt[Str], xs: List[Int]) -> Str
}
component CacheImpl provides cache: Cache {
  provide cache {
    fn touch(o, xs) = "ok"
  }
}
"""
    against = """
service Cache {
  fn touch() -> Str
}
component CacheImpl provides cache: Cache {
  provide cache {
    fn touch() = "ok"
  }
}
component App requires cache: Cache { }
"""
    result = _plan(old, new, against)
    assert result["consumers"][0]["verdict"] == NEEDS_ADAPTER
    assert result["consumers"][0]["adapter"]["methods"] == ["touch"]
    assert result["classification"] == NEEDS_APPROVAL
    assert result["oldRemovable"] is False  # still pinned until the adapter lands


def _handoff(state_type: str, extra_method: str = "",
             extra_body: str = "") -> str:
    return f"""
service Store {{
  fn get(k: Str) -> Opt[Str]
  fn put(k: Str, v: Str)
{extra_method}
}}
component Cache provides cache: Store {{
  handoff cache: {state_type}
  let m = effect Map.new() undo m.drop()
  provide cache {{
    fn get(k) = m.get(k)
    fn put(k, v) {{ effect m.insert(k, v) undo m.remove(k) }}
{extra_body}
  }}
}}
"""


HANDOFF_OLD = _handoff("Map[Str, Str]")
HANDOFF_NEW = _handoff("Map[Str, Str]", "  fn size() -> Int", "    fn size() = 0")
HANDOFF_AGAINST = """
service Store {
  fn get(k: Str) -> Opt[Str]
  fn put(k: Str, v: Str)
}
component Cache provides cache: Store {
  handoff cache: Map[Str, Str]
  let m = effect Map.new() undo m.drop()
  provide cache {
    fn get(k) = m.get(k)
    fn put(k, v) { effect m.insert(k, v) undo m.remove(k) }
  }
}
component App requires cache: Store { }
"""


def test_dedicated_state_handoff_is_needs_handoff():
    result = _plan(HANDOFF_OLD, HANDOFF_NEW, HANDOFF_AGAINST)
    assert result["classification"] == NEEDS_HANDOFF
    assert result["handoff"][0]["verdict"] == "compatible"
    assert result["rollback"]["possible"] is False


def test_incompatible_state_handoff_is_refused():
    new = _handoff("List[Str]")
    result = _plan(HANDOFF_OLD, new, HANDOFF_AGAINST)
    assert result["handoff"][0]["verdict"] == "incompatible"
    assert result["classification"] == REFUSED
    assert result["oldRemovable"] is False


def test_successor_without_handoff_drops_state_and_needs_approval():
    new = _handoff("Map[Str, Str]").replace("  handoff cache: Map[Str, Str]\n", "")
    result = _plan(HANDOFF_OLD, new, HANDOFF_AGAINST)
    assert result["handoff"][0]["verdict"] == "state-dropped"
    assert result["classification"] == NEEDS_APPROVAL
    assert result["rollback"]["possible"] is False


def test_a_consumer_in_another_realm_is_not_checked_against_this_provider():
    """G2 is per (key, realm): the shared version change must not be tested
    against a tenant realm's separately provided `cache`."""
    realm_composition = """
service Cache {
  fn get(key: Str) -> Str
  fn bump(n: Int) -> Int
}
component CacheImpl provides cache: Cache {
  provide cache {
    fn get(key) = key
    fn bump(n) = n
  }
}
component TenantProvider provides cache: Cache {
  isolate cache in realm("tenant_a")
  provide cache {
    fn get(key) = key
    fn bump(n) = n
  }
}
component SharedApp requires cache: Cache { }
component TenantApp requires cache: Cache {
  isolate cache in realm("tenant_a")
}
"""
    result = _plan(OLD, NEW, realm_composition)
    assert [entry["component"] for entry in result["consumers"]] == ["SharedApp"]
    assert [entry["realm"] for entry in result["consumers"]] == [""]


def test_widened_capability_on_a_kept_method_is_needs_approval():
    # the consumer only calls `get`, so the widened `put` scope breaks no real
    # consumer; it is still an authority widening and must need approval.
    old = """
service Cache {
  fn get(key: Str) -> Str
  emission[db] fn put(key: Str, value: Str)
}
component CacheImpl provides cache: Cache {
  provide cache {
    fn get(key) = key
    fn put(key, value) { }
  }
}
"""
    new = old.replace("emission[db] fn put", "emission[db, bus] fn put")
    against = """
service Cache {
  fn get(key: Str) -> Str
  emission[db] fn put(key: Str, value: Str)
}
service Tiny {
  fn get(key: Str) -> Str
}
component CacheImpl provides cache: Cache {
  provide cache {
    fn get(key) = key
    fn put(key, value) { }
  }
}
component App requires cache: Tiny { }
"""
    result = _plan(old, new, against)
    assert result["consumers"][0]["verdict"] == MOVABLE
    assert "capability:Cache.put" in result["authority"]["widened"]
    assert result["classification"] == NEEDS_APPROVAL


def test_a_new_host_body_backend_is_needs_approval():
    # adding a host body is new reachable host code. `diff_backends` is its
    # authority bucket and must not let the plan read as safe.
    old = """
service Probe {
  fn ping() -> Str
}
extern pure fn host_probe() -> Str = @py { return "x" }
component Impl provides probe: Probe {
  provide probe {
    fn ping() = "x"
  }
}
"""
    new = old.replace(
        '= @py { return "x" }',
        '= @py { return "x" }\n  = @ts { return "x" }')
    result = _plan(old, new, old + "component App requires probe: Probe { }\n")
    assert "backend:host_probe:ts" in result["authority"]["widened"]
    assert result["classification"] == NEEDS_APPROVAL


def test_adapter_plus_a_compatible_handoff_is_needs_approval():
    # an authored adapter outranks a state hand-off in the documented order:
    # both are work, and approval is the stricter bucket.
    old = """
service Cache {
  fn touch() -> Str
}
component Cache provides cache: Cache {
  handoff cache: Map[Str, Str]
  let m = effect Map.new() undo m.drop()
  provide cache {
    fn touch() = "ok"
  }
}
"""
    new = """
service Cache {
  fn touch(o: Opt[Str], xs: List[Int]) -> Str
}
component Cache provides cache: Cache {
  handoff cache: Map[Str, Str]
  let m = effect Map.new() undo m.drop()
  provide cache {
    fn touch(o, xs) = "ok"
  }
}
"""
    result = _plan(old, new, old + "component App requires cache: Cache { }\n")
    assert result["consumers"][0]["verdict"] == NEEDS_ADAPTER
    assert result["handoff"][0]["verdict"] == "compatible"
    assert result["classification"] == NEEDS_APPROVAL


# --------------------------------------------------------- input guard rails

def test_against_must_carry_consumer_service_shapes(tmp_path, capsys):
    old = tmp_path / "old.rvl"
    new = tmp_path / "new.rvl"
    against = tmp_path / "against.json"
    old.write_text(OLD)
    new.write_text(NEW)
    # an audit/interchange document carries only bare key lists
    against.write_text(json.dumps({
        "boundary": {},
        "manifest": {"components": [{"name": "A", "inject": ["cache"],
                                     "provides": []}]},
    }))
    assert main(["migrate", str(old), str(new), "--against", str(against)]) == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "components" in err


def test_against_without_service_names_is_refused():
    against = {
        "components": [{"name": "A", "requires": {"cache": None},
                        "provides": {}, "isolate": {}}],
        "services": {},
    }
    try:
        plan(compile_source(OLD), compile_source(NEW), against)
    except ValueError as error:
        assert "does not name the service" in str(error)
    else:  # pragma: no cover - the guard is the point of the test
        raise AssertionError("a degraded --against document must be refused")


def test_against_with_a_key_list_instead_of_a_service_map_is_refused():
    against = {"components": [{"name": "A", "requires": ["cache"]}]}
    with pytest.raises(ValueError, match="does not name the services"):
        plan(compile_source(OLD), compile_source(NEW), against)


def test_against_compiled_with_the_new_version_is_refused():
    # if the against composition already runs NEW, every consumer would read
    # movable and the old version removable: refuse the wrong baseline.
    new_composition = NEW + "\ncomponent App requires cache: Cache { }\n"
    with pytest.raises(ValueError, match="does not run the old version"):
        _plan(OLD, NEW, new_composition)


def test_old_version_with_an_undeclared_service_is_refused():
    # a compiled document always declares what it provides; a hand-built or
    # truncated document that does not must be refused, not read as unchanged.
    bad_old = {
        "services": {},
        "components": [{"name": "Impl", "requires": {},
                        "provides": {"cache": "Cache"}, "isolate": {}}],
    }
    with pytest.raises(ValueError, match="does not declare service"):
        plan(bad_old, compile_source(NEW), compile_source(COMPOSITION))


def test_commands_reference_documents_the_command_and_classification_table():
    text = (ROOT / "docs" / "commands-reference.md").read_text(encoding="utf-8")
    assert "### `revl migrate`" in text
    for name in (REFUSED, NEEDS_APPROVAL, NEEDS_HANDOFF, NEEDS_CANARY, SAFE):
        assert f"`{name}`" in text
