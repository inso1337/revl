"""truc slice S4 — `truc ship <component>` publishes into a registry.

These drive the real `truc` console command through the backend's own venv
(the one with cordis-py installed) and assert the two policies the human
settled are enforced from truc's own CLI:

  1. **registry-declared evidence policy** — a registry declares its ship
     policy registry-side (`policy.json`: "gauntlet" | "audit" | "none"). The
     *official* stance ("gauntlet") requires admission AND gauntlet evidence
     (item 31) and stamps the dossier into the entry; an *additional* registry
     ("audit") requires only that the component admits. Shipping the same
     well-described component to the official registry with no evidence is
     REFUSED; to an audit-only registry it publishes.
  2. **discoverability** — ship REFUSES an under-described component (missing
     description / tags), naming what is missing; the published index row
     carries description + tags + the provide/require/capability surfaces so
     `revl_resolve` / registry search can find it.

Refusals leave the registry untouched (the empty-guard). Without the runtime
these skip (never reported as passing). Set up with `sh backends/python/setup.sh`.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORDIS_PY = ROOT / "backends" / "python" / ".venv" / "bin" / "python"

pytestmark = pytest.mark.skipif(
    not CORDIS_PY.exists(),
    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")

# a self-contained component: no requires, so it boots cold and the gauntlet
# lifecycle battery reports `passed` — shippable to the official registry.
GREETER = """\
service Greet {
  fn hello(name: Str) -> Str
}
component Greeter provides greet: Greet {
  provide greet {
    fn hello(name) = "hello, ".concat(name)
  }
}
"""


def _truc(project: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([str(CORDIS_PY), "-m", "revl.truc", *args],
                          cwd=str(project), env=env,
                          capture_output=True, text=True, timeout=300)


def _empty_registry(base: Path, name: str, policy: str | None) -> Path:
    reg = base / name
    (reg / "components").mkdir(parents=True)
    (reg / "index.json").write_text(
        json.dumps({"indexVersion": "0", "components": {}}))
    if policy is not None:
        (reg / "policy.json").write_text(json.dumps({"ship": policy}))
    return reg


def _gauntlet_dossier(source: str) -> str:
    """The author's prior proving-ground run (item 31), as they would have
    produced it with `revl_gauntlet` before shipping. Run in-process here —
    the test is not inside a truc Session, so there is no nested event loop."""
    src = str(ROOT / "src")
    code = (
        "import json,sys;"
        "from revl.mcp.session import Session;"
        "from revl.mcp import gauntlet;"
        "print(json.dumps(gauntlet.run(Session(), {'source': sys.stdin.read()})))"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.run([str(CORDIS_PY), "-c", code], input=source, env=env,
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _project(tmp_path: Path, *, registry: Path, reg_name: str, source: str,
             description: str | None, tags: list[str] | None,
             dossier: str | None, version: str | None = None,
             version_scheme: str | None = None,
             publisher: str | None = None) -> Path:
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True, exist_ok=True)
    (proj / "src" / "comp.rvl").write_text(source)
    toml = [
        "[assembly]", 'name = "app"', 'entry = ["src/comp.rvl"]', "",
        "[registries]", f'{reg_name} = {{ path = "{registry}" }}', "",
        "[ship]", f'registry = "{reg_name}"',
    ]
    if description is not None:
        toml.append(f'description = "{description}"')
    if tags is not None:
        toml.append("tags = [" + ", ".join(f'"{t}"' for t in tags) + "]")
    if version is not None:
        toml.append(f'version = "{version}"')
    if version_scheme is not None:
        toml.append(f'version_scheme = "{version_scheme}"')
    if publisher is not None:
        toml.append(f'publisher = "{publisher}"')
    if dossier is not None:
        (proj / "dossier.json").write_text(dossier)
        toml.append('evidence = "dossier.json"')
    (proj / "truc.toml").write_text("\n".join(toml) + "\n")
    return proj


def _names(registry: Path) -> set[str]:
    return {p.name for p in (registry / "components").iterdir() if p.is_dir()}


# ---------------------------------------------------------------- success (official)

def test_ship_official_with_evidence_publishes_a_discoverable_row(tmp_path):
    reg = _empty_registry(tmp_path, "official", "gauntlet")
    dossier = _gauntlet_dossier(GREETER)
    proj = _project(tmp_path, registry=reg, reg_name="official", source=GREETER,
                    description="A greeting service: name in, hello out.",
                    tags=["greeting", "hello"], dossier=dossier)

    r = _truc(proj, "ship", "greeter")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "published" in r.stdout and "greeter" in r.stdout

    # the triple is written; gauntlet evidence is stamped (official policy).
    entry = reg / "components" / "greeter"
    assert (entry / "component.rvl").read_text() == GREETER
    assert (entry / "manifest.json").exists()
    assert (entry / "dossier.json").exists()

    # the published row is discoverable: description + tags + the surfaces.
    row = json.loads((reg / "index.json").read_text())["components"]["greeter"]
    assert row["description"].startswith("A greeting service")
    assert set(row["tags"]) == {"greeting", "hello"}
    assert row["provides"] == {"greet": "Greet"}
    assert "requires" in row and "capabilities" in row and "emissions" in row

    # the WHOLE row is byte-reproducible by build_index, description and tags
    # included: ship records them in `components/greeter/meta.json` beside the
    # source instead of patching them into the generated index, so the registry
    # a publish leaves behind passes `verify` rather than reading stale forever.
    assert json.loads((entry / "meta.json").read_text()) == {
        "description": row["description"], "tags": row["tags"]}
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    fresh = subprocess.run(
        [str(CORDIS_PY), "-c",
         "import json,sys;from revl.registry import build_index,verify;"
         "print(json.dumps({'row': build_index(sys.argv[1], write=False)"
         "['components']['greeter'], 'problems': verify(sys.argv[1])}))",
         str(reg)],
        env=env, capture_output=True, text=True, timeout=300)
    assert fresh.returncode == 0, fresh.stderr
    regenerated = json.loads(fresh.stdout)
    assert row == regenerated["row"]
    assert regenerated["problems"] == []


# ---------------------------------------------------------------- refusal (a): metadata

def test_ship_refuses_an_under_described_component(tmp_path):
    reg = _empty_registry(tmp_path, "official", "gauntlet")
    dossier = _gauntlet_dossier(GREETER)
    proj = _project(tmp_path, registry=reg, reg_name="official", source=GREETER,
                    description=None, tags=None, dossier=dossier)

    r = _truc(proj, "ship", "greeter")
    assert r.returncode == 1, r.stdout
    assert "under-described" in r.stdout
    assert "description" in r.stdout and "tags" in r.stdout
    # registry untouched on refusal
    assert "greeter" not in _names(reg)
    assert json.loads((reg / "index.json").read_text())["components"] == {}


def test_ship_refuses_when_only_tags_are_missing(tmp_path):
    reg = _empty_registry(tmp_path, "community", "audit")
    proj = _project(tmp_path, registry=reg, reg_name="community", source=GREETER,
                    description="Has a description but no tags.", tags=None,
                    dossier=None)
    r = _truc(proj, "ship", "greeter")
    assert r.returncode == 1, r.stdout
    assert "missing tags" in r.stdout
    assert "greeter" not in _names(reg)


# ---------------------------------------------------------------- refusal (b): evidence

def test_ship_official_without_evidence_is_refused_registry_untouched(tmp_path):
    reg = _empty_registry(tmp_path, "official", "gauntlet")
    proj = _project(tmp_path, registry=reg, reg_name="official", source=GREETER,
                    description="Well described.", tags=["a", "b"], dossier=None)

    r = _truc(proj, "ship", "greeter")
    assert r.returncode == 1, r.stdout
    assert "gauntlet evidence" in r.stdout and "none was supplied" in r.stdout
    assert "greeter" not in _names(reg)
    assert json.loads((reg / "index.json").read_text())["components"] == {}


def test_ship_official_with_stale_evidence_is_refused(tmp_path):
    """Evidence is ship-verified: a dossier computed for a DIFFERENT source does
    not clear the current source (the regenerate-or-red discipline on evidence).
    Here the supplied dossier is for a broken source (verdict `rejected`)."""
    reg = _empty_registry(tmp_path, "official", "gauntlet")
    stale = _gauntlet_dossier("component Broken { this is not revl }")
    proj = _project(tmp_path, registry=reg, reg_name="official", source=GREETER,
                    description="Well described.", tags=["a", "b"], dossier=stale)

    r = _truc(proj, "ship", "greeter")
    assert r.returncode == 1, r.stdout
    assert "does not pass" in r.stdout
    assert "greeter" not in _names(reg)


# ---------------------------------------------------------------- policy axis

def test_same_component_ships_to_an_audit_registry_without_evidence(tmp_path):
    """The policy axis: the component the official registry refused for lack of
    evidence publishes fine to an audit-only registry — and without a dossier
    (that registry does not require one)."""
    reg = _empty_registry(tmp_path, "community", "audit")
    proj = _project(tmp_path, registry=reg, reg_name="community", source=GREETER,
                    description="Well described.", tags=["a", "b"], dossier=None)

    r = _truc(proj, "ship", "greeter")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "published" in r.stdout
    entry = reg / "components" / "greeter"
    assert (entry / "component.rvl").exists()
    assert not (entry / "dossier.json").exists()   # audit policy stamps none
    row = json.loads((reg / "index.json").read_text())["components"]["greeter"]
    assert row["description"] and set(row["tags"]) == {"a", "b"}


def test_ship_default_policy_is_audit_when_registry_declares_none(tmp_path):
    """A registry with no policy.json defaults to `audit` — a component must at
    least compile clean, but no gauntlet evidence is required."""
    reg = _empty_registry(tmp_path, "plain", None)   # no policy.json
    proj = _project(tmp_path, registry=reg, reg_name="plain", source=GREETER,
                    description="Well described.", tags=["a"], dossier=None)
    r = _truc(proj, "ship", "greeter")
    assert r.returncode == 0, r.stderr + r.stdout
    assert not (reg / "components" / "greeter" / "dossier.json").exists()


# ------------------------------------------------- first-come and the update flow
# A free name is still claimed first-come. A name already published may now be
# REPUBLISHED as a new release (roadmap item 49 phase 2), and every rule that
# makes that safe is the registry's — `tests/test_registry_releases.py` is the
# gate on those; these prove `truc ship` enforces exactly them, end to end.

GREETER_PLUS = """\
service Greet {
  fn hello(name: Str) -> Str
  fn bye(name: Str) -> Str
}
component Greeter provides greet: Greet {
  provide greet {
    fn hello(name) = "hello, ".concat(name)
    fn bye(name) = "bye, ".concat(name)
  }
}
"""


def test_ship_refuses_an_update_to_a_name_published_without_a_version(tmp_path):
    """Fail closed. Nothing recorded which release the published bytes are, so
    there is no bump to compute and nothing a new version could be checked
    against — this is also what protects every entry published before phase 2."""
    reg = _empty_registry(tmp_path, "community", "audit")
    proj = _project(tmp_path, registry=reg, reg_name="community", source=GREETER,
                    description="First one.", tags=["a"], dossier=None)
    first = _truc(proj, "ship", "greeter")
    assert first.returncode == 0, first.stdout
    assert "UNVERSIONED" in first.stdout

    # a second, different project trying to take the name over is refused.
    other = GREETER.replace("hello, ", "hi, ")
    proj2 = _project(tmp_path / "second", registry=reg, reg_name="community",
                     source=other, description="Second one.", tags=["b"],
                     dossier=None, version="2.0.0")
    r = _truc(proj2, "ship", "greeter")
    assert r.returncode == 1, r.stdout
    assert "declares no version" in r.stdout
    # the original entry is unchanged (still the first source).
    assert (reg / "components" / "greeter" / "component.rvl").read_text() == GREETER


def test_ship_refuses_an_update_that_does_not_declare_its_release(tmp_path):
    reg = _empty_registry(tmp_path, "community", "audit")
    proj = _project(tmp_path, registry=reg, reg_name="community", source=GREETER,
                    description="First one.", tags=["a"], dossier=None,
                    version="1.0.0")
    assert _truc(proj, "ship", "greeter").returncode == 0

    proj2 = _project(tmp_path / "second", registry=reg, reg_name="community",
                     source=GREETER_PLUS, description="Next.", tags=["a"],
                     dossier=None)
    r = _truc(proj2, "ship", "greeter")
    assert r.returncode == 1, r.stdout
    assert "an update must declare which release it is" in r.stdout
    assert (reg / "components" / "greeter" / "component.rvl").read_text() == GREETER


def test_ship_publishes_a_new_release_and_attaches_its_changelog(tmp_path):
    """The item 49 phase 2 loop end to end: the release is declared, the prior
    release is frozen, the index carries the chain, and the derived changelog
    (item 261) is stored beside the release with item 64's bump as its
    headline."""
    reg = _empty_registry(tmp_path, "community", "audit")
    proj = _project(tmp_path, registry=reg, reg_name="community", source=GREETER,
                    description="Greets.", tags=["greet"], dossier=None,
                    version="1.0.0", publisher="acme")
    assert _truc(proj, "ship", "greeter").returncode == 0

    proj2 = _project(tmp_path / "second", registry=reg, reg_name="community",
                     source=GREETER_PLUS, description="Greets, and says bye.",
                     tags=["greet"], dossier=None, version="1.1.0",
                     publisher="acme")
    r = _truc(proj2, "ship", "greeter")
    assert r.returncode == 0, r.stdout
    assert '"greeter@1.1.0" (replacing 1.0.0)' in r.stdout

    entry = reg / "components" / "greeter"
    assert entry.joinpath("component.rvl").read_text() == GREETER_PLUS
    assert entry.joinpath("version").read_text().strip() == "1.1.0"
    # the release it replaced keeps its own bytes.
    assert entry.joinpath("releases", "1.0.0", "component.rvl").read_text() == GREETER
    row = json.loads((reg / "index.json").read_text())["components"]["greeter"]
    assert row["version"] == "1.1.0"
    assert row["releases"] == ["1.0.0", "1.1.0"]
    changelog = json.loads(
        entry.joinpath("releases", "1.1.0", "changelog.json").read_text())
    assert changelog["headline"]["bump"] == "minor"
    record = json.loads(entry.joinpath("releases", "1.1.0", "release.json").read_text())
    assert record["bumpCheck"] == "verified"
    assert record["publisherContinuity"] == "verified"


def test_ship_refuses_a_release_that_under_bumps_the_computed_one(tmp_path):
    """Item 64's registry-refusal half, through truc: a breaking reshape
    declared as a patch is refused by name and the registry is untouched."""
    reg = _empty_registry(tmp_path, "community", "audit")
    proj = _project(tmp_path, registry=reg, reg_name="community", source=GREETER,
                    description="Greets.", tags=["greet"], dossier=None,
                    version="1.0.0")
    assert _truc(proj, "ship", "greeter").returncode == 0

    breaking = GREETER.replace("fn hello(name: Str) -> Str",
                               "fn hello(name: Str, loud: Bool) -> Str").replace(
        "fn hello(name) =", "fn hello(name, loud) =")
    proj2 = _project(tmp_path / "second", registry=reg, reg_name="community",
                     source=breaking, description="Greets louder.",
                     tags=["greet"], dossier=None, version="1.0.1")
    r = _truc(proj2, "ship", "greeter")
    assert r.returncode == 1, r.stdout
    assert "contradicts the computed bump" in r.stdout
    assert "major bump" in r.stdout
    assert (reg / "components" / "greeter" / "component.rvl").read_text() == GREETER


def test_ship_refuses_a_name_changing_publisher(tmp_path):
    reg = _empty_registry(tmp_path, "community", "audit")
    proj = _project(tmp_path, registry=reg, reg_name="community", source=GREETER,
                    description="Greets.", tags=["greet"], dossier=None,
                    version="1.0.0", publisher="acme")
    assert _truc(proj, "ship", "greeter").returncode == 0

    proj2 = _project(tmp_path / "second", registry=reg, reg_name="community",
                     source=GREETER_PLUS, description="Mine now.", tags=["greet"],
                     dossier=None, version="1.1.0", publisher="squatter")
    r = _truc(proj2, "ship", "greeter")
    assert r.returncode == 1, r.stdout
    assert "does not change hands silently" in r.stdout
    assert (reg / "components" / "greeter" / "component.rvl").read_text() == GREETER


# ---------------------------------------------------------------- refusal: won't compile

def test_ship_refuses_a_component_that_does_not_admit(tmp_path):
    reg = _empty_registry(tmp_path, "plain", None)
    proj = _project(tmp_path, registry=reg, reg_name="plain",
                    source="component Broken { this is not revl }",
                    description="Looks fine.", tags=["x"], dossier=None)
    r = _truc(proj, "ship", "brokencomp")
    assert r.returncode == 1, r.stdout
    assert "does not admit" in r.stdout
    assert "brokencomp" not in _names(reg)
