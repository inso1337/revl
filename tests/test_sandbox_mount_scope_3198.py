"""A container-sandboxed body reads its declared envelope plus the files its own
composition compile read, never the conductor's working directory (issue #3198).

Design 411 says the envelope, not the gate, is the security boundary. Before
this fix the driver added `-v <cwd>:<cwd>:ro` and
`-v <parent-of-each-source>:<same>:ro` to every container launch, so a body
could read the operator's home or project tree regardless of the declared `fs`
grants. These tests pin the narrowed set: one read-only FILE mount per compile
input, no directory mount and no working-directory mount, plus a refusal for any
driver-added directory mount that reaches past the envelope.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl import sandbox_runtime as _sb  # noqa: E402

_APP = ('service Job { emission fn run() -> Str }\n'
        'component Lonely provides job: Job {\n'
        '  provide job { fn run() = "ok" }\n'
        '}\n')

_ENV = {"isolation": "container", "image": "img:1", "fs": [], "net": "none"}


# ==========================================================================
# 1. the driver-added mount set: files, not directories
# ==========================================================================


def test_source_mounts_are_file_mounts_not_their_directories(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    app = project / "agent.rvl"
    app.write_text(_APP, encoding="utf-8")
    # a secret sitting BESIDE the composition: a directory mount handed it to
    # the confined body, which is exactly the widening the envelope refuses.
    (project / ".env").write_text("TOKEN=secret\n", encoding="utf-8")

    mounts = _sb.source_mounts([str(app)])

    assert mounts == [(str(app.resolve()), "ro")]
    assert str(project.resolve()) not in [path for path, _ in mounts]


def test_a_relative_root_resolves_without_mounting_the_conductor_cwd(
        tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    app = project / "agent.rvl"
    app.write_text(_APP, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    mounts = _sb.source_mounts(["project/agent.rvl"])

    assert mounts == [(str(app.resolve()), "ro")]
    assert str(tmp_path.resolve()) not in [path for path, _ in mounts]


def test_wrap_does_not_point_the_container_at_the_conductor_cwd(tmp_path):
    app = tmp_path / "agent.rvl"
    app.write_text(_APP, encoding="utf-8")
    achieved = {"_env": _ENV, "image": "img:1",
                "mounts": [(str(app), "ro")],
                "workdir": str(tmp_path), "pythonpath": None}

    argv, err = _sb.ContainerDriver().wrap(
        "p", [sys.executable, "-m", "revl._process_runner", "/spec"], None,
        achieved)

    assert err is None
    assert "--workdir" not in argv
    assert f"{tmp_path}:{tmp_path}:ro" not in argv


def test_the_reproduced_launch_does_not_mount_the_working_directory(tmp_path):
    """The issue's reproduction, through the same pure flag builders.

    With a composition at `<project>/agent.rvl` and the conductor started in
    `<tmp>`, the launch used to carry `<tmp>:<tmp>:ro` and
    `<project>:<project>:ro`. Neither may appear any more; the one file the
    process re-compiles must."""
    project = tmp_path / "project"
    project.mkdir()
    app = project / "agent.rvl"
    app.write_text(_APP, encoding="utf-8")
    seam = tmp_path / "placement"
    seam.mkdir()
    spec = seam / "agent.spec.json"
    spec.write_text("{}", encoding="utf-8")
    grant = tmp_path / "srv" / "data"
    grant.mkdir(parents=True)
    env = {**_ENV, "fs": [f"{grant}:ro"]}

    declared = _sb.seam_dir_mounts(
        {"seam_dir": str(seam), "spec_path": str(spec)}
    ) + _sb.envelope_mounts(env)
    host = _sb.source_mounts([str(app)])
    flags = _sb.container_flags(env, name="c", mounts=declared + host)
    joined = " ".join(flags)

    assert f"{tmp_path}:{tmp_path}:ro" not in joined
    assert f"{project}:{project}:ro" not in joined
    assert "--workdir" not in flags
    assert f"{app}:{app}:ro" in joined
    assert f"{grant}:{grant}:ro" in joined


# ==========================================================================
# 2. a driver-added directory mount never reaches past the envelope
# ==========================================================================


def test_a_host_mount_that_covers_the_operators_home_is_refused(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    err = _sb._overbroad_host_mount("p", [(str(tmp_path), "ro")],
                                    {"fs": []}, home=home)
    assert err and str(tmp_path) in err and "home" in err


def test_a_host_mount_that_covers_a_declared_grant_is_refused(tmp_path):
    mount = tmp_path / "srv"
    grant = mount / "data"
    grant.mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    err = _sb._overbroad_host_mount("p", [(str(mount), "ro")],
                                    {"fs": [f"{grant}:ro"]}, home=home)
    assert err and str(mount) in err and str(grant) in err


def test_a_single_file_or_an_unrelated_directory_is_admitted(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    app = tmp_path / "app.rvl"
    app.write_text(_APP, encoding="utf-8")
    checkout = tmp_path / "checkout" / "src"
    checkout.mkdir(parents=True)
    # a conductor checkout INSIDE the operator's home is normal (an ancestor of
    # home is the leak, a subdirectory of it is not)
    inside_home = home / "revl" / "src"
    inside_home.mkdir(parents=True)

    assert _sb._overbroad_host_mount(
        "p", [(str(app), "ro")], {"fs": []}, home=home) is None
    assert _sb._overbroad_host_mount(
        "p", [(str(checkout), "ro")], {"fs": []}, home=home) is None
    assert _sb._overbroad_host_mount(
        "p", [(str(inside_home), "ro")], {"fs": []}, home=home) is None


# ==========================================================================
# 3. the compile reports every host file the sandbox must mount to re-compile
# ==========================================================================


def _compile_fixture(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "util.rvl").write_text("service Util { fn ping() -> Str }\n",
                                  encoding="utf-8")
    (lib / "host.js").write_text(
        "export function work() { return 'x'; }\n", encoding="utf-8")
    (lib / "body.py").write_text("def helper():\n    return 'x'\n",
                                 encoding="utf-8")
    (lib / "a.txt").write_text("asset\n", encoding="utf-8")
    app = tmp_path / "app.rvl"
    app.write_text(
        'use "lib/util.rvl" as util\n'
        "type Asset = { path: Str, sha256: Str }\n"
        'extern pure fn work() -> Str = @ts ref work from "lib/host.js"\n'
        'extern pure fn helper() -> Str = @py file "lib/body.py"\n'
        'pub fn entry() -> Asset { return asset "lib/a.txt" }\n'
        "service Job { emission fn run() -> Str }\n"
        "component Lonely provides job: Job {\n"
        '  provide job { fn run() = work() }\n'
        "}\n",
        encoding="utf-8")
    return {"app": app, "util": lib / "util.rvl", "ref": lib / "host.js",
            "body": lib / "body.py", "asset": lib / "a.txt"}


def test_a_compile_reports_the_host_files_a_recompile_must_read(tmp_path):
    """The in-sandbox runner re-compiles the roots it is handed (item 337), so
    every file that compile reads has to be mounted into the boundary. Modules
    are reported in `disk_reads`; the non-module files (a ref, an asset, a host
    body) in `host_file_reads`, so placement can mount the exact input set."""
    files = _compile_fixture(tmp_path)
    report: dict = {}

    compile_files([str(files["app"])], report=report)

    assert str(files["app"]) in report["disk_reads"]
    assert str(files["util"]) in report["disk_reads"]
    assert str(files["ref"]) in report["host_file_reads"]
    assert str(files["body"]) in report["host_file_reads"]
    assert str(files["asset"]) in report["host_file_reads"]


def test_a_generated_types_file_reports_its_model_document(tmp_path):
    """A `revl gen-types` file's model document is read from disk to check its
    digest, so the in-boundary recompile needs it mounted too."""
    from revl.gen_types import gen_types_file
    fixture = ROOT / "tests" / "fixtures" / "gen_types" / "rentals.model.json"
    model = tmp_path / "rentals.model.json"
    model.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    types = tmp_path / "types.rvl"
    types.write_text(gen_types_file(str(model), str(types)), encoding="utf-8")
    report: dict = {}

    compile_files([str(types)], report=report)

    assert str(model.resolve()) in report["host_file_reads"]


def test_a_symlinked_input_is_mounted_under_the_spelling_the_compile_opens(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "f.rvl").write_text(_APP, encoding="utf-8")
    (tmp_path / "lnk").symlink_to(real, target_is_directory=True)
    spelled = tmp_path / "lnk" / "f.rvl"

    mounts = _sb.source_mounts([str(spelled)])

    # the in-boundary compile opens the spelling it was handed
    assert (str(spelled), "ro") in mounts


def test_the_recompile_reads_only_files_the_driver_would_mount(tmp_path):
    """End to end: compile once the way the conductor does, then re-compile from
    ONLY the mounted file set (the confined process sees nothing else) and
    require that no read falls outside it. Catches a read the report misses."""
    import builtins
    import io
    import os

    files = _compile_fixture(tmp_path)
    report: dict = {}
    compile_files([str(files["app"])], report=report)
    mounted = {str(files["app"]), *report["disk_reads"],
               *report["host_file_reads"]}
    seen: set[str] = set()
    real_open = builtins.open
    real_os_open = os.open

    def spy_open(path, *a, **k):
        if isinstance(path, (str, os.PathLike)):
            seen.add(os.path.abspath(os.fspath(path)))
        return real_open(path, *a, **k)

    def spy_os_open(path, *a, **k):
        seen.add(os.path.abspath(os.fspath(path)))
        return real_os_open(path, *a, **k)

    # `Path.read_text` goes through `io.open`, which `builtins.open` patching
    # alone does not catch
    builtins.open, io.open, os.open = spy_open, spy_open, spy_os_open
    try:
        compile_files([str(files["app"])])
    finally:
        builtins.open, io.open, os.open = real_open, real_open, real_os_open

    under_project = {p for p in seen if p.startswith(str(tmp_path))}
    assert under_project, "the spy saw no project read at all"
    assert under_project <= mounted, sorted(under_project - mounted)


# ==========================================================================
# 4. the guard is wired into the driver's own preflight
# ==========================================================================


def _preflight_with(tmp_path, monkeypatch, files, env=None):
    """`ContainerDriver.preflight` with the daemon stubbed out: it reaches the
    point where the driver-added host mounts are judged."""
    driver = _sb.ContainerDriver(docker="/usr/bin/docker")
    monkeypatch.setattr(_sb, "_run", lambda *a, **k: (0, "24.0", ""))
    monkeypatch.setattr(driver, "_ensure_image", lambda *a: None)
    probe = {"RUNTIME": "image", "PY": "yes", "MOUNTS": {}}
    monkeypatch.setattr(driver, "_probe", lambda *a, **k: (probe, None))
    monkeypatch.setattr(driver, "_evaluate", lambda *a, **k: (["ok"], None))
    seam = tmp_path / "seam"
    seam.mkdir(exist_ok=True)
    spec = seam / "p.spec.json"
    spec.write_text("{}", encoding="utf-8")
    ctx = {"backend": "py", "seam_dir": str(seam), "spec_path": str(spec),
           "files": [str(f) for f in files]}
    return driver.preflight("p", env or _ENV, ctx)


def test_preflight_records_file_mounts_and_no_workdir(tmp_path, monkeypatch):
    app = tmp_path / "app.rvl"
    app.write_text(_APP, encoding="utf-8")

    achieved, err = _preflight_with(tmp_path, monkeypatch, [app])

    assert err is None, err
    assert achieved["host_mounts"] == [(str(app), "ro")]
    assert "workdir" not in achieved


def test_preflight_refuses_a_directory_mount_over_the_home(tmp_path, monkeypatch):
    monkeypatch.setattr(_sb.Path, "home", classmethod(lambda cls: tmp_path / "h"))
    (tmp_path / "h").mkdir()
    # a driver-added DIRECTORY that is an ancestor of home (the old cwd mount)
    monkeypatch.setattr(_sb, "source_mounts",
                        lambda files: [(str(tmp_path), "ro")])

    achieved, err = _preflight_with(tmp_path, monkeypatch, [tmp_path])

    assert achieved is None
    assert err and "operator's home" in err


def test_preflight_refuses_a_compile_input_that_vanished(tmp_path, monkeypatch):
    """`-v` creates a missing host path as a directory, so a compile input that
    disappeared between the compile and the launch would be mounted as an empty
    directory. The driver refuses it instead."""
    gone = tmp_path / "gone.rvl"

    achieved, err = _preflight_with(tmp_path, monkeypatch, [gone])

    assert achieved is None
    assert err and str(gone) in err and "no longer exists" in err
