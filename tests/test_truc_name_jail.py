"""truc's name jail — GHSA-4pxr-phgp-rfq8, the unit half.

A truc name is three things at once: a directory (`trucs/<name>`), a
`truc.toml` key, and a line of `truc`'s own report output. Every other check on
the add/rm path is content-based — index membership, source hash, the admission
gate, the planner's empty-guard — so `../../victim` was a perfectly well-formed
plan for a perfectly well-formed registry row: `add` vendored outside the
project, `rm` deleted a directory outside the project, and the bare-key TOML
write left `truc.toml` unparseable, so not even the remediating `rm` could read
the project back.

These tests pin the guard that runs before any path or key is derived from a
name. `test_truc_path_jail.py` drives the same guards through the real console
command; neither file needs the cordis runtime.
"""

import tomllib

import pytest

from revl import registry
from revl.truc import _host

#: Names that must never be admitted: they resolve outside `trucs/`, or name a
#: directory that is not one, or cannot be a TOML key without being quoted.
HOSTILE = [
    "", " ", "  \t ", ".", "..", ".../", "../../victim", "../victim",
    "a/b", "/abs/evil", "/etc/passwd", "trucs/../../etc", "a\\b", "..\\victim",
    "a\x00b", "\x00",
]

#: Names that must keep working — every one of them is a name the registry
#: itself admits, which is the point: `truc add` has to be able to install every
#: component `ship` can publish.
PLAIN = [
    "a", "pg_database", "user_cache", "readonly_database", "A-1", "0start",
    "a.b", "a_b-c.d", "a=b", 'a"b', "a'b", "a b", "a[b]", "a#b", "café",
]


def _names_matching_both_rules():
    """Every name either rule admits, over a sweep wide enough to carry the
    control characters, the quotes, and the astral planes where TOML and JSON
    disagree. Compact by design: the sweep is what makes the corpus honest, not
    its size."""
    candidates = list(PLAIN) + list(HOSTILE)
    for cp in list(range(0x00, 0x130)) + [0x2028, 0xFEFF, 0xFFFD, 0x10000,
                                          0x1F600, 0x10FFFF]:
        candidates.append("a" + chr(cp) + "b")
    return candidates


# --------------------------------------------------------- the rule itself

def test_the_rule_is_the_registrys_rule():
    """One grammar, not two. A truc name IS a component name — a truc is a
    component vendored into a project — so the check must be the registry's own
    rule rather than a stricter copy of it (which would refuse a component the
    registry legitimately published) or a looser one (which would let
    `trucs/<name>` mean something the registry would never have written)."""
    for name in _names_matching_both_rules():
        assert bool(_host._name_refusal(name)) == bool(
            registry._unsafe_name_reason(name)), name


@pytest.mark.parametrize("name", HOSTILE)
def test_a_name_that_resolves_out_of_trucs_is_refused(name):
    reason = _host._name_refusal(name)
    assert reason, f"{name!r} must not be admissible"
    assert "truc name" in reason


@pytest.mark.parametrize("name", PLAIN)
def test_a_component_name_is_admitted(name):
    assert _host._name_refusal(name) == ""
    assert _host._check_name(name) == name


@pytest.mark.parametrize("name", ["", None, 7, b"pg_database", ["a"], ".", "a/b"])
def test_a_name_that_is_not_a_usable_string_is_refused(name):
    assert _host._name_refusal(name)
    with pytest.raises(_host.TrucNameRefusal):
        _host._check_name(name)


def test_check_name_raises_the_reason_it_reports():
    """`_name_refusal` is the report and `_check_name` is the refusal, and a
    caller that reads the two must not get two different stories."""
    with pytest.raises(_host.TrucNameRefusal) as raised:
        _host._check_name("../../etc")
    assert str(raised.value) == _host._name_refusal("../../etc")


def test_the_registry_name_is_reported_as_a_registry_name():
    """A `[registries]` key is checked by the same rule, so the message has to
    say which of the two names it is talking about."""
    reason = _host._name_refusal("../../reg", "registry")
    assert "registry name" in reason
    assert "trucs/" not in reason


# ----------------------------------------------------- where the name lands

@pytest.mark.parametrize("name", PLAIN)
def test_vendor_dir_is_always_one_segment_inside_trucs(tmp_path, name):
    """The whole claim: whatever a name is, the directory it names is one level
    under the project's own `trucs/`."""
    vendor = _host._vendor_dir(str(tmp_path), name)
    assert vendor.parent == tmp_path / "trucs"
    assert vendor.relative_to(tmp_path / "trucs").parts == (name,)
    assert vendor.parts[-1] not in (".", "..")


@pytest.mark.parametrize("name", HOSTILE)
def test_vendor_dir_refuses_a_name_that_escapes(tmp_path, name):
    with pytest.raises(_host.TrucNameRefusal):
        _host._vendor_dir(str(tmp_path), name)
    assert not (tmp_path / "trucs").exists()


def test_vendor_dir_refuses_a_symlinked_trucs_root(tmp_path):
    """`mkdir(exist_ok=True)` and `write_text` follow a link, so a linked
    `trucs/` turns "vendor a truc" into a write outside the project — the same
    reason `_launcher._contained_path` tests every segment before compiling."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "trucs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(_host.TrucNameRefusal) as raised:
        _host._vendor_dir(str(tmp_path), "pg_database")
    assert "symlink" in str(raised.value) and "trucs" in str(raised.value)


def test_vendor_dir_refuses_a_symlinked_name(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "trucs").mkdir()
    (tmp_path / "trucs" / "pg_database").symlink_to(outside,
                                                    target_is_directory=True)
    with pytest.raises(_host.TrucNameRefusal) as raised:
        _host._vendor_dir(str(tmp_path), "pg_database")
    assert "pg_database" in str(raised.value)


def test_a_plain_name_is_still_spelled_the_way_the_docs_spell_it(tmp_path):
    """Bare when it can be: `pg_database = { registry = "local" }` is what
    docs/truc.md and every hand-written project already contain, and rewriting
    it quoted would churn every project for no gain."""
    for name in ("pg_database", "a", "A-1", "a_b-c", "0start"):
        assert _host._toml_key(name) == name


# ----------------------------------------------- the name as a TOML key

@pytest.mark.parametrize("name", PLAIN)
def test_every_admitted_name_is_a_parseable_toml_key(name):
    """The second half of the advisory. A name interpolated into a key position
    bare produces a `truc.toml` that no longer parses, which bricks the project:
    neither `assemble` nor even the remediating `rm` can read it back."""
    line = f'{_host._toml_key(name)} = {{ registry = "local" }}'
    parsed = tomllib.loads(f"[trucs]\n{line}\n")
    assert list(parsed["trucs"]) == [name]
    assert _host._toml_key_of(line) == name


def test_the_key_escaper_agrees_with_toml_including_the_astral_planes():
    """`json.dumps` is a near-miss quoter: JSON and TOML agree everywhere except
    the astral planes, where JSON writes a surrogate pair (`\\ud83d\\ude00`)
    and TOML forbids the escape outright. An emoji in a name is enough."""
    for cp in list(range(0x00, 0x200)) + [0x2028, 0xFEFF, 0xFFFD, 0x10000,
                                          0x1F600, 0x10FFFF]:
        name = "a" + chr(cp) + "b"
        if _host._name_refusal(name):
            continue
        assert tomllib.loads(
            f"[trucs]\n{_host._toml_key(name)} = 1\n")["trucs"] == {name: 1}


def test_the_registry_value_is_escaped_too():
    """The value is interpolated by the same writer, so it needs the same
    escaping — a registry name with a quote in it is as good at breaking the
    parse as a key is."""
    quoted = _host._toml_string('a"b\\c')
    assert tomllib.loads(
        "[trucs]\nx = { registry = " + quoted + " }"
    )["trucs"]["x"]["registry"] == 'a"b\\c'


def test_the_key_is_read_back_from_every_spelling():
    """`rm` has to find the key its `add` wrote, and a hand-written project is
    still a project someone has to be able to remove their way out of."""
    assert _host._toml_key_of('pg_database = { registry = "local" }') == \
        "pg_database"
    assert _host._toml_key_of('"a b" = { registry = "local" }') == "a b"
    assert _host._toml_key_of("'a b' = { registry = \"local\" }") == "a b"
    assert _host._toml_key_of('"a=b" = 1') == "a=b"
    assert _host._toml_key_of('"a\\"b" = 1') == 'a"b'
    assert _host._toml_key_of('"caf\\u00e9" = 1') == "café"


def test_a_line_that_is_not_an_assignment_reads_as_no_key():
    for line in ("[trucs]", "[assembly]", "", "# pg_database = 1",
                 '"unterminated = 1', '"a"', "[trucs] # comment"):
        assert _host._toml_key_of(line) == "", line


def test_a_hostile_toml_cannot_crash_the_reader():
    """`truc.toml` is project input, and the reader runs before any decision —
    a backslash escape it did not write, a truncated `\\u`, or a lone surrogate
    must read as text rather than raise."""
    for line in ('"a\\qb" = 1', '"a\\u12" = 1', '"a\\ud800" = 1', '"\\" = 1',
                 '"a" "b" = 1', "'a", '"a\\', '"\\U0001F600" = 1'):
        assert isinstance(_host._toml_key_of(line), str)
