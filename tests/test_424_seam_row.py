"""The `seam` row: item 424 gap (b), slice B2.

`docs/design/424-dsh-language-gaps.md` §2 (D-424b.1 through D-424b.4, D-424b.8)
and §2.6, which names the exit test this file executes at the ADMISSION level:

    Placing a seam on `key("db")` leaves every consumer's source unchanged and
    every consumer resolves the seam (G2); the observer sees one record per call
    in order; ...; two stack layers seaming one edge refuse, naming both.

What B2 lands here is the SURFACE, the SYNTHESIS and the ADMISSION checks: a
seam is a composition row (D-424b.2); it synthesizes a forwarder that observes
then forwards, derived from the service declaration (D-424b.3); `decide` is
admitted only on a `Result` method and `rewrite` has no spelling (D-424b.4);
the observer never holds the inner handle (D-424b.8). "The observer sees one
record per call in order" is the RUNTIME half — this environment has no
cordis-py runtime (the same limit B1 measured), so it is pinned at the source
level (the generated forwarder body) rather than executed.

Two facts §2.2 measured against `origin/main` are carried here as tests rather
than left in the prose, because they bound what a forwarder can be:

  * the interposition is DISTINCT-KEY — the wrapped provider is placed under an
    inner key (`isolate` binds one realm per key, so the same-key shape is
    `run.py:747`'s hole); and
  * the forwarder reaches the inner and observer keys, so G4 refuses it unless
    the wrapped SERVICE declares a bound wide enough (D-424b.5 is the rule
    change that would move that check onto the seam's `through` set; it is filed
    for B3 and NOT made here). `test_a_narrow_bound_*` pins the current refusal.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl.composition import compile_composition, resolve_file  # noqa: E402
from revl.errors import RevlError  # noqa: E402
from revl.parser import Parser  # noqa: E402
from revl.synthesize import synthesize_provider  # noqa: E402

# A service whose `execute` declares a bound wide enough to cover the forwarder's
# crossing (the inner key `db__seamed` and the observer key `obs`). This is
# §2.4's fallback: until D-424b.5 moves the G4 check onto the seam's `through`
# set, the service author's declaration is what G4 reads.
DB = """
service Db { emission[wire, db__seamed, obs] fn execute(q: Str) -> Str }
service Obs { emission[log] fn saw(op: Str) }
extern emission fn wire(q: Str) -> Str = @py { return "row" }
extern emission fn log(s: Str) = @py { pass }
"""

# A service whose every method returns Result, so a `decide` seam admits.
GATE = """
type Decision = Allow | Deny(Str)
service Kv { emission[wire, kv__seamed, gate] fn get(k: Str) -> Result[Str, Str] }
service Gate { emission[log] fn allow(op: Str) -> Decision }
extern emission fn wire(k: Str) -> Result[Str, Str] = @py { return Ok("v") }
extern emission fn log(s: Str) -> Decision = @py { return Allow }
"""

# A service whose `execute` declares the NARROW bound the service author wrote —
# no inner/observer keys. This is what a real wrapped service looks like, and it
# is what D-424b.5 exists to unblock.
DB_NARROW = """
service Db { emission[wire] fn execute(q: Str) -> Str }
service Obs { emission[log] fn saw(op: Str) }
extern emission fn wire(q: Str) -> Str = @py { return "row" }
extern emission fn log(s: Str) = @py { pass }
"""

INNER_DB = """
component InnerDb provides db__seamed: Db {
  provide db__seamed { fn execute(q) = emit wire(q) }
}
"""

OBS = """
component ObsC provides obs: Obs {
  provide obs { fn saw(op) = emit log(op) }
}
"""

# A consumer written with no idea an observer stands in front of its provider.
# Not one word of this changes when a seam is placed, which is D-424b.1 holding
# for the CONSUMER (the wrapped provider IS re-keyed, §2.2's measured cost).
CONSUMER = """
service App { emission[wire, db] fn run() -> Str }
component AppC requires db: Db provides app: App {
  provide app { fn run() = emit db.execute("select 1") }
}
"""


def write(tmp_path: Path, **files: str) -> Path:
    for name, text in files.items():
        (tmp_path / f"{name}.rvl").write_text(text)
    return tmp_path


def resolve(tmp_path: Path, doc: str = "base"):
    return resolve_file(str(tmp_path / f"{doc}.rvl"), str(tmp_path))


# ------------------------------------------------------------------ the surface

def test_seam_row_parses_with_contextual_keywords_only():
    """`seam`, `on`, `observe`, `decide` and `through` head a clause only in
    this one position, so the lexer's KEYWORDS set is untouched and a program
    using any of them as an ordinary name still parses — the property the
    `remote` row and 426 S2 chose."""
    program = Parser("""
composition Shop {
  seam @audit on key("db", realm: "tenant_a") observe with @obs through audit, net.log
}
""", "t.rvl").parse()
    seam = program.compositions[0].seams[0]
    assert (seam.label, seam.key, seam.kind) == ("audit", "db", "observe")
    assert (seam.observer, seam.realm) == ("obs", "tenant_a")
    assert seam.through == ["audit", "net.log"]

    # The same words as ordinary names, in ordinary code.
    ordinary = Parser("""
service S {
  emission fn go(seam: Str, on: Str, observe: Str, through: Str) -> Str
}
""", "t.rvl").parse()
    assert list(ordinary.services[0].methods["go"].params)[0][0] == "seam"


def test_rewrite_has_no_spelling():
    """D-424b.4's load-bearing refusal: `rewrite` is 427 F2's approve-one-run-
    another shape, still unfixed, so a construct whose purpose is argument
    substitution has no spelling anywhere in the grammar."""
    with pytest.raises(RevlError) as excinfo:
        Parser("""
composition Shop {
  seam @r on key("db") rewrite with @obs
}
""", "t.rvl").parse()
    message = str(excinfo.value)
    assert "kind `rewrite`, which has no spelling" in message
    assert "427 F2" in message


def test_seam_kind_must_be_observe_or_decide():
    with pytest.raises(RevlError) as excinfo:
        Parser("""
composition Shop {
  seam @s on key("db") watch with @obs
}
""", "t.rvl").parse()
    assert "expected `observe` or `decide`" in str(excinfo.value)


def test_a_seam_row_shares_the_one_label_namespace(tmp_path):
    """A consumer cannot tell a seam row from a file row, so two rows that a
    consumer cannot tell apart must not share a name."""
    write(tmp_path, services=DB, inner=INNER_DB, obs=OBS, base="""
composition Shop {
  use "services.rvl"
  row @audit from "inner.rvl" provides db__seamed
  seam @audit on key("db") observe with @obs
}
""")
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    assert "duplicate row label `@audit`" in str(excinfo.value)


# ------------------------ exit test: the consumer is unchanged, resolves the seam

def test_observe_seam_compiles_and_the_consumer_resolves_it(tmp_path):
    """The forwarder is synthesized, the consumer's source is byte-identical to
    the un-seamed case, and the consumer resolves the SEAM (G2 gives one
    provider of `db`), which in turn resolves the inner provider."""
    write(tmp_path, services=DB, inner=INNER_DB, obs=OBS, consumer=CONSUMER,
          base="""
composition Shop {
  use "services.rvl"
  row @app from "consumer.rvl" provides app
  row @inner from "inner.rvl" provides db__seamed
  row @obs from "obs.rvl" provides obs
  seam @audit on key("db") observe with @obs
}
""")
    document = compile_composition(str(tmp_path / "base.rvl"), str(tmp_path))
    order = document["manifest"]["loadOrder"]
    # The forwarder is plugged; the consumer and inner provider are plugged; and
    # the consumer's provider is the seam, whose provider is the inner.
    assert {"AppC", "SeamAuditProvider", "InnerDb", "ObsC"} <= set(order)

    row = next(r for r in resolve(tmp_path).rows if r.label == "audit")
    assert row.claims == [("db", None)]
    assert row.requires == ["db__seamed", "obs"]
    assert row.seam == {
        "edge": 'key("db")', "kind": "observe", "observer": "obs",
        "innerKey": "db__seamed", "service": "Db", "observerService": "Obs",
    }


def test_the_synthesized_forwarder_is_ordinary_source_on_no_disk(tmp_path):
    """Like a remote row: the forwarder is handed to `compile_files` through the
    in-memory `sources` map, so `_link` runs G2/G3/G4 over it, and the
    provenance path is derived from origin and label alone so two machines
    produce byte-identical rows."""
    write(tmp_path, services=DB, inner=INNER_DB, obs=OBS, base="""
composition Shop {
  use "services.rvl"
  row @inner from "inner.rvl" provides db__seamed
  row @obs from "obs.rvl" provides obs
  seam @audit on key("db") observe with @obs
}
""")
    table = resolve(tmp_path)
    row = next(r for r in table.rows if r.label == "audit")
    assert row.source == ".revl/synthesized/_project/audit.seam.rvl"
    assert not (tmp_path / row.source).exists()
    assert row.source in table.paths()

    assert json.dumps(table.to_ir()) == json.dumps(
        resolve_file(str(tmp_path / "base.rvl"), str(tmp_path)).to_ir())

    text = table.sources[row.source]
    assert ("component SeamAuditProvider requires db__seamed: Db, obs: Obs "
            "provides db: Db" in text)
    # D-424b.3: the forwarder observes then forwards.
    assert 'emit obs.saw("execute")' in text
    assert "return emit db__seamed.execute(q)" in text
    assert "SYNTHESIZED FORWARDING PROVIDER" in text


# ---------------------------------------------------- the decide kind (D-424b.4)

def test_decide_seam_compiles_on_a_result_service(tmp_path):
    """A `decide` seam admits on a service whose every method returns `Result`.
    The observer NEVER holds the inner handle (D-424b.8): only the `Allow` arm
    names the forwarder's inner call, and a `Deny` becomes the method's `Err`."""
    write(tmp_path, services=GATE, inner="""
component InnerKv provides kv__seamed: Kv {
  provide kv__seamed { fn get(k) = emit wire(k) }
}
""", gate="""
component GateC provides gate: Gate {
  provide gate { fn allow(op) = emit log(op) }
}
""", base="""
composition Shop {
  use "services.rvl"
  row @inner from "inner.rvl" provides kv__seamed
  row @gate from "gate.rvl" provides gate
  seam @g on key("kv") decide with @gate
}
""")
    document = compile_composition(str(tmp_path / "base.rvl"), str(tmp_path))
    assert "SeamGProvider" in document["manifest"]["loadOrder"]

    text = resolve(tmp_path).sources[
        next(r for r in resolve(tmp_path).rows if r.label == "g").source]
    assert 'let _decision = emit gate.allow("get")' in text
    # The inner call appears ONLY in the Allow arm — a Deny mints nothing.
    assert "Allow => emit kv__seamed.get(k)" in text
    assert "Deny(_msg) => Err(_msg)" in text


def test_decide_on_a_non_result_service_is_refused_naming_the_method(tmp_path):
    """D-424b.4: a `decide` seam needs somewhere to put a `Deny`, so every
    method must return `Result[T, E]`. `Db.execute` returns `Str`. The observer
    is a valid `decide` observer (declares `allow`), so the refusal is the
    decidability one, not the observer-contract one."""
    write(tmp_path, services="""
type Decision = Allow | Deny(Str)
service Db { emission[wire, db__seamed, gate] fn execute(q: Str) -> Str }
service Gate { emission[log] fn allow(op: Str) -> Decision }
extern emission fn wire(q: Str) -> Str = @py { return "row" }
extern emission fn log(s: Str) -> Decision = @py { return Allow }
""", inner=INNER_DB, gate="""
component GateC provides gate: Gate {
  provide gate { fn allow(op) = emit log(op) }
}
""", base="""
composition Shop {
  use "services.rvl"
  row @inner from "inner.rvl" provides db__seamed
  row @gate from "gate.rvl" provides gate
  seam @g on key("db") decide with @gate
}
""")
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    message = str(excinfo.value)
    assert "`decide` seam `@g`" in message
    assert "`execute` returns `Str`" in message


# --------------------------------------------- the observer contract (D-424b.3)

def test_the_observer_service_must_declare_the_method(tmp_path):
    """An `observe` seam calls `saw`; a `decide` seam calls `allow`. An observer
    whose service declares neither is refused naming the method."""
    write(tmp_path, services=DB, inner=INNER_DB, bad="""
component BadObs provides obs: Obs {
  provide obs { fn saw(op) = emit log(op) }
}
""", base="""
composition Shop {
  use "services.rvl"
  row @inner from "inner.rvl" provides db__seamed
  row @obs from "bad.rvl" provides obs
  seam @g on key("db") decide with @obs
}
""")
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    message = str(excinfo.value)
    assert "must declare `fn allow(...)`" in message


def test_a_seam_naming_an_unknown_observer_row_is_refused(tmp_path):
    write(tmp_path, services=DB, inner=INNER_DB, base="""
composition Shop {
  use "services.rvl"
  row @inner from "inner.rvl" provides db__seamed
  seam @g on key("db") observe with @nowhere
}
""")
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    assert "with @nowhere" in str(excinfo.value)


# --------------------------------------- the wrapped provider must be re-keyed

def test_a_seam_with_no_inner_provider_is_refused(tmp_path):
    """The wrapped provider is placed under the DISTINCT inner key (`db__seamed`)
    in its own source, §2.2's measured cost. A seam with nothing under that key
    is refused naming it."""
    write(tmp_path, services=DB, obs=OBS, base="""
composition Shop {
  use "services.rvl"
  row @obs from "obs.rvl" provides obs
  seam @g on key("db") observe with @obs
}
""")
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    message = str(excinfo.value)
    assert "no row provides the inner key `db__seamed`" in message


# ------------------------------- D-424b.5 / §2.4: G4 bounds the forwarder today

def test_a_narrow_service_bound_refuses_the_forwarder(tmp_path):
    """The load-bearing measured fact (§2.2, §2.4). The forwarder reaches
    `db__seamed` and `obs`, so G4 refuses it against a service that declares
    `emission[wire]` only. Moving that check onto the seam's `through` set is the
    D-424b.5 rule change, filed for B3 and NOT made here; this pins the boundary
    so the day it lands, something says so."""
    write(tmp_path, services=DB_NARROW, inner=INNER_DB, obs=OBS, base="""
composition Shop {
  use "services.rvl"
  row @inner from "inner.rvl" provides db__seamed
  row @obs from "obs.rvl" provides obs
  seam @audit on key("db") observe with @obs through audit
}
""")
    # Resolution and synthesis SUCCEED — the seam is admissible as a row.
    table = resolve(tmp_path)
    assert any(r.label == "audit" for r in table.rows)
    # It is G4, at _link over the synthesized forwarder, that refuses it.
    with pytest.raises(RevlError) as excinfo:
        compile_composition(str(tmp_path / "base.rvl"), str(tmp_path))
    message = str(excinfo.value)
    assert "`Db.execute` is declared `emission[wire]`" in message
    assert "db__seamed" in message


# ------------------------------------------- two seams on one edge collide (G2)

def test_two_seams_on_one_edge_collide_naming_both_rows(tmp_path):
    """D-424b.1 addresses the EDGE, so two seams on `key("db")` both claim
    `(db, None)` and G2 refuses at the row level, naming both — the same rule
    that keeps two remote peers in one realm apart."""
    write(tmp_path, services=DB, inner=INNER_DB, obs=OBS, base="""
composition Shop {
  use "services.rvl"
  row @inner from "inner.rvl" provides db__seamed
  row @obs from "obs.rvl" provides obs
  seam @a on key("db") observe with @obs
  seam @b on key("db") observe with @obs
}
""")
    with pytest.raises(RevlError) as excinfo:
        resolve(tmp_path)
    message = str(excinfo.value)
    assert 'key("db") is claimed by both row `.::@a`' in message
    assert "`.::@b`" in message


def test_through_is_carried_into_the_row_ir(tmp_path):
    """B3's `through` bound has its SURFACE here: it parses and is carried into
    the row and the IR. The G4-against-`through` CHECK (D-424b.5) is B3's and is
    not wired, which the forwarder header states in the artifact itself."""
    write(tmp_path, services=DB, inner=INNER_DB, obs=OBS, base="""
composition Shop {
  use "services.rvl"
  row @inner from "inner.rvl" provides db__seamed
  row @obs from "obs.rvl" provides obs
  seam @audit on key("db") observe with @obs through audit, net.log
}
""")
    table = resolve(tmp_path)
    row = next(r for r in table.rows if r.label == "audit")
    assert row.seam["through"] == ["audit", "net.log"]
    assert "filed for B3" in table.sources[row.source]


# ------------------------------------------- the kind, called directly (§4)

def test_synthesize_provider_seam_kind_is_registered():
    """§4's claim is that the four constructs are four KINDS of one function.
    `seam` is now the second the module ships (after `remote`)."""
    from revl.synthesize import KINDS
    assert "seam" in KINDS

    program = Parser(DB, "t.rvl").parse()
    service = next(s for s in program.services if s.name == "Db")
    component, text = synthesize_provider(service, "seam", {
        "label": "audit", "key": "db", "realm": None,
        "inner_key": "db__seamed", "observer_key": "obs",
        "observer_service": "Obs", "kind": "observe", "through": (),
        "doc": "t.rvl", "line": 1,
    })
    assert component == "SeamAuditProvider"
    assert "provides db: Db" in text


def test_synthesize_provider_refuses_an_unknown_kind():
    """The function says which kinds it has rather than silently doing the wrong
    one; `configure` (§4's fourth kind) is designed but not yet built here."""
    program = Parser(DB, "t.rvl").parse()
    with pytest.raises(ValueError) as excinfo:
        synthesize_provider(program.services[0], "configure", {})
    message = str(excinfo.value)
    assert "'remote'" in message and "'seam'" in message
