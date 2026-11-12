"""AGENTIC-DRIVERS-1: the six agentic benchmark drivers are traceable (issue #2621).

The drivers were written in scratch directories and copied between them, so the
same docstring ended up over three different capped variants and no published
number could be traced back to a file. These tests hold the tracked copies to
the properties that make them traceable, not to the numbers they produce:

  * every driver under `bench/agentic/` is named in the manifest, and every
    manifest entry is a driver in that tree;
  * each tracked file still hashes to its recorded `tracked_sha256`;
  * the tracked copy, with its module docstring token removed, hashes to its
    recorded `tracked_body_sha256`; `source_body_sha256` is a recorded value
    that no test recomputes, because the sources are not in the repository
    (the only check on it is that an `edit: none` entry records it equal to the
    tracked body sha); the recorded edits are the docstring identity lines,
    explicit UTF-8 encodings and the machine-path removals, which do not change
    what a driver sends to a model;
  * the tracked body hashes are pinned here, outside the manifest, so a body
    edit fails even if the manifest is refreshed with it;
  * each driver's docstring opens with its own filename, the identity a copied
    variant used to lose;
  * an `edit: none` entry records the tracked hash as equal to the source hash;
  * no driver carries a machine path as a default: the repository root is
    derived from the driver's own location and `--arm-src` is required;
  * no driver carries a long dash in a comment or docstring;
  * the manifest says, per driver, whether its source could be re-read, and
    records the size and modification time only for a source that was.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTIC = ROOT / "bench" / "agentic"
MANIFEST = AGENTIC / "manifest.json"

EXPECTED = {
    "bench_agentic.py",
    "bench_agentic2.py",
    "bench_agentic2_cap.py",
    "bench_agentic2_cap6.py",
    "bench_agentic2_cap2.py",
    "bench_agentic3.py",
}

# The record's trust anchor. These are the tracked body hashes, kept out of
# manifest.json so that refreshing the manifest is not enough to change one:
# a body edit fails here until the test itself is edited and reviewed.
FROZEN_BODY_SHA256 = {
    "bench_agentic.py": "b92418c364fd363f81ce21c2f2dd5e28f7517747274dc83f3329e1df6d15937c",
    "bench_agentic2.py": "cb80dbf16d0d86376c259e2bc5ed293010f61d751779ca2cf2962059274e88e1",
    "bench_agentic2_cap.py": "c0e987ec4d9c721e4283dd4ac224ceeb7b01aa90a99f76f8120aea4f8417d034",
    "bench_agentic2_cap6.py": "f92d3ddc2b42b4b5980d649c063d1feb38a2c8d5c0a189468a39cbf0c9ec1d5a",
    "bench_agentic2_cap2.py": "66ba30240f0738926ffc7eb72e660598300a548d29bf92976ce25e3a9ad37cc0",
    "bench_agentic3.py": "1fd459ae5bd894dbee0ebcb7abe7fa9ab9753b2e903ce57e8cfd357be18b0f3d",
}


def _variants():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["variants"]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _body_sha(text: str) -> str:
    """sha256 of the source with the module docstring token removed.

    The tokenizer, not the AST, because an AST dump's text differs between
    Python versions: CI runs 3.12 and a contributor may run 3.14, and a body
    hash that changes with the interpreter would red the suite for no defect.
    """
    start = end = None
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.STRING:
            start, end = tok.start, tok.end
            break
        if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.ENCODING):
            continue
        break
    assert start is not None, "no module docstring"
    lines = text.splitlines(keepends=True)

    def _offset(pos: tuple[int, int]) -> int:
        row, col = pos
        return sum(len(line) for line in lines[: row - 1]) + col

    body = text[: _offset(start)] + text[_offset(end) :]
    return _sha(body.encode())


def _first_docstring_line(text: str) -> str:
    doc = ast.get_docstring(ast.parse(text)) or ""
    return doc.splitlines()[0]


def test_manifest_lists_the_six_variants():
    variants = _variants()
    names = [v["name"] for v in variants]
    assert len(names) == len(set(names)) == 6, names
    assert set(names) == EXPECTED, set(names) ^ EXPECTED
    assert {v["generation"] for v in variants} == {1, 2, 3}
    for variant in variants:
        assert variant["predecessor"] is None or variant["predecessor"] in set(names), variant["name"]


def test_every_driver_is_in_the_manifest_and_every_entry_is_a_driver():
    tracked = {str(p.relative_to(ROOT)) for p in AGENTIC.rglob("*.py")}
    listed = {v["tracked_path"] for v in _variants()}
    assert tracked == listed, tracked ^ listed
    for variant in _variants():
        path = Path(variant["tracked_path"])
        assert path.name == variant["name"], variant["name"]
        assert path.parent.name == f"gen{variant['generation']}", variant["name"]


def test_tracked_copy_matches_its_recorded_hash():
    for variant in _variants():
        data = (ROOT / variant["tracked_path"]).read_bytes()
        assert _sha(data) == variant["tracked_sha256"], variant["name"]


def test_docstring_identity_cannot_drift():
    for variant in _variants():
        text = (ROOT / variant["tracked_path"]).read_text(encoding="utf-8")
        first = _first_docstring_line(text)
        assert first.startswith(variant["name"] + " "), (variant["name"], first)


def test_body_hashes_are_frozen():
    for variant in _variants():
        assert variant["tracked_body_sha256"] == FROZEN_BODY_SHA256[variant["name"]], variant["name"]


def test_recorded_edits_match_the_hashes():
    for variant in _variants():
        text = (ROOT / variant["tracked_path"]).read_text(encoding="utf-8")
        assert _body_sha(text) == variant["tracked_body_sha256"], variant["name"]
        if variant["edit"] == "none":
            assert variant["source_sha256"] == variant["tracked_sha256"], variant["name"]
            assert variant["source_body_sha256"] == variant["tracked_body_sha256"], variant["name"]
        else:
            assert variant["source_sha256"] != variant["tracked_sha256"], variant["name"]


def test_manifest_hashes_are_lowercase_hex():
    for variant in _variants():
        for key in (
            "source_sha256",
            "tracked_sha256",
            "source_body_sha256",
            "tracked_body_sha256",
        ):
            assert re.fullmatch(r"[0-9a-f]{64}", variant[key]), (variant["name"], key)


def _driver_paths():
    return sorted(AGENTIC.rglob("*.py"))


def test_no_driver_carries_a_machine_path():
    for path in _driver_paths():
        text = path.read_text(encoding="utf-8")
        for needle in ("/Users/", "/tmp/b", "/private/tmp"):
            assert needle not in text, (path.name, needle)


def test_repo_root_default_is_derived_from_the_driver_location():
    saved = os.environ.pop("REVL_REPO", None)
    try:
        for path in _driver_paths():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            node = next(
                n for n in tree.body
                if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "REVL_REPO" for t in n.targets)
            )
            ns = {
                "os": os,
                "Path": Path,
                "__file__": str(path),
                "HERE": path.resolve().parent,
            }
            value = eval(compile(ast.Expression(node.value), str(path), "eval"), ns)
            assert Path(value).resolve() == ROOT, (path.name, value)
    finally:
        if saved is not None:
            os.environ["REVL_REPO"] = saved


def test_gen3_requires_arm_src_with_a_clear_error(tmp_path):
    driver = AGENTIC / "gen3" / "bench_agentic3.py"
    run = subprocess.run(
        [sys.executable, str(driver), "--workdir", str(tmp_path / "wd")],
        capture_output=True, text=True, cwd=tmp_path, timeout=60,
    )
    assert run.returncode == 2, (run.returncode, run.stderr[-400:])
    assert "--arm-src" in run.stderr and "Traceback" not in run.stderr, run.stderr[-400:]


def test_no_long_dash_in_comments_or_docstrings():
    dash = "\u2014"
    for path in _driver_paths():
        text = path.read_text(encoding="utf-8")
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                assert dash not in tok.string, (path.name, tok.start)
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node, clean=False) or ""
                assert dash not in doc, (path.name, getattr(node, "name", "module"))


def test_source_provenance_is_checkable_or_marked_unverified():
    readme = (AGENTIC / "README.md").read_text(encoding="utf-8")
    for variant in _variants():
        name = variant["name"]
        assert isinstance(variant["source_reread"], bool), name
        record = variant.get("source_record")
        if variant["source_reread"]:
            assert isinstance(record["size_bytes"], int) and record["size_bytes"] > 0, name
            assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", record["mtime"]), name
        else:
            assert record is None, name
            assert f"`{name}`" in readme.split("could not be re-read", 1)[1], name
