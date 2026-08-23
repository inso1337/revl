"""Search-as-admission de-risk probe (roadmap item 49 precondition).

Item 49 is an *agent-first component registry*: an agent asks "find me a
provider I can drop into this running composition", and the registry answers
with the candidates that would actually link. The open question this probe
settles — cheaply, before any registry is built — is whether that search is
*already* expressible with zero new machinery, by running the §5
structural-compatibility gate (`docs/service-compat.md`,
`src/revl/admission.py`) as a *query predicate* over a candidate set.

The claim under test: a registry search is nothing more than
"which of these candidate providers would this composition admit?", i.e. the
existing admission gate run as a FILTER. To prove it we must call the REAL
predicate — not a reimplementation. We do that the way the runtime itself
does: `compile_source(candidate, manifest=running_ir, replacing=...)` runs
`refuse_admission` over `_admit_service_replacement` end to end. A candidate
that would break a running consumer raises the admission diagnostic; one that
links returns an IR. The filter is just: keep the ones that don't raise.

Corpus: `examples/user_cache.rvl`. Its `UserCache` component is a running
*consumer* of `Database` (it calls `db.query(...) -> List[Row]` and
`emit db.execute(...) -> Int`); `PgDatabase` is the provider we hot-swap out.
The candidate set is a spread of alternative `Database` providers — some
interface-compatible with UserCache's call sites, some not. The gate, run as
a filter, must partition them exactly along the §5 relation.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from revl import RevlError, compile_files, compile_source  # noqa: E402

EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "examples")

# The running composition whose consumer (`UserCache`) call sites are the
# "service requirement" a registry search is filtering against.
RUNNING_SOURCE = os.path.join(EXAMPLES, "user_cache.rvl")
REPLACING = ("PgDatabase",)   # the provider we hot-swap; UserCache stays running


# --- the candidate corpus: alternative `Database` providers -----------------
#
# Each is a self-contained provider component that re-declares `service
# Database` and offers `db: Database`. What varies is the interface shape,
# which is exactly what the §5 gate scores against UserCache's call sites.

_ADMISSIBLE = {
    "MysqlDatabase": """
        // identical interface, different provider body -> the plain hot-swap
        service Database {
          fn query(sql: Str) -> List[Row]
          emission fn execute(sql: Str) -> Int
        }
        component MysqlDatabase provides db: Database {
          config { url: Str = "mysql://", pool_size: Int = 4 }
          let pool = effect Pool.open(config.url, config.pool_size) undo pool.close()
          provide db {
            fn query(sql)   = pool.query(sql)
            fn execute(sql) = pool.execute(sql)
          }
        }
    """,
    "AuditedDatabase": """
        // ADDS a method -> compatible evolution: a consumer type-checked
        // against the old interface still type-checks against a superset.
        service Database {
          fn query(sql: Str) -> List[Row]
          emission fn execute(sql: Str) -> Int
          fn ping() -> Bool
        }
        component AuditedDatabase provides db: Database {
          config { url: Str = "pg://", pool_size: Int = 4 }
          let pool = effect Pool.open(config.url, config.pool_size) undo pool.close()
          provide db {
            fn query(sql)   = pool.query(sql)
            fn execute(sql) = pool.execute(sql)
            fn ping()       = true
          }
        }
    """,
    "PurePutDatabase": """
        // DROPS the emission on `execute` -> tightening purity is safe for a
        // consumer (its call site was already marked); the gate admits it.
        service Database {
          fn query(sql: Str) -> List[Row]
          fn execute(sql: Str) -> Int
        }
        component PurePutDatabase provides db: Database {
          config { url: Str = "x://", pool_size: Int = 4 }
          let pool = effect Pool.open(config.url, config.pool_size) undo pool.close()
          provide db {
            fn query(sql)   = pool.query(sql)
            fn execute(sql) = 0
          }
        }
    """,
}

_INCOMPATIBLE = {
    "ReadOnlyDatabase": """
        // REMOVES `execute` -> UserCache still emits `db.execute(...)`; its
        // call site would dangle. The gate refuses.
        service Database {
          fn query(sql: Str) -> List[Row]
        }
        component ReadOnlyDatabase provides db: Database {
          config { url: Str = "ro://", pool_size: Int = 4 }
          let pool = effect Pool.open(config.url, config.pool_size) undo pool.close()
          provide db { fn query(sql) = pool.query(sql) }
        }
    """,
    "EmittingQueryDatabase": """
        // INTRODUCES an emission on `query` -> UserCache calls `db.query(...)`
        // from a pure position; an unmarked call site would silently cross the
        // effect boundary (G4/G8). The gate refuses.
        service Database {
          emission fn query(sql: Str) -> List[Row]
          emission fn execute(sql: Str) -> Int
        }
        component EmittingQueryDatabase provides db: Database {
          config { url: Str = "e://", pool_size: Int = 4 }
          let pool = effect Pool.open(config.url, config.pool_size) undo pool.close()
          provide db {
            fn query(sql)   { emit pool.query(sql) }
            fn execute(sql) = pool.execute(sql)
          }
        }
    """,
}


def _running_ir() -> dict:
    return compile_files([RUNNING_SOURCE])


def admits(running_ir: dict, candidate_src: str) -> tuple[bool, str]:
    """The registry predicate: would `running_ir` admit this candidate?

    This is the ONLY logic the probe adds on top of the runtime — and it adds
    no *compatibility* logic at all. It calls the real admission gate
    (`compile_source` with a running manifest -> `refuse_admission` ->
    `_admit_service_replacement` -> `_service_compatible`) and reports whether
    it raised the admission diagnostic. That is the whole "search" primitive.
    """
    try:
        compile_source(candidate_src, "<candidate>.rvl",
                       manifest=running_ir, replacing=REPLACING)
        return True, ""
    except RevlError as exc:
        return False, str(exc)


def search(running_ir: dict, candidates: dict[str, str]) -> dict[str, bool]:
    """Filter a candidate set to those the composition admits — a registry
    query expressed purely as the admission gate run over a candidate set."""
    return {name: admits(running_ir, src)[0] for name, src in candidates.items()}


# --- the probe, as an asserting test ----------------------------------------

def test_admission_gate_filters_the_candidate_corpus():
    running = _running_ir()
    assert "Database" in (running.get("services") or {})

    verdict = search(running, {**_ADMISSIBLE, **_INCOMPATIBLE})

    # the gate keeps exactly the interface-compatible providers ...
    for name in _ADMISSIBLE:
        assert verdict[name] is True, f"{name} should be admissible"
    # ... and drops the ones that would break UserCache's call sites.
    for name in _INCOMPATIBLE:
        assert verdict[name] is False, f"{name} should be refused"


def test_refusals_name_the_broken_consumer_and_reason():
    """A registry does not just filter; it must be able to tell an agent *why*
    a candidate was rejected. The same call already carries that — the gate's
    drift diagnostic names the running consumer and the offending method."""
    running = _running_ir()

    ok, why = admits(running, _INCOMPATIBLE["ReadOnlyDatabase"])
    assert ok is False
    assert "UserCache" in why and "execute" in why

    ok, why = admits(running, _INCOMPATIBLE["EmittingQueryDatabase"])
    assert ok is False
    assert "UserCache" in why and "query" in why


# --- runnable as a script: prints the filter result over examples/ ----------

def _print_probe() -> None:
    running = _running_ir()
    print("search-as-admission probe over examples/user_cache.rvl")
    print("requirement: UserCache consumes `Database`"
          " (db.query -> List[Row], emit db.execute -> Int)")
    print(f"hot-swapping out provider: {', '.join(REPLACING)}\n")
    print("candidate providers, filtered by the REAL §5 admission gate:")
    for name, src in {**_ADMISSIBLE, **_INCOMPATIBLE}.items():
        ok, why = admits(running, src)
        if ok:
            print(f"  ADMIT   {name}")
        else:
            reason = why.split("breaks", 1)[-1].strip()
            print(f"  REFUSE  {name}  <- {reason[:88]}")
    kept = [n for n, ok in search(running, {**_ADMISSIBLE, **_INCOMPATIBLE}).items() if ok]
    print(f"\nregistry search result (admissible providers): {kept}")


if __name__ == "__main__":
    _print_probe()
