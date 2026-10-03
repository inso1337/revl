"""Multi-file artifacts on a private pool: one bundle digest over every file,
its path and its mode (issue #1198; `src/revl/pool_bundle.py`).

What is under test, in the order it matters.

1. **A THREE-FILE COMPOSITION RUNS REMOTELY, THROUGH THE CLI.** The pool is
   stood up with `revl pool init`, the member joins with `pool request` /
   `pool join`, both pinning the digest `revl pool digest` prints, and the peer
   runs `revl pool serve` as its own OS process. `revl run --pool private` is
   handed three files, one of which imports the other two. The result comes
   back, the signed receipt names the bundle digest and every file, and
   `revl pool ledger` records the same.

   **Non-vacuity.** Before this change `run --pool private` refused any second
   file on `multi-file-artifact`, and `pool digest` did not exist.

2. **A TAMPERED FILE IS REFUSED**, on both ends. The operator refuses to send
   a bundle whose digest the member was not admitted with. The peer, handed a
   task the operator signed whose file bytes disagree with its manifest,
   refuses it and names the file. Missing and extra files are refused by name
   the same way. Nothing is written to the peer's workspace for any of them.

3. **A PATH-TRAVERSAL NAME IS REFUSED**, on both ends. `../x.rvl` on the
   operator's command line is refused before anything is recorded, and a
   signed task naming `../x` is refused by the peer before anything is
   written, so no file appears outside its workspace.

What these tests do NOT reach: two machines. The peer is a second OS process
on loopback, as in `test_pool_health_1198.py` and `test_pool_dispatch_524.py`.
"""

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import peer_identity as pi  # noqa: E402
from revl import peer_pool as pp  # noqa: E402
from revl import pool_bundle as pb  # noqa: E402
from revl import pool_dispatch as pd  # noqa: E402
from revl.lawful_retry import EffectClass  # noqa: E402

OP_KEY = b"pool-operator-key-1198-bundle"
CEILING = "compute()"

#: Three files, boundary-free, one importing the other two. Every `.rvl` file
#: is a root, as it is for a local `revl test a.rvl b.rvl`.
COMPOSITION = {
    "main.rvl": b'''use "./lib/math.rvl" { add }
use "./lib/twice.rvl" { twice }

test "add and twice compose across files" {
  assert twice(add(2, 3)) == 10
}
''',
    "lib/math.rvl": b'''pub fn add(a: Int, b: Int) -> Int {
  return a + b
}
''',
    "lib/twice.rvl": b'''pub fn twice(n: Int) -> Int {
  return n * 2
}

test "twice doubles" {
  assert twice(4) == 8
}
''',
}
FILES = ["main.rvl", "lib/math.rvl", "lib/twice.rvl"]


# ---------------------------------------------------------------------------
# the CLI harness
# ---------------------------------------------------------------------------


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    return env


def revl(*argv: str, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "revl", *argv],
                          capture_output=True, text=True, env=_env(),
                          cwd=cwd, timeout=300)


def ok(*argv: str, cwd=None) -> subprocess.CompletedProcess:
    done = revl(*argv, cwd=cwd)
    assert done.returncode == 0, (argv, done.stdout, done.stderr)
    return done


class Pool:
    """A pool whose one member was admitted for the bundle of ``COMPOSITION``,
    made only through the CLI."""

    def __init__(self, base: Path, composition=None):
        composition = composition or COMPOSITION
        self.base = base
        self.dir = base / "pool"
        self.keys = base / "keys"
        self.comp = base / "comp"
        self.keys.mkdir(parents=True)
        for name, data in composition.items():
            (self.comp / name).parent.mkdir(parents=True, exist_ok=True)
            (self.comp / name).write_bytes(data)
        (self.keys / "operator.secret").write_bytes(OP_KEY)
        for name in ("operator", "attestor", "alpha"):
            ok("pool", "keygen", "--peer-id", name,
               "--out", str(self.key(name)), "--public", str(self.pub(name)))
        self.digest = ok("pool", "digest", *composition,
                         cwd=self.comp).stdout.splitlines()[0].strip()
        ok("pool", "init", "--dir", str(self.dir), "--pool-id", "lab",
           "--ceiling", CEILING, "--entry-caps", CEILING,
           "--artifact", self.digest, "--key", str(self.secret),
           "--attest-identity", str(self.pub("attestor")))
        join = base / "alpha.join.json"
        ok("pool", "register", "--dir", str(self.dir),
           "--public", str(self.pub("alpha")))
        ok("pool", "request", "--charter", str(self.dir / pp.CHARTER_FILE),
           "--peer-id", "alpha", "--artifact", self.digest,
           "--ceiling", CEILING, "--identity-key", str(self.key("alpha")),
           "--out", str(join))
        ok("pool", "join", "--dir", str(self.dir), "--join", str(join),
           "--key", str(self.secret))

    @property
    def secret(self) -> Path:
        return self.keys / "operator.secret"

    def key(self, name: str) -> Path:
        return self.keys / f"{name}.key"

    def pub(self, name: str) -> Path:
        return self.keys / f"{name}.pub"

    def run(self, addr: str, *files: str,
            runner: str = "test-py") -> subprocess.CompletedProcess:
        return revl("run", "--pool", "private", "--pool-dir", str(self.dir),
                    "--peer", "alpha", "--peer-addr", addr,
                    "--dispatch-identity", str(self.key("operator")),
                    "--attest-identity", str(self.key("attestor")),
                    "--pool-runner", runner, "--pool-timeout", "120",
                    *files, cwd=self.comp)

    def ledger(self) -> dict:
        return json.loads(ok("pool", "ledger", "--dir", str(self.dir),
                             "--json").stdout)


class Serving:
    """`revl pool serve` as its own process, with a workspace the test can
    inspect."""

    def __init__(self, pool: Pool):
        self.workdir = pool.base / "peer" / "work"
        self.workdir.mkdir(parents=True)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "revl", "pool", "serve",
             "--charter", str(pool.dir / pp.CHARTER_FILE),
             "--identity-key", str(pool.key("alpha")),
             "--operator-public", str(pool.pub("operator")),
             "--workdir", str(self.workdir), "--port", "0",
             "--timeout", "120"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=_env())
        line = self.process.stdout.readline()
        assert "listening on" in line, (line, self.process.stderr.read())
        self.addr = line.split("listening on ", 1)[1].split()[0]
        self.host, _, port = self.addr.rpartition(":")
        self.port = int(port)

    def send(self, record: dict) -> dict:
        return pd.send_task(record, host=self.host, port=self.port,
                            timeout=120)

    def line(self) -> str:
        return self.process.stdout.readline()

    def stop(self) -> None:
        self.process.kill()
        self.process.wait(timeout=30)
        self.process.stdout.close()
        self.process.stderr.close()


@pytest.fixture
def pool(tmp_path):
    return Pool(tmp_path)


@pytest.fixture
def serving(pool):
    peer = Serving(pool)
    yield peer
    peer.stop()


def _bundle() -> pb.Bundle:
    return pb.Bundle(tuple(sorted(
        (pb.BundleFile(name, pb.MODE_FILE, data)
         for name, data in COMPOSITION.items()), key=lambda f: f.path)))


def signed_task(pool: Pool, wire: dict, digest: str, task_id: str) -> dict:
    """A task the OPERATOR signed, carrying ``wire`` as its bundle. The
    signature is real, so only the bundle checks can refuse it."""
    charter = json.loads((pool.dir / pp.CHARTER_FILE).read_text())
    body = pd.build_task(
        pool_id=charter["pool_id"],
        charter_digest=pp.canonical_digest(charter), peer_id="alpha",
        task_id=task_id, artifact=b"", runner=pd.RUNNER_TEST_PY,
        effect_class=EffectClass.PURE)
    body.update(artifact="", artifact_digest=digest, bundle=wire)
    return pd.sign_task(body, pi.load_private_identity(pool.key("operator")))


# ---------------------------------------------------------------------------
# 1. a three-file composition runs remotely
# ---------------------------------------------------------------------------


def test_a_three_file_composition_runs_remotely(pool, serving):
    assert pool.digest == _bundle().digest()

    done = pool.run(serving.addr, *FILES)
    assert done.returncode == 0, (done.stdout, done.stderr)
    outcome = json.loads(done.stdout)
    assert outcome["ok"] is True
    assert outcome["artifact_digest"] == pool.digest
    assert outcome["result"]["exit_code"] == 0, outcome["result"]
    assert any("2 test(s) passed" in line
               for line in outcome["result"]["stdout"]), outcome["result"]
    assert "ran task" in serving.line()

    # The receipt the PEER signed names the bundle digest and every file.
    receipt = outcome["receipt"]
    assert receipt["artifact_digest"] == pool.digest
    assert receipt["bundle"]["digest"] == pool.digest
    assert [e["path"] for e in receipt["bundle"]["files"]] == sorted(FILES)
    assert all(e["mode"] == pb.MODE_FILE for e in receipt["bundle"]["files"])
    assert outcome["bundle"] == receipt["bundle"]

    # The ledger records it.
    ledger = pool.ledger()
    (entry,) = ledger["tasks"].values()
    assert entry["state"] == pd.STATE_DELIVERED
    assert entry["artifact_digest"] == pool.digest
    assert entry["bundle"]["digest"] == pool.digest
    assert [e["path"] for e in entry["bundle"]["files"]] == sorted(FILES)
    assert entry["receipt"]["bundle"]["digest"] == pool.digest
    text = ok("pool", "ledger", "--dir", str(pool.dir)).stdout
    assert f"bundle={pool.digest[:16]} (3 files)" in text


def test_the_bundle_digest_covers_content_path_and_mode_not_order(pool):
    """`pool digest` and `run --pool private` read the command line the same
    way, so this pins what the charter pins."""
    def digest(*files):
        return ok("pool", "digest", *files,
                  cwd=pool.comp).stdout.splitlines()[0].strip()

    assert digest(*reversed(FILES)) == pool.digest
    assert digest("./main.rvl", "lib/../lib/math.rvl",
                  "lib/twice.rvl") == pool.digest

    (pool.comp / "lib" / "twice.rvl").chmod(0o755)
    assert digest(*FILES) != pool.digest
    (pool.comp / "lib" / "twice.rvl").chmod(0o644)
    assert digest(*FILES) == pool.digest

    (pool.comp / "sub").mkdir()
    (pool.comp / "lib" / "math.rvl").rename(pool.comp / "sub" / "math.rvl")
    assert digest("main.rvl", "sub/math.rvl", "lib/twice.rvl") != pool.digest


def _has_cordis() -> bool:
    import importlib.util

    return importlib.util.find_spec("cordis") is not None


@pytest.mark.skipif(not _has_cordis(),
                    reason="the `run-once-py` runner boots a composition, "
                           "which needs a cordis-py runtime ON THE PEER; "
                           "skipped rather than weakened without one")
def test_a_composition_split_across_files_boots_on_the_peer(tmp_path):
    """The other runner: the service in one file, the components in another,
    booted and torn down on the peer."""
    split = {
        "counter.rvl": b"""service Counter {
  fn next() -> Int
}
""",
        "app.rvl": b"""component CounterSvc provides counter: Counter {
  provide counter {
    fn next() = 42
  }
}

component CounterUser requires counter: Counter {
}
""",
    }
    pool = Pool(tmp_path, composition=split)
    peer = Serving(pool)
    try:
        done = pool.run(peer.addr, "app.rvl", "counter.rvl",
                        runner="run-once-py")
    finally:
        peer.stop()
    assert done.returncode == 0, (done.stdout, done.stderr)
    outcome = json.loads(done.stdout)
    assert outcome["result"]["exit_code"] == 0, outcome["result"]
    assert any("no residue" in line for line in outcome["result"]["stdout"])
    assert outcome["receipt"]["bundle"]["digest"] == pool.digest


# ---------------------------------------------------------------------------
# 2. a tampered file is refused
# ---------------------------------------------------------------------------


def test_a_tampered_file_is_refused_by_the_operator_before_it_is_sent(
        pool, serving):
    (pool.comp / "lib" / "math.rvl").write_bytes(
        COMPOSITION["lib/math.rvl"].replace(b"a + b", b"a - b"))
    done = pool.run(serving.addr, *FILES)
    assert done.returncode == 1, (done.stdout, done.stderr)
    refusal = json.loads(done.stdout)
    assert refusal["link"] == pd.LINK_ARTIFACT_DIGEST
    assert refusal["bundle"]["digest"] != pool.digest
    assert pool.ledger()["tasks"] == {}
    assert list(serving.workdir.iterdir()) == []


def test_a_tampered_file_is_refused_by_the_peer_by_name(pool, serving):
    wire = _bundle().to_wire()
    for item in wire["files"]:
        if item["path"] == "lib/math.rvl":
            item["content"] = base64.b64encode(
                COMPOSITION["lib/math.rvl"].replace(b"a + b", b"a - b")
            ).decode()
    answer = serving.send(signed_task(pool, wire, pool.digest, "t-tamper"))
    assert answer["ok"] is False
    assert answer["link"] == pd.LINK_BUNDLE_FILE_DIGEST, answer
    assert answer["paths"] == ["lib/math.rvl"]
    assert "refused task t-tamper on bundle-file-digest" in serving.line()
    assert list(serving.workdir.iterdir()) == []

    # A mode is part of what the manifest pins.
    wire = _bundle().to_wire()
    wire["files"][0]["mode"] = pb.MODE_EXEC
    answer = serving.send(signed_task(pool, wire, pool.digest, "t-mode"))
    assert (answer["link"], answer["paths"]) == (
        pd.LINK_BUNDLE_FILE_DIGEST, [wire["files"][0]["path"]])


def test_a_missing_or_extra_file_is_refused_by_the_peer_by_name(
        pool, serving):
    wire = _bundle().to_wire()
    wire["files"] = [f for f in wire["files"] if f["path"] != "lib/twice.rvl"]
    answer = serving.send(signed_task(pool, wire, pool.digest, "t-missing"))
    assert (answer["link"], answer["paths"]) == (
        pd.LINK_BUNDLE_MISSING_FILE, ["lib/twice.rvl"]), answer

    wire = _bundle().to_wire()
    wire["files"].append({"path": "lib/extra.rvl", "mode": pb.MODE_FILE,
                          "content": base64.b64encode(b"").decode()})
    answer = serving.send(signed_task(pool, wire, pool.digest, "t-extra"))
    assert (answer["link"], answer["paths"]) == (
        pd.LINK_BUNDLE_EXTRA_FILE, ["lib/extra.rvl"]), answer

    # A manifest edited to match is a different bundle, and the digest the
    # task pins says so before any file is looked at.
    wire = _bundle().to_wire()
    wire["manifest"] = wire["manifest"][:2]
    answer = serving.send(signed_task(pool, wire, pool.digest, "t-manifest"))
    assert answer["link"] == pd.LINK_ARTIFACT_DIGEST, answer
    assert list(serving.workdir.iterdir()) == []


# ---------------------------------------------------------------------------
# 3. a path-traversal name is refused
# ---------------------------------------------------------------------------


def test_a_traversal_name_is_refused_by_the_operator(pool, serving):
    (pool.base / "x.rvl").write_bytes(COMPOSITION["lib/math.rvl"])
    done = pool.run(serving.addr, "main.rvl", "../x.rvl")
    assert done.returncode == 1, (done.stdout, done.stderr)
    refusal = json.loads(done.stdout)
    assert refusal["link"] == pd.LINK_BUNDLE_PATH
    assert refusal["paths"] == ["../x.rvl"]
    assert pool.ledger()["tasks"] == {}

    digest = revl("pool", "digest", "main.rvl", "../x.rvl", cwd=pool.comp)
    assert digest.returncode == 1
    assert "../x.rvl" in digest.stderr


def test_a_traversal_name_is_refused_by_the_peer_before_anything_is_written(
        pool, serving):
    escaped = serving.workdir.parent / "x"
    for name in ("../x", "lib/../../x", "/tmp/x", "-x.rvl"):
        wire = _bundle().to_wire()
        wire["files"].append({"path": name, "mode": pb.MODE_FILE,
                              "content": base64.b64encode(b"owned").decode()})
        wire["manifest"].append({"path": name, "mode": pb.MODE_FILE,
                                 "digest": pb.file_digest(b"owned")})
        task_id = f"t-path-{len(name)}"
        answer = serving.send(signed_task(pool, wire, pool.digest, task_id))
        assert (answer["link"], answer["paths"]) == (
            pd.LINK_BUNDLE_PATH, [name]), answer
    assert not escaped.exists()
    assert list(serving.workdir.iterdir()) == []


def test_names_that_collide_on_a_case_insensitive_disk_are_refused():
    problem = pb.manifest_problem([
        {"path": "Main.rvl", "mode": pb.MODE_FILE, "digest": "0" * 64},
        {"path": "main.rvl", "mode": pb.MODE_FILE, "digest": "0" * 64}])
    assert problem.kind == pb.PROBLEM_PATH
    problem = pb.manifest_problem([
        {"path": "lib", "mode": pb.MODE_FILE, "digest": "0" * 64},
        {"path": "lib/a.rvl", "mode": pb.MODE_FILE, "digest": "0" * 64}])
    assert problem.kind == pb.PROBLEM_PATH


def test_a_receipt_whose_bundle_block_disagrees_with_its_artifact_is_refused():
    from revl import pool_receipt as pr

    worker = pi.identity_from_seed("alpha", b"seed/alpha/1198-bundle")
    bundle = _bundle()
    other = pb.Bundle(bundle.files[:2])
    receipt = pr.issue_receipt(pool_id="lab", task_id="t-1",
                               artifact_digest=bundle.digest(),
                               result={"exit_code": 0}, identity=worker,
                               bundle=other.describe())
    attestor = pi.identity_from_seed("attestor", b"seed/attestor/1198-bundle")
    directory = pi.IdentityDirectory()
    directory.register(worker.public())
    directory.register(attestor.public())
    check = pr.check_receipt(
        receipt, pr.attest_receipt(receipt, identity=attestor),
        pool_id="lab", peer_id="alpha", artifact_digest=bundle.digest(),
        admitted_at="2000-01-01T00:00:00Z",
        attest_key_ids=(attestor.key_id,), directory=directory)
    assert check.link == pr.LINK_RECEIPT_ARTIFACT, check.as_dict()
