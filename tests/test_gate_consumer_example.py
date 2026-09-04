"""The real external-consumer example — roadmap item 338, py slice exit test.

`examples/ecosystem-consumer/` is a small, standalone-shaped project (its own
`pyproject.toml` depending on `revl`, its own `ci_gate.py` importing ONLY
`revl.gate`) that demonstrates the item's exit: a third-party tool imports
the published gate and admits agent-authored candidates, gating its
"register" decision on `admitted` while logging `gate_version()` and
recording `frontier` with every verdict (docs/design/338-revl-as-dependency.md
§6, §8; docs/gate-dependency-contract.md).

Two things this file proves, matching the design's exit tests:

* **The consumer's own logic is correct** (`VerdictCache`/`cache_key`): a
  verdict cached under one `gate_version()` is invisible to a lookup under a
  DIFFERENT `gate_version()` — the versioning obligation
  (docs/design/338 §3, "Language skew") demonstrated, not just stated.
* **The example genuinely exercises the PACKAGED surface, not an in-tree
  import.** `ci_gate.py` is run as a subprocess against a freshly assembled,
  ISOLATED copy of the `revl` package (nothing of this checkout's `src/` is
  on its `PYTHONPATH`), which is what a `pip install revl` into a fresh
  environment would put on `sys.path`. `build`/`hatchling` are not available
  in this environment (see tests/test_packaging.py's guarded wheel-build
  test), so this reproduces the same fresh-install shape without them: a
  wheel is just its declared file set on `sys.path`, and copying that file
  set into an isolated directory with nothing else of the checkout visible
  is behaviorally identical to what an unpacked `pip install` produces for
  the modules this example actually touches (`revl.gate` and the pure-python
  modules it lazily imports — the packaged `backends/`/`stdlib/` trees are not
  needed here because the example never calls `compile_to` or `use`s
  anything).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = ROOT / "examples" / "ecosystem-consumer"
CANDIDATES_DIR = EXAMPLE_DIR / "candidates"


def _load_ci_gate():
    """Import `ci_gate.py` by path (it is not a package under `src/`, and
    must not be — it is a standalone consumer's own code). `tests/conftest.py`
    already puts this checkout's `src/` on `sys.path` for the whole session,
    which is what lets `ci_gate`'s own `from revl.gate import ...` resolve
    here; the subprocess tests below deliberately do NOT rely on that."""
    spec = importlib.util.spec_from_file_location(
        "ci_gate", EXAMPLE_DIR / "ci_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci_gate = _load_ci_gate()


# --------------------------------------------------------------------------- #
# The example's own logic: the verdict cache keyed on the full gate_version().
# --------------------------------------------------------------------------- #

def test_cache_key_is_the_full_triple():
    version = {"api": "1.0.0", "language": "2.0.0",
              "frontier": "reference-full:2.0.0"}
    assert ci_gate.cache_key(version) == ("1.0.0", "2.0.0",
                                          "reference-full:2.0.0")


def test_cache_hit_on_the_same_version():
    cache = ci_gate.VerdictCache()
    version = {"api": "1.0.0", "language": "2.0.0",
              "frontier": "reference-full:2.0.0"}
    cache.put(version, "double_tool.rvl", {"admitted": True, "code": None,
                                           "frontier": version["frontier"]})
    hit = cache.get(version, "double_tool.rvl")
    assert hit is not None
    assert hit["admitted"] is True


def test_language_skew_invalidates_the_cache():
    """docs/design/338-revl-as-dependency.md §3, "Language skew": a verdict
    cached under one `language` must not be served for a different one — the
    exact bug the design calls out ("a stale cached admission trusted across
    a language bump")."""
    cache = ci_gate.VerdictCache()
    old_version = {"api": "1.0.0", "language": "2.0.0",
                  "frontier": "reference-full:2.0.0"}
    cache.put(old_version, "double_tool.rvl",
             {"admitted": True, "code": None,
              "frontier": old_version["frontier"]})

    new_version = {"api": "1.0.0", "language": "2.1.0",
                  "frontier": "reference-full:2.1.0"}
    assert cache.get(new_version, "double_tool.rvl") is None, (
        "a cached verdict from language 2.0.0 must not be served under 2.1.0")


def test_frontier_skew_invalidates_the_cache_even_at_the_same_language():
    """The dangerous skew (docs/design/338 §3, "Frontier skew"): two gates
    can share `language` but cover different surfaces. A cache keyed on
    `language` alone would wrongly serve a py reference-full verdict to a
    caller reading a rust self-host-frontier gate's cache; keying on the full
    triple (this project's `cache_key`) prevents it structurally."""
    cache = ci_gate.VerdictCache()
    py_gate = {"api": "1.0.0", "language": "2.0.0",
              "frontier": "reference-full:2.0.0"}
    cache.put(py_gate, "double_tool.rvl",
             {"admitted": True, "code": None, "frontier": py_gate["frontier"]})

    native_gate = {"api": "1.0.0", "language": "2.0.0",
                  "frontier": "selfhost:2.0.0-corpus"}
    assert cache.get(native_gate, "double_tool.rvl") is None


def test_every_cached_record_carries_its_frontier():
    """docs/design/338 §2: `frontier` is a first-class field a consumer MUST
    record with any admission it caches. `admit_candidate` must put it on
    every record, admitted or refused."""
    cache = ci_gate.VerdictCache()
    version = {"api": "1.0.0", "language": "2.0.0",
              "frontier": "reference-full:2.0.0"}
    for name in ("double_tool.rvl", "leaky_tool.rvl", "draft_tool.rvl"):
        record = ci_gate.admit_candidate(CANDIDATES_DIR / name, cache, version)
        assert record["frontier"] == version["frontier"], name


def test_run_gate_never_registers_a_refusal():
    """The security contract, demonstrated: Clause 1 (a refusal is
    authoritative) means the "register" decision (this example's stand-in for
    "run/accept") must never fire for a refused candidate."""
    cache = ci_gate.VerdictCache()
    logged: list[str] = []
    results = ci_gate.run_gate(CANDIDATES_DIR, cache, log=logged.append)

    by_name = {r["name"]: r for r in results}
    assert by_name["double_tool.rvl"]["admitted"] is True
    assert by_name["leaky_tool.rvl"]["admitted"] is False
    assert by_name["draft_tool.rvl"]["admitted"] is False
    assert by_name["draft_tool.rvl"]["code"] == "T3"

    register_lines = [line for line in logged if line.startswith("REGISTER")]
    refuse_lines = [line for line in logged if line.startswith("REFUSE")]
    assert len(register_lines) == 1
    assert "double_tool.rvl" in register_lines[0]
    assert len(refuse_lines) == 2
    assert not any("leaky_tool.rvl" in line for line in register_lines)
    assert not any("draft_tool.rvl" in line for line in register_lines)


# --------------------------------------------------------------------------- #
# The genuinely-external proof: run against an ISOLATED, packaged-shaped copy.
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def fresh_revl_install(tmp_path_factory) -> Path:
    """Assemble an isolated copy of the `revl` package — nothing else of this
    checkout — so a subprocess pointed at it can only see what a fresh
    `pip install revl` would put on `sys.path`. Pure `shutil.copytree`, no
    wheel-build tooling required (none is installed in this environment)."""
    site = tmp_path_factory.mktemp("fresh-site")
    dest = site / "revl"
    shutil.copytree(
        ROOT / "src" / "revl", dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return site


def _run_ci_gate(fresh_site: Path, *extra_args: str) -> subprocess.CompletedProcess:
    import os
    env = dict(os.environ)
    # The one override that matters: PYTHONPATH points ONLY at the isolated
    # copy of `revl`, and this checkout's `src/` is deliberately absent, so
    # the subprocess can only resolve `revl.gate` from the packaged-shaped
    # copy — never from an in-tree import.
    env["PYTHONPATH"] = str(fresh_site)
    return subprocess.run(
        [sys.executable, str(EXAMPLE_DIR / "ci_gate.py"), str(CANDIDATES_DIR),
         *extra_args],
        capture_output=True, text=True, env=env, timeout=60)


def test_example_admits_against_an_isolated_packaged_copy(fresh_revl_install):
    """The item's own exit (design §8, bullet 1): a third-party project,
    depending on nothing but the packaged `revl.gate` surface, admits a batch
    of candidates in a fresh environment and gets the expected per-candidate
    verdicts — not from an in-tree import."""
    proc = _run_ci_gate(fresh_revl_install)
    assert proc.returncode == 0, proc.stderr

    assert "gate_version: api=1.0.0" in proc.stdout
    assert "frontier=reference-full:" in proc.stdout
    assert "REGISTER double_tool.rvl" in proc.stdout
    assert "REFUSE   leaky_tool.rvl" in proc.stdout
    assert "REFUSE   draft_tool.rvl  code=T3" in proc.stdout
    # the security contract, demonstrated: neither refusal is ever REGISTERed.
    assert "REGISTER leaky_tool.rvl" not in proc.stdout
    assert "REGISTER draft_tool.rvl" not in proc.stdout


def test_example_json_output_matches_cli_admission_semantics(fresh_revl_install):
    """The verdict this out-of-tree consumer computes from the packaged
    surface must be the SAME verdict `revl compile`/the CLI admission path
    would give (333's match discipline, from a genuinely external package).
    Cross-checked here against `revl.gate.admit` run in-process against the
    SAME candidate sources — the identity the whole example exists to prove."""
    proc = _run_ci_gate(fresh_revl_install, "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)

    assert set(payload["gate_version"]) == {"api", "language", "frontier"}

    from revl.gate import admit as inprocess_admit  # this checkout's copy,
    # used only as the reference oracle for this assertion, never by ci_gate
    # itself (which only ever sees the isolated copy above).

    by_name = {r["name"]: r for r in payload["results"]}
    for path in sorted(CANDIDATES_DIR.glob("*.rvl")):
        oracle = inprocess_admit(path.read_text(encoding="utf-8"))
        record = by_name[path.name]
        assert record["admitted"] == oracle.admitted, path.name
        assert record["code"] == oracle.code, path.name
        assert record["frontier"] == payload["gate_version"]["frontier"]
