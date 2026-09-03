"""The `container` isolation rung, made real (roadmap item 411, Slice 2).

Slice 1 declared and gated a sandbox; nothing was ever confined. This file
covers the runtime driver for the first rung of the ladder: the confinement
flags one envelope derives, the in-sandbox boot canary that CONFIRMS the
boundary from inside it, the refusals that replace every silent downgrade, and
an end-to-end placement whose component really does boot inside a container.

Levels:

1. derived confinement + refusals, with no container runtime at all: the flags
   an envelope produces, the rungs that have no driver (and the placement-level
   refusal that follows), the seam/backend/runtime refusals, and — the core of
   "refuse, never degrade" — `_evaluate`, judging canary reports that fail to
   confirm each envelope clause;
2. against a REAL container runtime, gated on `REVL_SANDBOX_DOCKER` (the
   `sandbox-container` CI job sets it, so these execute in CI and are not a
   suite that quietly never runs — item 445): the boundary is established and
   the canary confirms it, an unresolvable image refuses, a whole placement
   boots its component inside the container and tears it down, and nothing is
   left running afterwards.

Level 2 costs one small image build (a stock python image plus cordis-py's two
third-party imports) and a handful of short-lived containers, all labelled
`revl.sandbox=411` and removed on the way out.
"""

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import placement as _placement  # noqa: E402
from revl import sandbox_runtime as _sb  # noqa: E402

# The switch that says a container runtime is available AND meant to be used.
# It is deliberately an env gate rather than a `shutil.which("docker")` probe:
# a filesystem probe is invisible to tests/test_env_gated_skips_run_somewhere.py
# and would let this whole level skip in CI without anything noticing, which is
# the exact failure item 445 exists to stop. With the variable set, a broken or
# absent runtime REDS these tests instead of skipping them.
_DOCKER_GATE = "REVL_SANDBOX_DOCKER"

def _docker_gate_unset() -> bool:
    """Whether the container rung SKIPS: true exactly when the gate variable is
    absent or empty.

    The read is BARE — no default — so nothing but CI (or a developer at a
    keyboard) can supply a value, and a job that forgets to set it cannot be
    papered over by a fallback. Named rather than inlined so the property can be
    driven against each environment state instead of grepped for."""
    return not os.environ.get(_DOCKER_GATE)


_needs_docker = pytest.mark.skipif(
    _docker_gate_unset(),
    reason=(f"{_DOCKER_GATE} is unset: the container rung needs a working "
            f"container runtime (a `docker` CLI and a reachable daemon) to "
            f"launch a real boundary. The `sandbox-container` CI job sets it; "
            f"locally, start Docker and set it to 1."))

_IMAGE_TAG = "revl-sandbox-test:py312"
# cordis-py imports pyyaml and watchdog at package import time, and the runner
# imports cordis. An image for the container rung must carry the runner's
# third-party dependencies (the driver mounts revl + cordis-py themselves, so
# the confined process runs the conductor's own vintage).
_DOCKERFILE = "FROM python:3.12-slim\nRUN pip install --no-cache-dir pyyaml watchdog\n"

_ENV_NONE = {"isolation": "container", "image": _IMAGE_TAG, "fs": [], "net": "none"}


# ==========================================================================
# 1. derived confinement + refusals (no container runtime needed)
# ==========================================================================


def test_net_none_derives_a_network_namespace_with_no_egress():
    flags = _sb.container_flags(_ENV_NONE, name="c", mounts=[])
    assert "--network=none" in flags


def test_net_all_does_not_claim_a_network_confinement():
    flags = _sb.container_flags({**_ENV_NONE, "net": "all"}, name="c", mounts=[])
    assert "--network=none" not in flags


def test_every_container_is_hardened_beyond_the_envelope():
    # the rung is not just fs+net: a read-only root, no capabilities and no
    # privilege escalation are what make it a boundary rather than a chroot.
    flags = _sb.container_flags(_ENV_NONE, name="c", mounts=[])
    for expected in ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges"):
        assert expected in flags
    assert "--label" in flags and "revl.sandbox=411" in flags


def test_mounts_are_identity_mapped_with_their_declared_mode():
    flags = _sb.container_flags(
        _ENV_NONE, name="c", mounts=[("/scratch", "rw"), ("/data", "ro")])
    assert "/scratch:/scratch:rw" in flags
    assert "/data:/data:ro" in flags


def test_envelope_mounts_default_to_read_only():
    assert _sb.envelope_mounts({"fs": ["/a", "/b:rw", "/c:ro"]}) == [
        ("/a", "ro"), ("/b", "rw"), ("/c", "ro")]


def test_only_the_container_rung_has_a_driver():
    assert _sb.resolve_driver("container") is not None
    # the other two rungs are NOT quietly treated as the rung below them
    assert _sb.resolve_driver("wasm-cell") is None
    assert _sb.resolve_driver("microvm") is None


def test_a_rung_with_no_driver_refuses_rather_than_running_unconfined():
    driver = _sb.resolve_driver("microvm")
    assert driver is None, (
        "a rung with no driver must resolve to None so the conductor refuses; "
        "returning a weaker rung's driver would silently downgrade the boundary")


def test_missing_container_runtime_is_a_refusal_naming_what_is_missing():
    driver = _sb.ContainerDriver(docker="")  # nothing on PATH
    achieved, err = driver.preflight("p", _ENV_NONE, {"backend": "py", "seam_dir": "/t"})
    assert achieved is None
    assert "no container runtime is on PATH" in err
    assert "never downgraded" in err


def test_a_cross_boundary_seam_refuses_until_the_transport_lands():
    driver = _sb.ContainerDriver(docker="/nonexistent/docker")
    achieved, err = driver.preflight(
        "p", _ENV_NONE, {"backend": "py", "seam_dir": "/t", "seam_keys": ["work"]})
    assert achieved is None
    assert "cross-boundary seam" in err and "work" in err


def test_a_non_py_backend_in_a_container_refuses():
    driver = _sb.ContainerDriver(docker="/nonexistent/docker")
    achieved, err = driver.preflight(
        "p", _ENV_NONE, {"backend": "rust", "seam_dir": "/t"})
    assert achieved is None
    assert "py` backend only" in err


# -- the heart of it: a canary report that does not CONFIRM the envelope ----

def _report(**over) -> dict:
    """A canary report from a boundary that IS established, before `over`."""
    base = {"PY": "yes", "ROUTES": "0", "EGRESS": "blocked:101", "ROOTFS": "ro",
            "RUNTIME": "image", "MOUNTS": {"/seam": "rw,relatime", "/data": "ro,relatime"}}
    base.update(over)
    return base


def _judge(report, mounts=(("/seam", "rw"),), env=None):
    return _sb.ContainerDriver()._evaluate(
        "p", env or _ENV_NONE, _IMAGE_TAG, list(mounts), report)


def test_a_confirmed_boundary_reports_its_evidence():
    lines, err = _judge(_report())
    assert err is None
    assert any("net=none confirmed in-sandbox" in ln for ln in lines)
    assert any("root filesystem read-only" in ln for ln in lines)
    assert any("mount /seam rw, confirmed in-sandbox" in ln for ln in lines)


def test_a_route_in_the_namespace_refuses_net_none():
    _, err = _judge(_report(ROUTES="2"))
    assert err and "did not take" in err and "never silently downgraded" in err


def test_an_egress_probe_that_does_not_answer_refuses():
    # a timeout is not a pass. An unconfirmed boundary is refused exactly like a
    # broken one, because the composition cannot tell them apart either.
    _, err = _judge(_report(EGRESS="timeout"))
    assert err and "unconfirmed" in err


def test_an_open_egress_refuses():
    _, err = _judge(_report(EGRESS="open"))
    assert err and "unconfirmed" in err


def test_a_writable_root_filesystem_refuses():
    _, err = _judge(_report(ROOTFS="rw"))
    assert err and "--read-only` did not take" in err


def test_a_missing_granted_mount_refuses():
    _, err = _judge(_report(), mounts=[("/scratch", "rw")])
    assert err and "not in /proc/mounts" in err


def test_a_mount_whose_mode_widened_refuses():
    # declared read-only, established read-write: the envelope the process would
    # run under is not the one the manifest claims, so it never starts.
    _, err = _judge(_report(), mounts=[("/seam", "ro")])
    assert err and "the envelope declares ro" in err


def test_an_image_without_python_refuses():
    _, err = _judge(_report(PY="no"))
    assert err and "has no `python3`" in err


def test_net_all_confines_nothing_and_says_so():
    lines, err = _judge(_report(ROUTES="1", EGRESS="unclaimed"),
                        env={**_ENV_NONE, "net": "all"})
    assert err is None
    assert any("nothing confined" in ln for ln in lines)


# -- the placement-level refusal --------------------------------------------

_APP = """
service Job { emission fn run() -> Str }
component Lonely provides job: Job {
  provide job { fn run() = "ok" }
}
"""


def _placement_files(tmp_path, rung: str, image: str = _IMAGE_TAG):
    app = tmp_path / "app.rvl"
    app.write_text(_APP, encoding="utf-8")
    toml = tmp_path / "p.toml"
    toml.write_text('default_tier = "py"\n'
                    '[sandbox]\n'
                    f'Lonely = {{ isolation = "{rung}", image = "{image}" }}\n',
                    encoding="utf-8")
    return str(app), str(toml)


def test_placement_refuses_a_rung_with_no_driver_and_spawns_nothing(tmp_path, capsys):
    app, toml = _placement_files(tmp_path, "microvm")
    spawned = []

    def spy(cmd, **kw):  # pragma: no cover - asserted never to run
        spawned.append(cmd)
        raise AssertionError("a placement whose isolation cannot be established "
                             "must not spawn anything")

    with mock.patch.object(_placement, "_cordis_py_installed", lambda: True), \
         mock.patch.object(_placement, "_preflight", lambda *a, **k: None), \
         mock.patch.object(_placement.subprocess, "Popen", spy):
        rc = _placement.run_placement([app], toml, once=True)
    assert rc == 1
    assert spawned == []
    err = capsys.readouterr().err
    assert "no runtime driver in this build" in err
    assert "never downgraded to an unconfined process" in err


def test_audit_names_the_enforcement_each_rung_would_get(tmp_path):
    from revl import compile_files
    app, _ = _placement_files(tmp_path, "container")
    ir = compile_files([app])
    lines, err = _placement.sandbox_audit_view(
        ir, {"default_tier": "py",
             "sandbox": {"Lonely": {"isolation": "container", "image": _IMAGE_TAG}}})
    assert err is None
    assert any("enforcement: rung container has a runtime driver" in ln for ln in lines)

    lines, err = _placement.sandbox_audit_view(
        ir, {"default_tier": "py",
             "sandbox": {"Lonely": {"isolation": "wasm-cell"}}})
    assert err is None
    assert any("enforcement: NONE" in ln and "REFUSES" in ln for ln in lines)


# ==========================================================================
# 2. against a real container runtime (gated: REVL_SANDBOX_DOCKER)
# ==========================================================================


def _docker(*args, **kw):
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=kw.pop("timeout", 600))


@pytest.fixture(scope="module")
def runner_image():
    """A minimal image that can host the py runner: a stock python plus the two
    third-party modules cordis-py imports. Built once, reused afterwards."""
    if _docker("image", "inspect", _IMAGE_TAG).returncode != 0:
        built = subprocess.run(
            ["docker", "build", "-t", _IMAGE_TAG, "-"], input=_DOCKERFILE,
            capture_output=True, text=True, timeout=900)
        assert built.returncode == 0, built.stderr[-2000:]
    return _IMAGE_TAG


@_needs_docker
def test_the_boundary_is_established_and_the_canary_confirms_it(runner_image, tmp_path):
    seam = tmp_path / "seam"
    seam.mkdir()
    grant = tmp_path / "grant"
    grant.mkdir()
    env = {"isolation": "container", "image": runner_image,
           "fs": [f"{grant}:rw"], "net": "none"}
    driver = _sb.ContainerDriver()
    achieved, err = driver.preflight(
        "sandbox_Lonely", env,
        {"backend": "py", "seam_dir": str(seam), "seam_keys": [],
         "files": [], "cwd": str(tmp_path)})
    assert err is None, err
    assert achieved["rung"] == "container" and achieved["enforced"] is True
    evidence = "\n".join(achieved["evidence"])
    # the confinement is CONFIRMED from inside, not inferred from the flags
    assert "net=none confirmed in-sandbox" in evidence
    assert "root filesystem read-only, confirmed in-sandbox" in evidence
    assert f"mount {grant} rw, confirmed in-sandbox" in evidence
    assert f"mount {seam} rw, confirmed in-sandbox" in evidence
    # and what the driver added on the author's behalf is named, not hidden
    assert achieved["host_mounts"], "driver-added mounts must be reported"


@_needs_docker
def test_an_unresolvable_image_refuses_instead_of_launching(tmp_path):
    env = {"isolation": "container", "fs": [], "net": "none",
           "image": "revl-sandbox-does-not-exist:404"}
    achieved, err = _sb.ContainerDriver().preflight(
        "p", env, {"backend": "py", "seam_dir": str(tmp_path), "seam_keys": [],
                   "files": [], "cwd": str(tmp_path)})
    assert achieved is None
    assert "neither present locally nor pullable" in err


@_needs_docker
def test_a_component_really_boots_inside_the_container(runner_image, tmp_path, capsys):
    """End to end: `revl run --placement` puts the component in a container with
    no network and a read-only root, it comes UP, serves its teardown and goes
    DOWN, and the conductor's summary reports the rung it ACHIEVED."""
    app, toml = _placement_files(tmp_path, "container", runner_image)
    launched: list = []
    real = _placement.subprocess.Popen

    def spy(cmd, **kw):
        launched.append(list(cmd))
        return real(cmd, **kw)

    with mock.patch.object(_placement.subprocess, "Popen", spy):
        rc = _placement.run_placement([app], toml, once=True)
    out = capsys.readouterr().out
    assert rc == 0, out
    # the runner was launched THROUGH the container runtime, not beside it.
    # (`launched` also holds the driver's own preflight calls, which go through
    # subprocess.run and therefore Popen; the runner is the one carrying the
    # process-runner module.)
    runner = [c for c in launched if "revl._process_runner" in c]
    assert len(runner) == 1, launched
    assert Path(runner[0][0]).name == "docker" and runner[0][1] == "run"
    assert "--network=none" in runner[0] and "--read-only" in runner[0]
    # ... and as the image's own python3, never the conductor's interpreter path
    assert "python3" in runner[0] and sys.executable not in runner[0]
    assert "[sandbox_Lonely] UP" in out and "[sandbox_Lonely] DOWN" in out
    assert "rung container ACHIEVED" in out
    assert "net=none confirmed in-sandbox" in out


@_needs_docker
def test_the_placement_leaves_no_container_behind():
    left = _docker("ps", "-a", "--filter", "label=revl.sandbox=411",
                   "--format", "{{.Names}}").stdout.strip()
    assert left == "", f"containers left behind: {left}"


@_needs_docker
def test_the_spec_the_confined_process_reads_never_leaves_the_boundary(tmp_path):
    """The spec file lives in the placement directory, which is the one mount
    the driver adds rw. Nothing else of the host is writable: a granted `ro`
    mount really is read-only inside."""
    seam = tmp_path / "seam"
    seam.mkdir()
    ro = tmp_path / "readonly"
    ro.mkdir()
    (ro / "witness").write_text("x", encoding="utf-8")
    name = "revl-sandbox-411-rotest"
    flags = _sb.container_flags(
        {"isolation": "container", "image": _IMAGE_TAG, "fs": [], "net": "none"},
        name=name, mounts=[(str(seam), "rw"), (str(ro), "ro")], interactive=False)
    proc = _docker("run", *flags, _IMAGE_TAG, "sh", "-c",
                   f"touch {ro}/breach 2>/dev/null && echo BREACH || echo REFUSED")
    assert "REFUSED" in proc.stdout, proc.stdout + proc.stderr
    assert not (ro / "breach").exists()


def test_the_gate_is_read_bare_so_ci_must_set_it():
    """A guard on the guard (item 445): the whole container rung hangs off one
    environment variable, so a read that grew a default — or a marker that
    stopped consulting the read at all — takes the level dark in CI with
    nothing to notice, because a skip and a pass are the same colour.

    Driven, not grepped. The assertion this replaced was
    `'os.environ.get(_DOCKER_GATE)' in source`, which is satisfied by the
    spelling appearing anywhere in the file — including in a helper the skip
    marker no longer calls. Here the predicate is evaluated against each
    environment state, and the marker the rung tests actually carry is checked
    to BE that predicate's verdict.

    `mock.patch.dict` rather than `monkeypatch.setenv`, deliberately:
    tests/test_env_gated_skips_run_somewhere.py reads a `setenv` of this name as
    the suite OWNING the switch, which would excuse CI from setting it."""
    assert _DOCKER_GATE == "REVL_SANDBOX_DOCKER"

    # the marker installed at import is this predicate's verdict, not an
    # independent expression that can drift away from it. Read before the
    # environment is touched.
    assert _needs_docker.mark.name == "skipif"
    assert _needs_docker.mark.args == (_docker_gate_unset(),)
    assert _DOCKER_GATE in _needs_docker.mark.kwargs["reason"]

    # and it is the rung tests that carry it — a level whose gate is perfect and
    # applied to nothing is the same silence by another route.
    for gated in (test_the_boundary_is_established_and_the_canary_confirms_it,
                  test_a_component_really_boots_inside_the_container,
                  test_an_unresolvable_image_refuses_instead_of_launching):
        assert _needs_docker.mark in gated.pytestmark, gated.__name__

    with mock.patch.dict(os.environ, {}, clear=True):
        assert _docker_gate_unset() is True, "absent must skip"
    with mock.patch.dict(os.environ, {_DOCKER_GATE: ""}, clear=True):
        assert _docker_gate_unset() is True, "empty must skip — the read is bare"
    with mock.patch.dict(os.environ, {_DOCKER_GATE: "1"}, clear=True):
        assert _docker_gate_unset() is False, "set must RUN the rung"
