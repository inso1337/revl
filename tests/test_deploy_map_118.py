"""The deploy map, and the boundary a deploy may actually cross.

Roadmap item 118, **Slice 2a**. Slice 1 built the coordinated PREPARE/COMMIT
protocol and left the caller to construct `Participant`s in Python: there was no
way to write a deploy down, and therefore no way to refuse one before things
started being spawned. This file covers the written form (`[processes.<p>.deploy]`
on the item-56 placement map), the plan-time admission of it, and the one real
boundary this slice opens.

The deploy map is **unauthenticated operator input** — it is not inside the
attested bundle and nothing signs it, yet it is the file that says which machine
runs the composition. So the whole of level 1 is refusals, and every one of them
refuses rather than downgrades.

Levels:

1. parsing and plan-time admission, with no container runtime at all: the
   machine boundary refused outright, the unknown `via` refused rather than read
   as `local`, the seam-carrying container target refused on the measured
   bind-mount fact, the network provider refused for the reason `revl swap`
   refuses to re-tier one, the missing trust store, and the all-or-nothing shape
   of the verdict;
2. against a REAL container runtime, gated on `REVL_SANDBOX_DOCKER` the same way
   item 411's rung tests are (an env gate, not a `shutil.which` probe, so a
   missing runtime in CI reds rather than silently skips): a participant really
   does run its slice inside a container and write its WAL out through the one
   read-write mount, and a container DESTROYED mid-protocol is reported
   `unresolved` — never `rolled-back` — while its WAL stays settle-able on this
   side.

Level 2 launches short-lived containers from a stock python image, all labelled
`revl.sandbox=411` by the shared flag builder and force-removed on the way out.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import deploy  # noqa: E402
from revl.recovery import recover  # noqa: E402

_DOCKER_GATE = "REVL_SANDBOX_DOCKER"

_needs_docker = pytest.mark.skipif(
    not os.environ.get(_DOCKER_GATE),
    reason=(f"{_DOCKER_GATE} is unset: a container boundary needs a working "
            f"container runtime (a `docker` CLI and a reachable daemon). The "
            f"`sandbox-container` CI job sets it; locally, start Docker and set "
            f"it to 1."))

# Pure stdlib is all the participant runner imports, so any image with a
# `python3` is a sufficient runner for it — no revl, no cordis, no third-party
# dependency, and therefore no image build.
_IMAGE = "python:3.12-slim"


def _map(**processes):
    return {"processes": processes}


# ==========================================================================
# 1. parsing and plan-time admission (no container runtime needed)
# ==========================================================================

def test_a_placement_with_no_deploy_table_is_a_valid_deploy_map():
    """The back-compat that makes a deploy map a placement map: no `[deploy]`
    anywhere means "every process is my own child"."""
    verdict = deploy.admit_deploy_map(_map(a={"components": ["A"]},
                                           b={"components": ["B"]}))
    assert verdict.ok
    assert verdict.targets == {}
    assert verdict.refusals == ()


def test_parse_reads_every_deploy_table_and_keeps_the_raw_form():
    targets, error = parse = deploy.parse_deploy_map(_map(
        db={"components": ["Db"],
            "deploy": {"via": "container", "image": "img:1", "trust": "/t"}},
        edge={"components": ["Edge"], "deploy": {"via": "local"}}))
    assert error is None and parse
    assert set(targets) == {"db", "edge"}
    assert targets["db"].boundary == deploy.BOUNDARY_CONTAINER
    assert targets["db"].crosses_boundary is True
    assert targets["edge"].boundary == deploy.BOUNDARY_PROCESS
    assert targets["edge"].crosses_boundary is False
    assert targets["db"].raw["image"] == "img:1"


def test_a_deploy_table_with_no_via_is_refused():
    _, error = deploy.parse_deploy_map(_map(db={"deploy": {"host": "h"}}))
    assert error and "gives no `via`" in error


def test_a_misspelled_deploy_key_is_refused_not_ignored():
    """An ignored key in a deploy map is an operator believing they configured
    something they did not."""
    _, error = deploy.parse_deploy_map(_map(
        db={"deploy": {"via": "ssh", "hostkey": "sha256:..."}}))
    assert error and "'hostkey'" in error and "unrecognized" in error


def test_an_unknown_via_is_refused_never_read_as_local():
    verdict = deploy.admit_deploy_map(_map(db={"deploy": {"via": "kubernetes"}}))
    assert not verdict.ok
    (refusal,) = verdict.refusals
    assert refusal["rule"] == "unknown-via"
    assert "never quietly run as a local child" in refusal["reason"]
    # and nothing is admitted: an all-or-nothing verdict, so a deploy never
    # opens some boundaries and then discovers it cannot open the rest.
    assert verdict.targets == {}


def test_a_machine_boundary_is_refused_outright_not_best_efforted():
    verdict = deploy.admit_deploy_map(_map(
        db={"deploy": {"via": "ssh", "host": "deploy@10.0.0.5",
                       "trust": "/etc/revl/trust.d", "runner": "revl"}}))
    assert not verdict.ok
    (refusal,) = verdict.refusals
    assert refusal["rule"] == "machine-boundary"
    # the refusal carries BOTH halves: the missing control plane, and the fact
    # that there would be no teardown to promise across it even with one.
    assert "pinned SSH host key" in refusal["reason"]
    assert "load-measured signed COMMIT receipt" in refusal["reason"]
    assert deploy.TEARDOWN_PROMISE[deploy.BOUNDARY_MACHINE] in refusal["reason"]


def test_a_container_target_that_carries_a_seam_is_refused():
    """The measured fact, not a policy choice: the seam is a Unix socket and a
    Unix socket does not cross a container bind mount portably. `sandbox_runtime`
    refuses the same shape."""
    verdict = deploy.admit_deploy_map(
        _map(db={"deploy": {"via": "container", "image": _IMAGE, "trust": "/t"}}),
        seams={"db": ["users"]})
    assert not verdict.ok
    (refusal,) = verdict.refusals
    assert refusal["rule"] == "container-seam"
    assert "users" in refusal["reason"]
    assert "does not cross a container bind mount" in refusal["reason"]


def test_a_container_target_with_an_unknown_seam_set_is_refused():
    """"Not told" is not "none". The conductor must PROVE the target seam-free,
    the same way `federation_admission` refuses an unknown provider instead of
    skipping it."""
    verdict = deploy.admit_deploy_map(
        _map(db={"deploy": {"via": "container", "image": _IMAGE, "trust": "/t"}}))
    assert not verdict.ok
    assert verdict.refusals[0]["rule"] == "seam-set-unknown"


def test_a_seam_free_container_target_is_admitted():
    verdict = deploy.admit_deploy_map(
        _map(db={"deploy": {"via": "container", "image": _IMAGE, "trust": "/t"}}),
        seams={"db": []})
    assert verdict.ok
    assert verdict.boundaries == {"db": deploy.BOUNDARY_CONTAINER}
    rendered = "\n".join(verdict.render())
    assert "crosses the container boundary" in rendered
    assert "teardown across the container boundary" in rendered


def test_a_network_provider_may_not_be_deployed_across_a_boundary():
    """Its address is a contract other machines already hold; moving it is the
    re-tier of a network provider that `revl swap` refuses."""
    verdict = deploy.admit_deploy_map(
        _map(db={"components": ["Db"],
                 "address": {"host": "10.0.0.5", "port": 9443},
                 "deploy": {"via": "container", "image": _IMAGE, "trust": "/t"}}),
        seams={"db": []})
    assert not verdict.ok
    (refusal,) = verdict.refusals
    assert refusal["rule"] == "network-provider"
    assert "revl swap" in refusal["reason"]


def test_a_network_provider_may_still_be_a_local_target():
    """The refusal is about crossing a boundary, not about being a provider: a
    `via = local` target crosses none, so it stays admissible."""
    verdict = deploy.admit_deploy_map(
        _map(db={"address": {"host": "10.0.0.5", "port": 9443},
                 "deploy": {"via": "local"}}),
        seams={"db": []})
    assert verdict.ok and verdict.boundaries == {"db": deploy.BOUNDARY_PROCESS}


def test_a_boundary_crossing_target_without_a_trust_store_is_refused():
    verdict = deploy.admit_deploy_map(
        _map(db={"deploy": {"via": "container", "image": _IMAGE}}),
        seams={"db": []})
    assert not verdict.ok
    (refusal,) = verdict.refusals
    assert refusal["rule"] == "no-trust-store"
    assert "a copy, not a deploy" in refusal["reason"]


def test_a_container_target_without_an_image_is_refused():
    verdict = deploy.admit_deploy_map(
        _map(db={"deploy": {"via": "container", "trust": "/t"}}),
        seams={"db": []})
    assert verdict.refusals[0]["rule"] == "container-without-image"


def test_a_local_target_carrying_remote_fields_is_refused():
    verdict = deploy.admit_deploy_map(
        _map(db={"deploy": {"via": "local", "host": "deploy@10.0.0.5"}}))
    assert verdict.refusals[0]["rule"] == "local-with-remote-fields"


def test_one_bad_target_refuses_the_whole_map():
    verdict = deploy.admit_deploy_map(
        _map(ok={"deploy": {"via": "local"}},
             bad={"deploy": {"via": "ssh", "host": "h", "trust": "/t"}}))
    assert not verdict.ok
    assert verdict.targets == {} and verdict.boundaries == {}
    assert [r["process"] for r in verdict.refusals] == ["bad"]


def test_every_via_maps_to_a_boundary_with_a_recorded_teardown_promise():
    """No `via` may acquire a guarantee nobody wrote down."""
    for via in deploy.KNOWN_VIA:
        boundary = deploy.VIA_BOUNDARY[via]
        assert deploy.teardown_promise(boundary)
    with pytest.raises(Exception):
        deploy.teardown_promise("carrier-pigeon")


# ==========================================================================
# 2. a real container boundary (gated on REVL_SANDBOX_DOCKER)
# ==========================================================================

def _spec(state_dir: Path, identity: str, effects, **extra) -> Path:
    spec = {"identity": identity,
            "world": str(state_dir / "world.json"),
            "wal": str(state_dir / f"{identity}.wal"),
            "effects": effects, **extra}
    path = state_dir / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


def _target(identity: str = "db") -> deploy.DeployTarget:
    return deploy.DeployTarget(process=identity, via=deploy.VIA_CONTAINER,
                               image=_IMAGE, trust="/etc/revl/trust.d")


def test_a_non_canonical_state_path_refuses_before_the_boundary_opens(tmp_path):
    """Mounts are identity-mapped, so a non-canonical path names a file that does
    not exist inside the container — and it would not fail until the participant
    tried to WRITE it, mid-COMMIT, where the only verdict left is `unresolved`.
    Needs no runtime: the check runs before anything is launched."""
    state = (tmp_path / "state").resolve()
    state.mkdir()
    link = tmp_path / "link"
    link.symlink_to(state)
    spec = {"identity": "db", "world": str(link / "world.json"),
            "wal": str(link / "db.wal"), "effects": []}
    path = state / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    problem = deploy._spec_paths_reachable(_target(), path, state)
    assert problem and "is not canonical" in problem


def test_a_state_path_outside_the_mounted_directory_refuses(tmp_path):
    state = (tmp_path / "state").resolve()
    state.mkdir()
    spec = {"identity": "db", "world": str((tmp_path / "world.json").resolve()),
            "wal": str((state / "db.wal").resolve()), "effects": []}
    path = state / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    problem = deploy._spec_paths_reachable(_target(), path, state)
    assert problem and "outside the mounted state directory" in problem


def test_the_launcher_refuses_a_target_that_is_not_a_container(tmp_path):
    target = deploy.DeployTarget(process="db", via=deploy.VIA_SSH)
    participant, error = deploy.launch_container_participant(
        target, spec_path=tmp_path / "x.json", state_dir=tmp_path)
    assert participant is None
    assert error and "opens container boundaries only" in error


def test_the_launcher_refuses_when_no_container_runtime_is_on_path(tmp_path):
    """A boundary that cannot be established is never downgraded to running the
    participant unconfined on the conductor's own kernel."""
    state = (tmp_path / "state").resolve()
    state.mkdir()
    spec = _spec(state, "db", [])
    participant, error = deploy.launch_container_participant(
        _target(), spec_path=spec, state_dir=state,
        docker=str(tmp_path / "no-such-docker"))
    assert participant is None
    assert error and "not usable" in error


@_needs_docker
def test_a_participant_really_applies_its_slice_inside_a_container(tmp_path):
    state = (tmp_path / "state").resolve()
    state.mkdir()
    spec = _spec(state, "db", [{"name": "row", "reversible": True},
                               {"name": "idx", "reversible": True}])
    participant, error = deploy.launch_container_participant(
        _target(), spec_path=spec, state_dir=state)
    assert error is None, error
    try:
        outcome = deploy.run_deploy([participant])
    finally:
        participant.stop()

    assert outcome["verdict"] == "applied"
    receipt = outcome["participants"]["db"]["receipt"]
    # pid 1: the participant ran in its own PID namespace, not as a child of the
    # conductor. The boundary was real.
    assert receipt["pid"] == 1
    assert receipt["applied"] == ["db:row", "db:idx"]
    # the boundary state and the WAL came back out through the one rw mount
    world = json.loads((state / "world.json").read_text(encoding="utf-8"))
    assert world == {"db:row": True, "db:idx": True}
    assert (state / "db.wal").exists()


@_needs_docker
def test_a_destroyed_container_is_unresolved_and_never_rolled_back(tmp_path):
    """The teardown promise, exercised. The accumulator and every closure-only
    inverse die with the container, so the conductor may not claim a rollback it
    did not witness — but the WAL is on a mount this side also holds, so the
    target stays settle-able."""
    state = (tmp_path / "state").resolve()
    state.mkdir()
    spec = _spec(state, "db", [{"name": "row", "reversible": True}])
    participant, error = deploy.launch_container_participant(
        _target(), spec_path=spec, state_dir=state)
    assert error is None, error
    try:
        assert participant.prepare()["ok"]
        assert participant.commit()["ok"]
        subprocess.run(["docker", "kill", participant.container],
                       capture_output=True, timeout=60, check=True)
        with pytest.raises(deploy.Unreachable) as caught:
            participant.abort()
    finally:
        participant.stop()
    assert "only its own WAL can settle it" in str(caught.value)

    # what survived: the WAL, with the inverse DESCRIPTOR (not the closure), so
    # `recovery.py` can settle the target by its own existing rule. The deploy
    # verdict never claims that settling happened.
    records = [json.loads(line) for line in
               (state / "db.wal").read_text(encoding="utf-8").splitlines() if line]
    effects = [r for r in records if r.get("record") == "effect"]
    assert effects and effects[0]["inverse"] == {"op": "remove",
                                                 "referent": "db:row"}
    world = json.loads((state / "world.json").read_text(encoding="utf-8"))
    settled = recover(str(state / "db.wal"), world=world)
    assert settled["verdict"]


@_needs_docker
def test_the_launcher_refuses_an_image_that_does_not_resolve(tmp_path):
    state = (tmp_path / "state").resolve()
    state.mkdir()
    spec = _spec(state, "db", [])
    target = deploy.DeployTarget(
        process="db", via=deploy.VIA_CONTAINER, trust="/t",
        image="revl-no-such-image-118:definitely-absent")
    participant, error = deploy.launch_container_participant(
        target, spec_path=spec, state_dir=state)
    assert participant is None
    assert error and "neither present locally nor pullable" in error
