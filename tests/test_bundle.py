"""`revl bundle` / `revl verify`, the reproducible production bundle
(roadmap item 305).

These drive the REAL bundle machinery (`revl.bundle`) end to end: a real
composition is compiled through the real compiler, its source/IR/lock/policy and
per-backend emitted artifacts are written to a real bundle directory, evidence
(attestation via item 127, gauntlet via item 31) is attached, and then
`verify_bundle` recompiles the bundle and compares every tier. No mocks, the
bundle is assembled and re-checked by the same code paths the CLI runs.

The properties item 305 asks for, pinned here:

  * a bundle produced from a source VERIFIES green, source and IR hashes match,
    deps are locked, every emitted artifact corresponds to its backend,
    capabilities match policy, the attestation and gauntlet evidence check out,
    and the bundle rebuilds bit-for-bit (exit 0);
  * a tampered emitted artifact reports the EXACT backend tier as MISMATCH and
    fails the reproducible verdict (exit 1);
  * a tampered source reports the source tier as MISMATCH;
  * missing evidence (no signing key, no attestation) reports `cannot verify`,
    never a crash, and does not by itself fail the bundle;
  * a draft (open holes) is refused, it compiled but was never admitted;
  * a bundle verifies from a DIFFERENT directory than it was built in (the
    reproducibility invariant is location-independent);
  * the CLI exit codes: 0 (verified), 1 (mismatch), 2 (not a bundle).
"""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
# The backend emitters live under <root>/backends; add ROOT so the emitted-
# artifact tiers can actually emit and re-emit in this suite.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from revl import bundle as B  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.errors import RevlError  # noqa: E402


# a stable, admissible two-component composition (mirrors test_attest.BASE)
BASE = """
service Database { emission fn execute(sql: Str) -> Int }
service Cache { emission[db] fn put(key: Str, value: Str) }

component PgCache requires db: Database provides cache: Cache {
  provide cache { fn put(key, value) { emit db.execute(`INSERT ${key}`) } }
}
component Front requires cache: Cache { }
"""

# a draft: it compiles, but admission refuses it (an open hole).
DRAFT = (
    "service Cache { fn get(key: Str) -> Str }\n"
    "component C provides c: Cache {\n"
    '  provide c { fn get(key) = hole "look up in the store" }\n'
    "}\n"
)

KEY = {"REVL_ATTEST_KEY": "test-bundle-secret"}


def _write(tmp_path: Path, name: str, source: str) -> str:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return str(path)


def _tier(report, name: str):
    for c in report.checks:
        if c.tier == name:
            return c
    return None


def _tiers(report, prefix: str):
    return [c for c in report.checks if c.tier.startswith(prefix)]


@pytest.fixture
def signed_bundle(tmp_path):
    """A signed bundle of BASE, built with a resolvable signing key."""
    src = _write(tmp_path, "app.rvl", BASE)
    out = tmp_path / "app.revlbundle"
    B.build_bundle([src], str(out), env=dict(KEY))
    return out


# ------------------------------------------------------------------- green

def test_bundle_has_the_documented_layout(signed_bundle):
    """The bundle carries every part item 305 names."""
    b = signed_bundle
    assert (b / "source" / "app.rvl").exists()
    assert (b / "ir" / "ir.json").exists()
    assert (b / "ir" / "manifest.json").exists()
    assert (b / B.LOCK_NAME).exists()
    assert (b / B.POLICY_NAME).exists()
    assert (b / B.ATTESTATION_NAME).exists()
    assert (b / B.GAUNTLET_NAME).exists()
    assert (b / B.RUNTIME_MANIFEST).exists()
    # every default backend emitted an artifact
    for backend in B.DEFAULT_BACKENDS:
        assert (b / "emitted" / backend).is_dir()


def test_verify_is_green_end_to_end(signed_bundle):
    """A freshly built bundle verifies bit-for-bit: no tier is a MISMATCH."""
    report = B.verify_bundle(str(signed_bundle), env=dict(KEY))
    assert report.ok
    assert report.mismatches == []
    for name in ("source", "IR", "dependency lock", "policy surface",
                 "backend version", "attestation", "gauntlet", "reproducible"):
        assert _tier(report, name).status == B.OK, name
    # every emitted backend tier is OK too
    for c in _tiers(report, "emitted"):
        assert c.status == B.OK, c.tier


def test_verify_from_a_different_directory(signed_bundle, tmp_path):
    """The bundle is location-independent: moved somewhere else entirely, it
    still verifies green (the reproducibility invariant is not path-bound)."""
    moved = tmp_path.parent / "moved.revlbundle"
    if moved.exists():
        shutil.rmtree(moved)
    shutil.copytree(signed_bundle, moved)
    try:
        report = B.verify_bundle(str(moved), env=dict(KEY))
        assert report.ok, [c.detail for c in report.mismatches]
    finally:
        shutil.rmtree(moved)


# ------------------------------------------------------------------- tamper

def test_tampered_emitted_artifact_is_the_exact_backend_mismatch(signed_bundle):
    """Editing a committed emitted file makes exactly that backend's tier a
    MISMATCH and fails the reproducible verdict, no other tier is disturbed."""
    victim = signed_bundle / "emitted" / "python" / "components.py"
    victim.write_text(victim.read_text(encoding="utf-8") + "\n# tampered\n",
                      encoding="utf-8")
    report = B.verify_bundle(str(signed_bundle), env=dict(KEY))
    assert not report.ok
    assert _tier(report, "emitted [python]").status == B.MISMATCH
    assert _tier(report, "reproducible").status == B.MISMATCH
    # a sibling backend is untouched
    assert _tier(report, "emitted [go]").status == B.OK


def test_tampered_source_is_a_source_mismatch(signed_bundle):
    """Editing the committed source so its bytes no longer match the recorded
    hash is caught at the source tier."""
    src = signed_bundle / "source" / "app.rvl"
    src.write_text(src.read_text(encoding="utf-8") + "\n// tampered\n",
                   encoding="utf-8")
    report = B.verify_bundle(str(signed_bundle), env=dict(KEY))
    assert not report.ok
    assert _tier(report, "source").status == B.MISMATCH


def _stdlib_ref_bundle(tmp_path: Path, path: str, sha: str) -> Path:
    """A minimal bundle dir whose recorded ir/ir.json carries one stdlib-kind
    host ref, so `_check_stdlib_refs` (the item-410 verify tier) has a ref to
    re-resolve. The ref `path` and `sha256` are attacker-supplied here, standing
    in for a tampered bundle's recorded pin."""
    b = tmp_path / "reftest.revlbundle"
    (b / "ir").mkdir(parents=True)
    doc = {"externs": [{"name": "helper", "refs": {
        "ts": {"root": "stdlib", "path": path, "sha256": sha}}}]}
    (b / "ir" / "ir.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return b


def test_stdlib_ref_escaping_the_install_tree_is_refused(tmp_path):
    """A bundle-recorded stdlib ref whose `..`-bearing path resolves OUTSIDE the
    install tree is refused at verify (a MISMATCH), never re-hashed and reported
    OK against the attacker-chosen sha. This is the 410-escape twin: the verify
    tier is a second install-origin resolution site and must apply the same
    realpath-containment jail the compile-time resolver (hostref) does."""
    import hashlib as _h
    from revl._paths import stdlib_root

    install_root = Path(str(stdlib_root().parent))
    # A file the bundle never shipped, placed ABOVE the install root so no
    # legitimate stdlib ref could ever point at it, pinned to its real sha.
    outside = tmp_path / "outside_secret.txt"
    outside.write_bytes(b"a file outside the install tree\n")
    outside_sha = _h.sha256(outside.read_bytes()).hexdigest()
    up = os.path.relpath(str(outside), str(install_root))  # ..-bearing escape
    assert up.startswith("..")

    b = _stdlib_ref_bundle(tmp_path, up, outside_sha)
    checks = B._check_stdlib_refs(b)
    assert checks, "the crafted bundle must carry a stdlib ref to check"
    statuses = {c.status for c in checks}
    # The escape must be caught: refused as MISMATCH, and NEVER reported OK.
    assert B.MISMATCH in statuses
    assert B.OK not in statuses
    esc = next(c for c in checks if c.status == B.MISMATCH)
    assert "escapes the install tree" in esc.detail


def test_stdlib_ref_absolute_path_is_refused(tmp_path):
    """An absolute recorded ref path is refused before any read, independent of
    what the join semantics would do with it."""
    b = _stdlib_ref_bundle(tmp_path, "/etc/hostname", "0" * 64)
    checks = B._check_stdlib_refs(b)
    assert checks and all(c.status == B.MISMATCH for c in checks)
    assert "absolute" in checks[0].detail


def test_legit_in_tree_stdlib_ref_still_verifies_ok(tmp_path):
    """The happy path is untouched: a stdlib ref whose path resolves INSIDE the
    install tree, pinned to the real bytes, verifies OK."""
    import hashlib as _h
    from revl._paths import stdlib_root

    install_root = Path(str(stdlib_root().parent))
    legit = install_root / "stdlib" / "fs.rvl"
    sha = _h.sha256(legit.read_bytes()).hexdigest()
    b = _stdlib_ref_bundle(tmp_path, "stdlib/fs.rvl", sha)
    checks = B._check_stdlib_refs(b)
    assert checks and all(c.status == B.OK for c in checks)


def test_tampered_policy_is_a_policy_mismatch(signed_bundle):
    """A policy.json that no longer matches the rebuilt capability surface is a
    policy MISMATCH, a consumer sees the capability set was edited."""
    policy = signed_bundle / B.POLICY_NAME
    doc = json.loads(policy.read_text(encoding="utf-8"))
    doc["capabilities"] = ["network"]  # not what the IR actually reaches
    policy.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    report = B.verify_bundle(str(signed_bundle), env=dict(KEY))
    assert not report.ok
    assert _tier(report, "policy surface").status == B.MISMATCH


def test_tampered_attestation_hash_is_an_attestation_mismatch(signed_bundle):
    """An attestation whose embedded composition hash was edited fails the
    attestation tier (the signature no longer matches)."""
    att = signed_bundle / B.ATTESTATION_NAME
    doc = json.loads(att.read_text(encoding="utf-8"))
    doc["composition_hash"] = "0" * 64
    att.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    report = B.verify_bundle(str(signed_bundle), env=dict(KEY))
    assert not report.ok
    assert _tier(report, "attestation").status == B.MISMATCH


# ------------------------------------------------------- honest degradation

def test_no_key_means_no_attestation_and_cannot_verify(tmp_path):
    """Built without a signing key, the bundle carries no attestation.json and
    the attestation tier degrades to `cannot verify`, never a false OK, never a
    crash, and the bundle still verifies green overall (exit 0)."""
    src = _write(tmp_path, "app.rvl", BASE)
    out = tmp_path / "nokey.revlbundle"
    B.build_bundle([src], str(out), env={})  # empty env -> no key resolvable
    assert not (out / B.ATTESTATION_NAME).exists()
    report = B.verify_bundle(str(out), env={})
    assert _tier(report, "attestation").status == B.UNVERIFIED
    assert report.ok  # an unverifiable tier does not fail the bundle


def test_missing_attestation_file_does_not_crash(signed_bundle):
    """A bundle whose attestation.json was deleted verifies without crashing;
    the attestation tier is `cannot verify`, the rest still OK."""
    (signed_bundle / B.ATTESTATION_NAME).unlink()
    report = B.verify_bundle(str(signed_bundle), env=dict(KEY))
    assert _tier(report, "attestation").status == B.UNVERIFIED
    # removing evidence does not turn a reproducible bundle red
    assert _tier(report, "reproducible").status == B.OK


def test_missing_gauntlet_is_cannot_verify(signed_bundle):
    """A bundle whose gauntlet.json is absent reports the gauntlet tier as
    `cannot verify`, not a crash."""
    (signed_bundle / B.GAUNTLET_NAME).unlink()
    report = B.verify_bundle(str(signed_bundle), env=dict(KEY))
    assert _tier(report, "gauntlet").status == B.UNVERIFIED


# ------------------------------------------------------------------- refuse

def test_bundling_a_draft_is_refused(tmp_path):
    """A draft (open holes) is not admitted, so it cannot be bundled, a bundle
    is a production artifact, not a work in progress."""
    src = _write(tmp_path, "draft.rvl", DRAFT)
    out = tmp_path / "draft.revlbundle"
    with pytest.raises(RevlError) as exc:
        B.build_bundle([src], str(out), env=dict(KEY))
    assert "hole" in str(exc.value)


# ------------------------------------------------------------- backend / topology

def test_backend_selection_narrows_the_emitted_set(tmp_path):
    """`--backend` restricts which backends are emitted; only the selected ones
    appear and only they are verified."""
    src = _write(tmp_path, "app.rvl", BASE)
    out = tmp_path / "one.revlbundle"
    B.build_bundle([src], str(out), backends=("python",), env=dict(KEY))
    emitted = {p.name for p in (out / "emitted").iterdir()}
    assert emitted == {"python"}
    report = B.verify_bundle(str(out), env=dict(KEY))
    assert report.ok
    assert len(_tiers(report, "emitted")) == 1


def test_topology_is_carried_and_verified(tmp_path):
    """A supplied placement map is carried as topology.json and the topology tier
    reports OK; a bundle without one reports `cannot verify`."""
    src = _write(tmp_path, "app.rvl", BASE)
    topo = tmp_path / "placement.json"
    topo.write_text(json.dumps({"PgCache": {"process": "db"},
                                "Front": {"process": "web"}}),
                    encoding="utf-8")
    out = tmp_path / "topo.revlbundle"
    B.build_bundle([src], str(out), topology=str(topo), env=dict(KEY))
    assert (out / B.TOPOLOGY_NAME).exists()
    report = B.verify_bundle(str(out), env=dict(KEY))
    assert _tier(report, "topology").status == B.OK

    # a bundle without a topology reports cannot-verify for that tier
    out2 = tmp_path / "notopo.revlbundle"
    B.build_bundle([src], str(out2), env=dict(KEY))
    report2 = B.verify_bundle(str(out2), env=dict(KEY))
    assert _tier(report2, "topology").status == B.UNVERIFIED


# ------------------------------------------------------- one-file bundle

def _tree(root: Path) -> dict:
    """Every file under `root`, keyed by its POSIX relative path, valued by its
    verbatim bytes, so two bundle trees can be compared byte-for-byte."""
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_one_file_round_trips_byte_for_byte(signed_bundle, tmp_path):
    """Packing a `.revlbundle/` directory into one file and unpacking it back
    reproduces the whole tree byte-for-byte: same set of files, identical bytes.
    Nothing is re-derived, so every recorded hash travels verbatim."""
    packed = tmp_path / ("app" + B.ONEFILE_SUFFIX)
    B.pack_bundle(str(signed_bundle), str(packed))
    assert packed.is_file()

    restored = tmp_path / "restored.revlbundle"
    B.unpack_bundle(str(packed), str(restored))

    before, after = _tree(Path(signed_bundle)), _tree(restored)
    assert before.keys() == after.keys(), "the one-file bundle lost or added files"
    for name in before:
        assert before[name] == after[name], f"{name} differs after a round-trip"


def test_one_file_verifies_equivalently(signed_bundle, tmp_path):
    """A one-file bundle verifies exactly as the directory it was packed from:
    green, and every tier reports the same status. `verify` accepts the file
    directly (it expands it into a throwaway tree under the hood)."""
    packed = tmp_path / ("app" + B.ONEFILE_SUFFIX)
    B.pack_bundle(str(signed_bundle), str(packed))

    dir_report = B.verify_bundle(str(signed_bundle), env=dict(KEY))
    file_report = B.verify_bundle(str(packed), env=dict(KEY))

    assert file_report.ok
    assert {(c.tier, c.status) for c in file_report.checks} == \
           {(c.tier, c.status) for c in dir_report.checks}


def test_pack_is_deterministic(signed_bundle, tmp_path):
    """The same directory packs to identical bytes every time (no timestamps,
    sorted keys), so a one-file bundle is itself reproducible."""
    a = tmp_path / ("a" + B.ONEFILE_SUFFIX)
    b = tmp_path / ("b" + B.ONEFILE_SUFFIX)
    B.pack_bundle(str(signed_bundle), str(a))
    B.pack_bundle(str(signed_bundle), str(b))
    assert a.read_bytes() == b.read_bytes()


def test_tampering_a_one_file_bundle_is_caught_on_verify(signed_bundle, tmp_path):
    """Editing an emitted artifact inside a packed one-file bundle is caught at
    exactly that backend's tier, the same way it is in a directory (the bytes are
    what verify recompiles against)."""
    packed = tmp_path / ("app" + B.ONEFILE_SUFFIX)
    B.pack_bundle(str(signed_bundle), str(packed))
    doc = json.loads(packed.read_text(encoding="utf-8"))
    doc["files"]["emitted/python/components.py"] += "\n# tampered\n"
    packed.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    report = B.verify_bundle(str(packed), env=dict(KEY))
    assert not report.ok
    assert _tier(report, "emitted [python]").status == B.MISMATCH


def test_unpack_refuses_a_non_envelope(tmp_path):
    """A JSON document that is not a one-file envelope is refused, never silently
    treated as an empty bundle."""
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"kind": "something.else"}), encoding="utf-8")
    with pytest.raises(RevlError):
        B.unpack_bundle(str(bad), str(tmp_path / "out"))


def test_unpack_jails_a_traversing_entry(tmp_path):
    """A forged one-file bundle whose embedded path escapes the bundle root (an
    absolute path or a `..`-bearing one) is refused before it can write outside
    the extraction directory."""
    for rel in ("/etc/pwned", "../escaped.txt"):
        forged = tmp_path / "forged.json"
        forged.write_text(json.dumps(
            {"kind": B.ONEFILE_KIND, "version": B.ONEFILE_VERSION,
             "files": {rel: "x"}}), encoding="utf-8")
        with pytest.raises(RevlError):
            B.unpack_bundle(str(forged), str(tmp_path / "out"))


def test_is_onefile_discriminates(signed_bundle, tmp_path):
    """`is_onefile` is True only for a one-file envelope: a directory bundle, a
    plain JSON file, and a missing path are all False."""
    packed = tmp_path / ("app" + B.ONEFILE_SUFFIX)
    B.pack_bundle(str(signed_bundle), str(packed))
    assert B.is_onefile(str(packed))
    assert not B.is_onefile(str(signed_bundle))          # the directory
    assert not B.is_onefile(str(signed_bundle / B.RUNTIME_MANIFEST))  # inner json
    assert not B.is_onefile(str(tmp_path / "nope"))       # missing


def test_cli_one_file_flag_writes_and_verifies(tmp_path, monkeypatch, capsys):
    """`revl bundle --one-file FILE` writes the packed file beside the directory,
    and `revl verify FILE` exits 0 just like verifying the directory."""
    monkeypatch.setenv("REVL_ATTEST_KEY", KEY["REVL_ATTEST_KEY"])
    src = _write(tmp_path, "app.rvl", BASE)
    out = tmp_path / "cli.revlbundle"
    packed = tmp_path / ("cli" + B.ONEFILE_SUFFIX)
    assert main(["bundle", src, "--out", str(out), "--one-file", str(packed)]) == 0
    assert packed.is_file()
    assert main(["verify", str(packed)]) == 0


# ------------------------------------------------------------------- CLI exit

def test_cli_exit_codes(tmp_path, monkeypatch, capsys):
    """The CLI contract: bundle exits 0; verify exits 0 on a clean bundle, 1 on
    a tampered one, 2 on something that is not a bundle."""
    monkeypatch.setenv("REVL_ATTEST_KEY", KEY["REVL_ATTEST_KEY"])
    src = _write(tmp_path, "app.rvl", BASE)
    out = tmp_path / "cli.revlbundle"

    assert main(["bundle", src, "--out", str(out)]) == 0
    assert main(["verify", str(out)]) == 0

    victim = out / "emitted" / "python" / "components.py"
    victim.write_text(victim.read_text(encoding="utf-8") + "\n# x\n",
                      encoding="utf-8")
    assert main(["verify", str(out)]) == 1

    assert main(["verify", str(tmp_path / "nope")]) == 2


def test_cli_json_report_shape(tmp_path, monkeypatch, capsys):
    """`revl verify --json` emits a machine-readable tier-by-tier report."""
    monkeypatch.setenv("REVL_ATTEST_KEY", KEY["REVL_ATTEST_KEY"])
    src = _write(tmp_path, "app.rvl", BASE)
    out = tmp_path / "json.revlbundle"
    main(["bundle", src, "--out", str(out)])
    capsys.readouterr()
    assert main(["verify", str(out), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["verified"] is True
    assert {"tier", "status", "detail"} <= set(doc["checks"][0])
