"""`revl diff` — the semantic composition diff (roadmap item 123).

The PR-review tool for agent-generated compositions. Distinct from `revl audit
--diff` (the authority-drift *gate*, which only fails on a widening), `revl
diff` is a *descriptive* structural delta between two generations of a
composition: which components were added / removed / changed, which emissions
each gained or lost, which provide/require dependency edges were introduced or
broken. It reuses `audit_diff`'s crossing relation for the authority axis and
adds the membership (components) and wiring (provide/require) axes on top.

These tests pin the definition of done: a component gaining an emission, a
provider swap, an added requirement, and a removed component each surface
correctly; and two identical compositions diff to empty.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_source  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.composition_diff import diff, render  # noqa: E402


# a stable two-component base: PgCache provides `cache`, backed by `db`
BASE = """
service Database { emission fn execute(sql: Str) -> Int }
service Cache { emission[db] fn put(key: Str, value: Str) }

component PgCache requires db: Database provides cache: Cache {
  provide cache { fn put(key, value) { emit db.execute(`INSERT ${key}`) } }
}
component Front requires cache: Cache { }
"""


def _diff(before_src: str, after_src: str) -> dict:
    return diff(compile_source(before_src), compile_source(after_src))


# ---------------------------------------------------------------- membership

def test_identical_compositions_diff_to_empty():
    delta = _diff(BASE, BASE)
    assert delta["changed"] is False
    assert delta["components"] == {"added": [], "removed": [], "changed": []}
    assert delta["crossings"] == {"added": [], "removed": []}
    assert delta["guarantees"] == []


def test_a_removed_component_surfaces():
    after = """
    service Cache { emission[db] fn put(key: Str, value: Str) }
    service Database { emission fn execute(sql: Str) -> Int }
    component PgCache requires db: Database provides cache: Cache {
      provide cache { fn put(key, value) { emit db.execute(`INSERT ${key}`) } }
    }
    """  # Front is gone
    delta = _diff(BASE, after)
    assert delta["components"]["removed"] == ["Front"]
    assert "component `Front` removed" in delta["guarantees"]


def test_an_added_component_surfaces():
    after = BASE + """
    component Metrics requires cache: Cache { emit cache.put("hits", "1") }
    """
    delta = _diff(BASE, after)
    assert delta["components"]["added"] == ["Metrics"]
    assert "component `Metrics` added" in delta["guarantees"]


# ---------------------------------------------------------------- authority

def test_a_component_gaining_an_emission_surfaces():
    # Front goes from an empty body to emitting cache.put — a gained emission
    after = BASE.replace(
        "component Front requires cache: Cache { }",
        'component Front requires cache: Cache { emit cache.put("k", "v") }')
    delta = _diff(BASE, after)
    assert "Front" in delta["components"]["changed"]
    assert "emit:Front:cache.put" in delta["crossings"]["added"]
    # the scope of the emission ([db]) rides along in the guarantee
    assert any(g.startswith("component `Front` gained emission `cache.put`")
               for g in delta["guarantees"])


def test_a_lost_emission_surfaces():
    before = BASE.replace(
        "component Front requires cache: Cache { }",
        'component Front requires cache: Cache { emit cache.put("k", "v") }')
    delta = _diff(before, BASE)
    assert "emit:Front:cache.put" in delta["crossings"]["removed"]
    assert "component `Front` lost emission `cache.put`" in delta["guarantees"]


# ---------------------------------------------------------------- wiring

def test_a_provider_swap_surfaces():
    before = """
    service Database { fn execute(sql: Str) -> Int }
    component PgDatabase provides db: Database { provide db { fn execute(sql) = 1 } }
    component App requires db: Database { }
    """
    after = """
    service Database { fn execute(sql: Str) -> Int }
    component MysqlDatabase provides db: Database { provide db { fn execute(sql) = 2 } }
    component App requires db: Database { }
    """
    delta = _diff(before, after)
    swap = delta["providers"]["changed"]
    assert len(swap) == 1
    assert swap[0]["key"] == "db"
    assert swap[0]["from"] == "PgDatabase"
    assert swap[0]["to"] == "MysqlDatabase"
    assert ("provider of key `db` changed from `PgDatabase` to `MysqlDatabase`"
            in delta["guarantees"])


def test_an_added_requirement_surfaces():
    before = """
    service Session { fn token() -> Str }
    component SessionProvider provides session: Session {
      provide session { fn token() = "t" }
    }
    component Auth { }
    """
    after = """
    service Session { fn token() -> Str }
    component SessionProvider provides session: Session {
      provide session { fn token() = "t" }
    }
    component Auth requires session: Session { }
    """
    delta = _diff(before, after)
    assert {"component": "Auth", "key": "session"} in delta["requires"]["added"]
    assert "`Auth` now requires `session`" in delta["guarantees"]
    # the requirement resolves to a provider, so it is NOT a broken dependency
    assert delta["requires"]["broken"] == []


def test_a_broken_requirement_surfaces():
    # removing the provider leaves Auth's requirement dangling
    before = """
    service Session { fn token() -> Str }
    component SessionProvider provides session: Session {
      provide session { fn token() = "t" }
    }
    component Auth requires session: Session { }
    """
    after = """
    service Session { fn token() -> Str }
    component Auth requires session: Session { }
    """
    delta = _diff(before, after)
    assert {"component": "Auth", "key": "session"} in delta["requires"]["broken"]
    assert any("broken dependency" in g and "`Auth`" in g
               for g in delta["guarantees"])


# ---------------------------------------------------------------- rendering

def test_render_of_identical_is_a_clean_line():
    out = render(_diff(BASE, BASE), "before", "after")
    assert "no structural change" in out


def test_render_lists_components_and_guarantees():
    after = BASE.replace(
        "component Front requires cache: Cache { }",
        'component Front requires cache: Cache { emit cache.put("k", "v") }')
    out = render(_diff(BASE, after), "before", "after")
    assert "~ Front" in out
    assert "gained emission `cache.put`" in out


# ---------------------------------------------------------------- CLI

def _compile_to_json(tmp_path: Path, name: str, source: str) -> Path:
    src = tmp_path / f"{name}.rvl"
    src.write_text(source)
    ir_path = tmp_path / f"{name}.json"
    ir_path.write_text(json.dumps(compile_source(source)))
    return src, ir_path


def test_cli_diff_of_sources(tmp_path, capsys):
    before = tmp_path / "before.rvl"
    before.write_text(BASE)
    after = tmp_path / "after.rvl"
    after.write_text(BASE.replace(
        "component Front requires cache: Cache { }",
        'component Front requires cache: Cache { emit cache.put("k", "v") }'))
    assert main(["diff", str(before), str(after)]) == 0
    out = capsys.readouterr().out
    assert "gained emission `cache.put`" in out


def test_cli_diff_of_ir_documents_json(tmp_path, capsys):
    _, before = _compile_to_json(tmp_path, "before", BASE)
    _, after = _compile_to_json(
        tmp_path, "after",
        BASE.replace(
            "component Front requires cache: Cache { }",
            'component Front requires cache: Cache { emit cache.put("k", "v") }'))
    assert main(["diff", str(before), str(after), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert "emit:Front:cache.put" in report["crossings"]["added"]
    assert report["changed"] is True


def test_cli_identical_reports_no_change(tmp_path, capsys):
    before = tmp_path / "c.rvl"
    before.write_text(BASE)
    assert main(["diff", str(before), str(before)]) == 0
    assert "no structural change" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Realms (issue #1848): a provider is matched to the consumers in its realm.  #
# --------------------------------------------------------------------------- #

def _two_realms(*, with_b_provider: bool = True, b_offset: int = 2) -> str:
    """Realm `a`: DbA and one consumer. Realm `b`: DbB and three consumers.
    Both providers provide the same key, `db`."""
    provider_b = (f'component DbB provides db: Db {{\n'
                  f'  isolate db in realm("b")\n'
                  f'  provide db {{ fn get(k) = k + {b_offset} }}\n}}\n'
                  if with_b_provider else "")
    users_b = "".join(
        f"service U{i} {{ fn go(k: Int) -> Int }}\n"
        f"component UserB{i} requires db: Db provides u{i}: U{i} {{\n"
        f'  isolate db in realm("b")\n'
        f"  provide u{i} {{ fn go(k) = db.get(k) }}\n}}\n" for i in range(3))
    return ("service Db { fn get(k: Int) -> Int }\n"
            "service UA { fn go(k: Int) -> Int }\n"
            "component DbA provides db: Db {\n"
            '  isolate db in realm("a")\n'
            "  provide db { fn get(k) = k + 1 }\n}\n"
            "component UserA requires db: Db provides ua: UA {\n"
            '  isolate db in realm("a")\n'
            "  provide ua { fn go(k) = db.get(k) }\n}\n"
            + provider_b + users_b)


def test_removing_one_realms_provider_breaks_exactly_that_realms_consumers():
    """Realm `a` still provides `db`, so a realm-blind diff read realm `b`'s
    three consumers as satisfied and reported no break. They are broken, and
    they are exactly the cascade `revl_query` reports for withdrawing DbB."""
    from revl.query import withdrawal

    before = compile_source(_two_realms())
    after = compile_source(_two_realms(with_b_provider=False))
    delta = diff(before, after)
    broken = sorted(e["component"] for e in delta["requires"]["broken"])
    assert broken == ["UserB0", "UserB1", "UserB2"]
    assert all(e["realm"] == "b" for e in delta["requires"]["broken"])
    assert broken == sorted(c["component"] for c in withdrawal(before, "DbB")["cascade"])
    assert delta["providers"]["removed"] == [
        {"key": "db", "realm": "b", "service": "Db", "component": "DbB"}]
    assert "key `db` in realm `b` is no longer provided (was `Db`)" in delta["guarantees"]


def test_the_other_realms_provider_is_not_reported_changed():
    """Keyed by key alone, the second provider overwrote the first, so removing
    DbB read as `db` changing from DbB to DbA. Realm `a` did not change."""
    before = compile_source(_two_realms())
    after = compile_source(_two_realms(with_b_provider=False))
    assert diff(before, after)["providers"]["changed"] == []


def test_two_realms_with_no_change_diff_to_empty():
    ir = compile_source(_two_realms())
    delta = diff(ir, ir)
    assert delta["changed"] is False
    assert delta["requires"]["broken"] == []


def test_a_single_realm_delta_carries_no_realm_field():
    """The shared realm is the default: a single-realm composition's delta has
    the shape it always had, with no `realm` key on any record."""
    src = ("service Db { fn get(k: Int) -> Int }\n"
           "service U { fn go(k: Int) -> Int }\n"
           "component Db1 provides db: Db { provide db { fn get(k) = k } }\n"
           "component User requires db: Db provides u: U {\n"
           "  provide u { fn go(k) = db.get(k) } }\n")
    before = compile_source(src)
    after = compile_source(src.replace(
        "component Db1 provides db: Db { provide db { fn get(k) = k } }\n", ""))
    delta = diff(before, after)
    assert delta["requires"]["broken"] == [{"component": "User", "key": "db"}]
    assert delta["providers"]["removed"] == [
        {"key": "db", "service": "Db", "component": "Db1"}]
