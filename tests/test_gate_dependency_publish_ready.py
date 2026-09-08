"""`revl-as-a-dependency` is code-complete; only an owner publish remains.

Roadmap item 338 / issue #104. The item's deliverable is revl published as a
LIBRARY three ways — the py wheel (`pip install revl`,
`from revl.gate import ...`), the rust crate (`cargo add revl-gate`), and the
jco-transpiled wasm component (`npm i` the gate) — each with a real out-of-tree
consumer that admits or pre-filters agent-authored code against the packaged
surface (docs/design/338-revl-as-dependency.md; docs/gate-dependency-contract.md
"Publish status: the only remaining step").

Every part of that except the registry upload is built and gated from the
committed source in CI already, each by its own suite:

* py — `tests/test_gate_compat.py` (the `revl.gate.__all__` fence),
  `tests/test_gate_consumer_example.py` (the out-of-tree consumer against a
  packaged-shaped copy), `tests/test_packaging.py` (the wheel manifest and the
  stability metadata);
* rust — `tests/test_gate_crate_drift.py` and
  `tests/test_gate_consumer_example_rs.py`;
* wasm/js — `tests/test_gate_wasm_drift.py`, `tests/test_gate_wasm_vector.py`
  and `tests/test_gate_consumer_example_js.py`.

This file does not re-prove any of that. It pins the ONE claim none of those
suites owns on its own: that the only step left for each tier is the
irreversible, owner-run publish to its registry, and that the release path this
repository does ship refuses to take that step by itself. It is the mechanical
form of "#104 is code-complete", so the claim cannot quietly rot back into a
code gap — a removed publish gate, an example that stopped declaring revl as its
dependency, or a crate/component that stopped being built from committed source
would red here, loudly, rather than being discovered at a release.

Deliberately dependency-free (no PyYAML): the workflow assertions are made
against the file text with stable structural anchors, so this suite runs the
same in a minimal environment as it does in the full CI matrix.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
EXAMPLES = REPO / "examples"
CRATES = REPO / "crates"
DOCS = REPO / "docs"


# --------------------------------------------------------------------------- #
# py: the wheel publish path is tag-only and fail-closed, with a dry run that
# builds and installs the wheel but never uploads.
# --------------------------------------------------------------------------- #

def test_py_publish_is_tag_only_and_uploads_to_pypi():
    """`publish.yml` is the ONLY thing that puts a wheel on PyPI, it fires on a
    `v*` tag and on nothing else (never a push to a branch, so a merge to main
    can never ship a release), and the upload itself is the recommended
    Trusted-Publishing action. If any of that changes, the "only an owner tag
    publishes" guarantee changed with it."""
    wf = WORKFLOWS / "publish.yml"
    assert wf.exists(), "publish.yml (the PyPI release path) is missing"
    text = wf.read_text(encoding="utf-8")

    # Triggers on a version tag...
    assert "tags:" in text and '"v*"' in text, (
        "publish.yml must trigger on a v* tag")
    # ...and not on a branch push: a merge to main must never publish.
    assert "branches:" not in text, (
        "publish.yml must not have a branch trigger — a merge to main cannot "
        "be allowed to cut a release")
    # The tag/version sanity gate and the manifest gate both stand before the
    # upload, so a mislabeled or stray-file wheel cannot reach PyPI.
    assert "check_wheel_manifest.py" in text
    assert "does not match pyproject version" in text
    # The upload step is the real publish action, targeting the pypi env.
    assert "uses: pypa/gh-action-pypi-publish" in text
    assert "https://pypi.org/project/revl/" in text


def test_py_release_dry_run_builds_the_wheel_but_never_publishes():
    """The release dry run is what makes the py path code-complete rather than
    latent: it builds the wheel the publish job builds, installs it into a
    fresh venv and imports `revl.gate` — and it must NOT contain an upload step
    (design intent of release-dryrun.yml: a publish step that is merely
    `if:`-guarded is one edited condition away from an unintended release)."""
    wf = WORKFLOWS / "release-dryrun.yml"
    assert wf.exists(), "release-dryrun.yml is missing"
    text = wf.read_text(encoding="utf-8")

    assert "python -m build" in text
    assert "check_wheel_manifest.py" in text
    assert "pip install" in text and "dist/*.whl" in text
    # The load-bearing negative: the dry run never uploads, not even guarded.
    # (`pypa/gh-action-pypi-publish` appears only in a prose comment explaining
    # why there is no dry-run mode; what must be absent is the `uses:` step.)
    assert "uses: pypa/gh-action-pypi-publish" not in text, (
        "the release dry run must have NO publish step — building and smoking "
        "the wheel is its whole job; the real upload lives only in publish.yml")


# --------------------------------------------------------------------------- #
# rust + wasm: the artifacts a `cargo publish` / `npm publish` would ship are
# built from committed source in the repo, so only the upload is left.
# --------------------------------------------------------------------------- #

def test_rust_crate_is_committed_generated_source():
    """`cargo add revl-gate` is one `cargo publish` away because the crate is
    committed, generated source (built by `tools/build_gate_crate.py`, drift
    gated by `tests/test_gate_crate_drift.py`). The only missing thing is the
    crates.io copy."""
    cargo = CRATES / "revl-gate" / "Cargo.toml"
    assert cargo.exists(), "crates/revl-gate is missing"
    text = cargo.read_text(encoding="utf-8")
    assert 'name = "revl-gate"' in text
    assert "GENERATED by tools/build_gate_crate.py" in text
    assert (CRATES / "revl-gate" / "src" / "lib.rs").exists(), (
        "the crate's hand-written layer-1 shim (src/lib.rs) is missing")
    assert (REPO / "tools" / "build_gate_crate.py").exists()
    assert (REPO / "tests" / "test_gate_crate_drift.py").exists()


def test_wasm_component_and_js_transpile_are_built_from_source():
    """`npm i` the gate is one `npm publish` away because the wasm component is
    committed, generated source (built by `tools/build_gate_wasm.py`) and the
    JS module is transpiled from it by `tools/build_gate_js.py`, drift/import
    gated by the wasm suites. The only missing thing is the npm copy."""
    assert (CRATES / "revl-gate-wasm").is_dir(), "crates/revl-gate-wasm is missing"
    assert (REPO / "tools" / "build_gate_wasm.py").exists()
    assert (REPO / "tools" / "build_gate_js.py").exists()
    assert (REPO / "tests" / "test_gate_wasm_drift.py").exists()
    assert (REPO / "tests" / "test_gate_wasm_vector.py").exists()


# --------------------------------------------------------------------------- #
# The three out-of-tree consumers exist and each declares revl as its (only)
# dependency — the item's exit is a real external consumer per tier.
# --------------------------------------------------------------------------- #

def test_py_consumer_depends_on_the_published_wheel():
    """`examples/ecosystem-consumer/` is a standalone project whose own
    `pyproject.toml` depends on the published `revl` wheel, not on the
    checkout — the packaged-surface proof of the py exit."""
    pyproject = EXAMPLES / "ecosystem-consumer" / "pyproject.toml"
    assert pyproject.exists()
    text = pyproject.read_text(encoding="utf-8")
    assert "dependencies" in text
    assert '"revl' in text, (
        "the py consumer must declare `revl` as a dependency, so it exercises "
        "the packaged wheel rather than an in-tree import")
    assert (EXAMPLES / "ecosystem-consumer" / "ci_gate.py").exists()


def test_rust_consumer_depends_only_on_the_gate_crate():
    """`examples/ecosystem-consumer-rs/` depends solely on `revl-gate` (a path
    dependency today, `cargo add revl-gate` once published), and takes no
    revl source and no Python — the native-tier exit."""
    cargo = EXAMPLES / "ecosystem-consumer-rs" / "Cargo.toml"
    assert cargo.exists()
    text = cargo.read_text(encoding="utf-8")
    assert "revl-gate" in text
    assert (EXAMPLES / "ecosystem-consumer-rs" / "src" / "main.rs").exists()


def test_js_consumer_builds_the_gate_from_the_transpiler():
    """`examples/ecosystem-consumer-js/` builds its gate by transpiling the
    committed wasm component with `tools/build_gate_js.py` (its `npm run
    build`), so the example rides the same artifact an `npm publish` would
    ship — the wasm/js-tier exit."""
    pkg = EXAMPLES / "ecosystem-consumer-js" / "package.json"
    assert pkg.exists()
    text = pkg.read_text(encoding="utf-8")
    assert "build_gate_js.py" in text
    assert (EXAMPLES / "ecosystem-consumer-js" / "prefilter.mjs").exists()


# --------------------------------------------------------------------------- #
# The consumer-facing contract states the honest cross-registry status in one
# authoritative place, so the "only a publish remains" claim is documented
# where a consumer reads it, not only in a design note.
# --------------------------------------------------------------------------- #

def test_contract_doc_states_the_publish_only_remainder_for_all_three_registries():
    """docs/gate-dependency-contract.md must carry the single cross-tier
    statement that each dependency form is code-complete and awaiting one
    owner-run publish, naming all three registries. A consumer's tooling reads
    this doc (pyproject.toml points at it); the honest status must live here,
    not only in docs/design/."""
    doc = DOCS / "gate-dependency-contract.md"
    text = doc.read_text(encoding="utf-8")
    assert "Publish status: the only remaining step" in text
    for registry in ("PyPI", "crates.io", "npm"):
        assert registry in text, (
            f"the publish-status section must name {registry} as a registry "
            f"whose upload is the remaining owner step")
    # It points back at this test as the pin, closing the loop both ways.
    assert "test_gate_dependency_publish_ready.py" in text


if __name__ == "__main__":  # pragma: no cover - convenience only
    raise SystemExit(pytest.main([__file__, "-q"]))
