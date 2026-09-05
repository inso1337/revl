"""truc slice S5 — the bootstrap fixpoint: **truc assembles truc**.

The dogfood finale. truc is built in revl (its eight components under
`src/revl/truc/components/`); this test expresses truc *itself* as a truc
project (`src/revl/truc/bootstrap/truc.toml`, whose `[assembly]` is truc's own
components) and proves the fixpoint: run truc's own assemble path over truc's
components and every one is admitted through revl's G2/G4 gate — the same gate
truc admits everyone else with — and they compose into truc's running
composition. truc manages itself.

Two layers, by design:

* **The regenerate-or-red gate (always runs).** The admission gate is
  `revl.compiler.compile_files` and truc's stepping over it is
  `revl.truc._host.admit_all` — both pure frontend Python, no cordis runtime.
  So this layer runs in the normal `pytest tests/` suite *without* the backend
  venv, which is the whole point: CI's frontend job runs `pytest tests/`, and a
  self-assembly that ever drifts from the committed composition
  (`bootstrap/assembly.golden.json`) turns that job red — it never skips. This
  is the same regenerate-or-red discipline as `registry.verify` and the
  conformance baselines, applied to truc's own composition.

* **The end-to-end CLI proof (skips without the runtime).** Drives the real
  `truc assemble` console command through the cordis-py venv on the bootstrap
  project, so the whole path — launcher → Session → cli.run → asm.assemble →
  gate.admit_all → build/assembly.json — is exercised, not just the gate call.
  Set up with `sh backends/python/setup.sh`.

The gate is exercised *on truc itself*: a G2-violating injection into truc's
own component set (a second provider of `plan`) is refused with the why-trace,
and nothing is written — truc would refuse a broken truc.
"""

import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TRUC = ROOT / "src" / "revl" / "truc"
BOOTSTRAP = TRUC / "bootstrap"
GOLDEN = BOOTSTRAP / "assembly.golden.json"
CORDIS_PY = ROOT / "backends" / "python" / ".venv" / "bin" / "python"

# a second provider of the `plan` key — a deliberate G2 break injected into
# truc's *own* component set, to prove the gate refuses a broken truc.
ROGUE_PLAN = """\
service RoguePlan { fn noop() -> Int }
component RoguePlanner provides plan: RoguePlan {
  provide plan { fn noop() = 0 }
}
"""


def _bootstrap_entry() -> list[Path]:
    """truc's own components, in the order the committed manifest names them —
    the `[assembly]` of truc-as-a-truc-project, anchored to this checkout."""
    manifest = tomllib.loads((BOOTSTRAP / "truc.toml").read_text())
    return [(TRUC / rel).resolve() for rel in manifest["assembly"]["entry"]]


def _ordered(paths: list[Path]) -> str:
    """The `[{path, source, name}]` list truc's Planner hands the gate: entry
    files carry an empty name (they are the project's own composition, not
    fetched trucs). This mirrors `_host.read_sources` + `plan_ordered_sources`
    exactly — it is the input `truc assemble` feeds `admit_all`."""
    return json.dumps([
        {"path": str(p), "source": p.read_text(encoding="utf-8"), "name": ""}
        for p in paths])


def _normalize(manifest: dict) -> dict:
    """Strip the checkout-absolute `file` path down to its basename so the
    committed golden is checkout-independent (the composition — names, wiring,
    load order — is what is pinned, not where the repo lives)."""
    out = json.loads(json.dumps(manifest))
    for comp in out.get("components", []):
        if "file" in comp:
            comp["file"] = os.path.basename(comp["file"])
    return out


# ============================================================ regenerate-or-red
# These run with NO cordis runtime — the admission gate is frontend Python — so
# the CI frontend job (`pytest tests/`) actually enforces them. They never skip.

def test_truc_assembles_truc_and_matches_the_committed_composition():
    """The fixpoint + regenerate-or-red. Run truc's own gate-stepping
    (`_host.admit_all`, exactly what `truc assemble` calls) over truc's eight
    components: every one is admitted, and the resulting composition is
    byte-identical to the committed golden. Drift → red."""
    from revl.truc import _host

    verdict = json.loads(_host.admit_all(_ordered(_bootstrap_entry())))

    assert verdict["ok"], verdict.get("diagnostic")
    assert verdict["failed"] == ""
    # all eight of truc's components passed the gate, in resolution order.
    assert verdict["admitted"] == [str(p) for p in _bootstrap_entry()]

    got = _normalize(verdict["manifest"])
    expected = json.loads(GOLDEN.read_text())
    assert got == expected, (
        "truc's self-assembly drifted from bootstrap/assembly.golden.json.\n"
        "If this change to truc's composition is intended, regenerate the "
        "golden; otherwise truc can no longer assemble truc.\n"
        f"got:\n{json.dumps(got, indent=2)}")

    # the composition is the eight named in the manifest — no more, no less.
    assert [c["name"] for c in got["components"]] == [
        "RegistryClient", "Fetcher", "GateKeeper", "Workspace",
        "Planner", "Assembler", "Shipper", "CliDispatch"]


def test_whole_composition_admits_as_one_stage0_boot():
    """Stage 0: the launcher compiles truc's whole composition in one
    `compile_files` — that compile *is* an admission (G2/G3/A6 over the set),
    so every boot of truc is truc passing its own gate. Prove that cold-start
    admits and agrees with the incremental path on the composition."""
    from revl.compiler import compile_files

    entry = _bootstrap_entry()
    ir = compile_files([str(p) for p in entry])   # raises RevlError on refusal
    got = _normalize(ir["manifest"])
    assert got == json.loads(GOLDEN.read_text())


def test_the_brain_is_pure_the_authority_is_the_rim():
    """The audit property truc advertises about other people's assemblies,
    shown on truc itself: the Planner (truc's resolver brain) injects nothing
    and provides only `plan` — it holds no boundary authority — while the rim
    components carry every emission. Pinned via the committed composition."""
    golden = json.loads(GOLDEN.read_text())
    by_name = {c["name"]: c for c in golden["components"]}
    planner = by_name["Planner"]
    assert planner["inject"] == [] and planner["provides"] == ["plan"]
    # the core wires the rim: the Assembler injects the four rim keys + the
    # pure plan; the CLI injects the two it dispatches to.
    assert set(by_name["Assembler"]["inject"]) == {
        "index", "fetch", "gate", "ws", "plan"}
    assert set(by_name["CliDispatch"]["inject"]) == {"asm", "ship"}


def test_a_g2_break_injected_into_trucs_own_set_is_refused():
    """The gate, exercised on truc itself. Inject a second provider of `plan`
    into truc's own component set and the gate refuses it (G2) with the
    why-trace naming both providers — truc would refuse a broken truc. The
    empty guard means nothing is composed on refusal."""
    from revl.truc import _host

    entry = _bootstrap_entry()
    rogue = "/__rogue__/rogue_planner.rvl"
    ordered = json.loads(_ordered(entry))
    # after the real Planner is admitted, so the conflict is live.
    idx = next(i for i, o in enumerate(ordered)
               if o["path"].endswith("planner.rvl"))
    ordered.insert(idx + 1, {"path": rogue, "source": ROGUE_PLAN, "name": "rogue"})

    verdict = json.loads(_host.admit_all(json.dumps(ordered)))
    assert not verdict["ok"]
    assert verdict["failed"] == "rogue"
    assert "G2" in verdict["diagnostic"]
    assert "plan" in verdict["diagnostic"]
    assert "RoguePlanner" in verdict["diagnostic"]
    assert "Planner" in verdict["diagnostic"]


# ================================================================= end-to-end
# The whole console path — launcher → Session → cli.run → gate — on the real
# `truc assemble`. Needs the cordis-py runtime; skips cleanly without it.

pytestmark_runtime = pytest.mark.skipif(
    not CORDIS_PY.exists(),
    reason="cordis-py runtime not installed (run `sh backends/python/setup.sh`)")


def _project(tmp_path: Path, *, extra_entry: list[tuple[str, str]] = ()) -> Path:
    """Materialize truc-as-a-truc-project in a temp dir from the committed
    bootstrap manifest, with `[assembly].entry` anchored to this checkout's real
    component files (absolute paths a truc.toml in a temp dir can resolve).
    `extra_entry` appends `(filename, source)` components to the assembly — used
    to inject the G2 break."""
    proj = tmp_path / "truc-self"
    proj.mkdir()
    entry = [f'  "{p}",' for p in _bootstrap_entry()]
    for fname, src in extra_entry:
        f = proj / fname
        f.write_text(src)
        entry.append(f'  "{f}",')
    (proj / "truc.toml").write_text(
        '[assembly]\n'
        'name = "truc"\n'
        'entry = [\n' + "\n".join(entry) + '\n]\n\n'
        '[registries]\n'
        f'local = {{ path = "{ROOT / "registry"}" }}\n\n'
        '[trucs]\n')
    return proj


def _truc(project: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([str(CORDIS_PY), "-m", "revl.truc", *args],
                          cwd=str(project), env=env,
                          capture_output=True, text=True, timeout=300)


@pytestmark_runtime
def test_truc_assemble_cli_on_truc_itself_matches_golden(tmp_path):
    """End-to-end: `truc assemble` on truc-as-a-truc-project, through truc's own
    CLI, admits truc's components and writes the composition — matching the
    committed golden. The console command assembles truc."""
    proj = _project(tmp_path)
    r = _truc(proj, "assemble")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "admitted through the gate" in r.stdout

    got = _normalize(json.loads((proj / "build" / "assembly.json").read_text()))
    assert got == json.loads(GOLDEN.read_text())


@pytestmark_runtime
def test_truc_assemble_cli_refuses_a_g2_break_in_trucs_own_set(tmp_path):
    """End-to-end refusal: a G2 break injected into truc's own assembly makes
    the real `truc assemble` exit 1 with the why-trace, and — all-or-nothing —
    writes no composition."""
    proj = _project(tmp_path, extra_entry=[("rogue_planner.rvl", ROGUE_PLAN)])
    r = _truc(proj, "assemble")
    assert r.returncode == 1, r.stdout
    assert "G2" in r.stdout and "plan" in r.stdout
    assert not (proj / "build" / "assembly.json").exists()


# ========================================================= the stage-0 lock
# truc's own `truc.lock` is what stage 0 compiles from, and it is the only
# thing standing between "truc boots the composition that was released" and
# "truc boots whatever .rvl files are on disk". Roadmap 428 F3: the pin
# requirement landed for a *project's* trucs, but truc's own lock was a bare
# file list with no hashes, no containment check, and a glob fallback that made
# the whole thing opt-out by deleting one file. These pin the refusals.

LOCK = TRUC / "truc.lock"


@pytest.fixture
def package(tmp_path, monkeypatch):
    """A throwaway copy of truc's package directory, with the launcher pointed
    at it, so a test may tamper with the lock or a component without touching
    the installed one."""
    import shutil

    from revl.truc import _launcher

    dst = tmp_path / "truc"
    shutil.copytree(TRUC, dst)
    monkeypatch.setattr(_launcher, "_HERE", dst)
    return dst


def _read_lock(package: Path) -> dict:
    return json.loads((package / "truc.lock").read_text())


def _write_lock(package: Path, doc: dict) -> None:
    (package / "truc.lock").write_text(json.dumps(doc, indent=2))


def test_the_committed_lock_is_current():
    """Regenerate-or-red, the same discipline as the golden composition above.
    Every hash in the committed `truc.lock` is the sha256 of the file it names,
    so an edit to a component that does not carry the lock with it turns CI red
    rather than silently shipping an unpinned boot."""
    from revl.truc._launcher import lock_document

    assert json.loads(LOCK.read_text()) == lock_document(), (
        "src/revl/truc/truc.lock drifted from truc's components.\n"
        "If this change to truc's components is intended, regenerate the lock "
        "(`python -P -m revl.truc._relock`; the `-P` is the PYTHONSAFEPATH "
        "safety bit, issue #317) and commit it.")


def test_the_lock_pins_every_stage0_source(package):
    """The happy path: all eight components resolve, inside the package, with
    matching pins."""
    from revl.truc import _launcher

    files = _launcher.component_files()
    assert len(files) == 8
    assert all(Path(f).is_file() for f in files)
    assert all(str(package) in f for f in files)


def test_a_deleted_lock_refuses_instead_of_globbing(package):
    """428 F3, the bootstrap half. Deleting the lock is no harder than
    corrupting it, and it used to be *better*: a missing lock fell back to
    `components/*.rvl`, so a planted component joined truc's own composition
    and every boot compiled it. A check that is turned off by deleting its
    input is not a check, so a missing lock now refuses to boot."""
    from revl.truc import _launcher

    (package / "components" / "rogue.rvl").write_text(ROGUE_PLAN)
    (package / "truc.lock").unlink()
    with pytest.raises(_launcher.BootstrapLockError) as excinfo:
        _launcher.component_files()
    assert "truc.lock is missing" in str(excinfo.value)


def test_a_component_the_lock_does_not_name_is_never_compiled(package):
    """The lock is the composition, not a hint: a file dropped into
    `components/` that no row names is simply not part of stage 0."""
    from revl.truc import _launcher

    (package / "components" / "rogue.rvl").write_text(ROGUE_PLAN)
    assert len(_launcher.component_files()) == 8


def test_drifting_component_bytes_refuse_to_boot(package):
    """The pin itself. A component whose bytes moved without the lock moving
    with them refuses, naming both hashes."""
    from revl.truc import _launcher

    (package / "components" / "planner.rvl").write_text("// tampered\n")
    with pytest.raises(_launcher.BootstrapLockError) as excinfo:
        _launcher.component_files()
    assert "hash drift" in str(excinfo.value)
    assert "components/planner.rvl" in str(excinfo.value)


@pytest.mark.parametrize("blank", ["", "   ", "not-a-hash", None, 64 * "z"])
def test_a_row_with_no_usable_pin_refuses(package, blank):
    """A pin is required, not optional — the same rule `planner.plan_drift`
    applies to a project's trucs. A blank or malformed `sha256` refuses exactly
    like a drifting one, so blanking a row is not a way to opt out."""
    from revl.truc import _launcher

    doc = _read_lock(package)
    doc["files"][0]["sha256"] = blank
    _write_lock(package, doc)
    with pytest.raises(_launcher.BootstrapLockError) as excinfo:
        _launcher.component_files()
    assert "no usable pin" in str(excinfo.value)


def test_a_path_leaving_the_package_refuses(package, tmp_path):
    """Containment. The lock's trust root is the installed package's own
    integrity, and that claim only holds if every byte stage 0 compiles is
    actually inside the package. A `..` row used to be resolved and compiled."""
    from revl.truc import _launcher

    outside = tmp_path / "evil.rvl"
    outside.write_text(ROGUE_PLAN)
    doc = _read_lock(package)
    doc["files"].append({"path": "../evil.rvl", "sha256": 64 * "0"})
    _write_lock(package, doc)
    with pytest.raises(_launcher.BootstrapLockError) as excinfo:
        _launcher.component_files()
    assert "leaves truc's package" in str(excinfo.value)


def test_a_symlinked_component_refuses(package, tmp_path):
    """The same containment, reached the other way. `Path.resolve()` follows a
    link planted in `components/` straight out of the package, so the pinned
    name would have described bytes the package does not own."""
    from revl.truc import _launcher

    outside = tmp_path / "evil.rvl"
    outside.write_text(ROGUE_PLAN)
    target = package / "components" / "planner.rvl"
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(_launcher.BootstrapLockError) as excinfo:
        _launcher.component_files()
    assert "symlink" in str(excinfo.value)


@pytest.mark.parametrize("doc", [
    {"lockVersion": 0, "files": ["components/cli.rvl"]},   # the pre-fix format
    {"lockVersion": 2, "files": []},
    {"files": [{"path": "components/cli.rvl", "sha256": 64 * "0"}]},
])
def test_an_unknown_lock_version_refuses(package, doc):
    """A version-0 lock is a bare file list carrying no hashes, so honouring it
    would be honouring no pins at all; an unknown future version may mean
    something this launcher cannot check. Both refuse rather than guess."""
    from revl.truc import _launcher

    _write_lock(package, doc)
    with pytest.raises(_launcher.BootstrapLockError) as excinfo:
        _launcher.component_files()
    assert "lockVersion" in str(excinfo.value)


@pytest.mark.parametrize("text", ["{not json", "[]", '{"lockVersion": 1}'])
def test_an_unusable_lock_refuses(package, text):
    """Unparseable, not an object, or naming no files: each is a tampering
    signal, and none of them degrades to compiling what is on disk."""
    from revl.truc import _launcher

    (package / "truc.lock").write_text(text)
    with pytest.raises(_launcher.BootstrapLockError):
        _launcher.component_files()


def test_the_launcher_exits_cleanly_on_a_refusal(package, capsys):
    """A stage-0 refusal is an exit code and a message naming the fix, not a
    traceback, and truc does not boot."""
    from revl.truc import _launcher

    (package / "truc.lock").unlink()
    assert _launcher.main(["assemble"]) == 70
    err = capsys.readouterr().err
    assert "refusing to boot" in err
    assert "_relock" in err
