"""Namespaced provision keys (roadmap §5, the deferred half; docs/namespacing.md).

Provision keys are a flat namespace: two authors both providing `db` collide
on key identity under G2, and a multi-author component registry (item 49) has
no way to qualify them. This suite pins the additive `ns::key` surface syntax
and its lowering:

* a namespaced key parses and lowers, carrying its namespace as part of the
  wiring identity (the IR `provides`/`requires`/`inject` key);
* two namespaces with the *same* local key coexist in one composition — G2
  does not fire — while two providers of the *same* qualified key still do;
* an unqualified key still parses, lowers and resolves exactly as before
  (default/empty namespace), so v1 programs are unaffected;
* the admission gate and `plan._interface_drift` treat qualified keys as the
  same opaque identity the linker does, so search-as-admission (item 49) works
  per namespace.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import RevlError, compile_source  # noqa: E402
from revl.plan import plan  # noqa: E402


STORE = "service Store { fn get(k: Str) -> Str }\n"


def _by_name(ir, name):
    return next(c for c in ir["components"] if c["name"] == name)


# ------------------------------------------------------ parse + lower surface

def test_namespaced_provide_parses_and_lowers():
    ir = compile_source(STORE + """
component AcmeDb provides acme::db: Store {
  provide acme::db { fn get(k) = k }
}
""", "t.rvl")
    # the qualified string is the wiring identity in both the component record
    # and the composition manifest
    assert _by_name(ir, "AcmeDb")["provides"] == {"acme::db": "Store"}
    entry = next(e for e in ir["manifest"]["components"] if e["name"] == "AcmeDb")
    assert entry["provides"] == ["acme::db"]


def test_namespaced_requirement_binds_its_local_segment():
    # the consumer requires `acme::db` but writes `db.get(...)` in its body:
    # the binding is the trailing segment, the wiring key is the full string.
    ir = compile_source(STORE + """
component AcmeDb provides acme::db: Store {
  provide acme::db { fn get(k) = k }
}
component App requires acme::db: Store {
  let x = effect db.get("a") undo db.get("a")
}
""", "t.rvl")
    app = _by_name(ir, "App")
    assert app["requires"] == {"acme::db": "Store"}     # qualified in the IR
    entry = next(e for e in ir["manifest"]["components"] if e["name"] == "App")
    assert entry["inject"] == ["acme::db"]
    # provider-first order: the namespaced requirement resolved to AcmeDb
    assert ir["manifest"]["loadOrder"].index("AcmeDb") < \
        ir["manifest"]["loadOrder"].index("App")


# --------------------------------------------------------- collision behaviour

def test_two_namespaces_share_a_local_key_without_colliding():
    ir = compile_source(STORE + """
component AcmeDb provides acme::db: Store { provide acme::db { fn get(k) = k } }
component BcorpDb provides bcorp::db: Store { provide bcorp::db { fn get(k) = k } }
""", "t.rvl")
    # both are composed; G2 sees two distinct keys, not a conflict
    keys = {k for c in ir["components"] for k in c["provides"]}
    assert keys == {"acme::db", "bcorp::db"}
    assert set(ir["manifest"]["loadOrder"]) == {"AcmeDb", "BcorpDb"}


def test_same_qualified_key_still_collides_under_g2():
    with pytest.raises(RevlError) as ex:
        compile_source(STORE + """
component A provides acme::db: Store { provide acme::db { fn get(k) = k } }
component B provides acme::db: Store { provide acme::db { fn get(k) = k } }
""", "t.rvl")
    assert "provision conflict" in str(ex.value)
    assert "acme::db" in str(ex.value)


# ------------------------------------------------------------- back-compat (v1)

def test_unqualified_key_still_resolves():
    ir = compile_source(STORE + """
component Db provides db: Store { provide db { fn get(k) = k } }
component App requires db: Store {
  let x = effect db.get("a") undo db.get("a")
}
""", "t.rvl")
    # identical shape to before namespacing: the key is its own binding
    assert _by_name(ir, "Db")["provides"] == {"db": "Store"}
    assert _by_name(ir, "App")["requires"] == {"db": "Store"}
    assert ir["manifest"]["loadOrder"] == ["Db", "App"]


def test_unqualified_duplicate_still_collides():
    with pytest.raises(RevlError):
        compile_source(STORE + """
component A provides db: Store { provide db { fn get(k) = k } }
component B provides db: Store { provide db { fn get(k) = k } }
""", "t.rvl")


@pytest.mark.parametrize("bad", [
    "provides acme:: : Store",   # empty local segment
    "provides ::db: Store",      # empty namespace
], ids=["empty-local", "empty-namespace"])
def test_malformed_namespace_is_rejected(bad):
    with pytest.raises(RevlError):
        compile_source(STORE + f"component X {bad} {{ }}", "t.rvl")


# ------------------------------- admission / drift honour the namespace (item 49)

# A running composition consuming `acme::db`. A `bcorp::db` provider is present
# too — the multi-author case the flat namespace could not express. The two
# authors publish *different* services, so a change to one author's interface
# cannot strand the other (admission compares services; namespacing keeps the
# keys, and hence the authors, disjoint).
NS_SERVICES = STORE + "service Kv { fn read(k: Str) -> Str }\n"
NS_RUNNING = NS_SERVICES + """
component AcmeDb provides acme::db: Store { provide acme::db { fn get(k) = k } }
component BcorpDb provides bcorp::db: Kv { provide bcorp::db { fn read(k) = k } }
component App requires acme::db: Store {
  let x = effect db.get("a") undo db.get("a")
}
"""

# A compatible-but-not-identical replacement of the acme provider: it adds a
# method to `Store`. Under the flat `!=` check this differed textually; under
# §5 it is admitted (a running consumer never calls the added method). `Kv` and
# `BcorpDb` are untouched.
ACME_EVOLVE = STORE.replace(
    "fn get(k: Str) -> Str",
    "fn get(k: Str) -> Str\n  fn ping() -> Str") + "service Kv { fn read(k: Str) -> Str }\n" + """
component AcmeDb provides acme::db: Store {
  provide acme::db { fn get(k) = k
                     fn ping() = "ok" }
}
"""


def test_compatible_namespaced_swap_is_admitted():
    running = compile_source(NS_RUNNING, "run.rvl")
    admitted = compile_source(ACME_EVOLVE, "cand.rvl", manifest=running)
    assert "ping" in admitted["services"]["Store"]["methods"]


def test_interface_drift_agrees_with_admission_on_a_namespaced_swap():
    """`plan._interface_drift` runs the gate's own §5 predicate, so a
    compatible-but-not-identical namespaced swap the gate admits is previewed
    with no drift — the preview matches the verdict (roadmap item 9, part 2)."""
    running = compile_source(NS_RUNNING, "run.rvl")
    result = plan(source=ACME_EVOLVE, manifest=running)
    assert result["admissible"] is True
    assert result["interfaceDrift"] == []
