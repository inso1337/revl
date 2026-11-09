"""AGENTIC-DRIVERS-1: the six agentic benchmark drivers are traceable (issue #2621).

The drivers were written in scratch directories and copied between them, so the
same docstring ended up over three different capped variants and no published
number could be traced back to a file. These tests hold the tracked copies to
the properties that make them traceable, not to the numbers they produce:

  * every driver under `bench/agentic/` is named in the manifest, and every
    manifest entry is a driver in that tree;
  * each tracked file still hashes to its recorded `tracked_sha256`;
  * the source with the module docstring token removed still hashes to its
    recorded body sha, and the tracked copy to its own; the recorded edits are
    the docstring identity lines and explicit UTF-8 encodings, which do not
    change benchmark behaviour;
  * the tracked body hashes are pinned here, outside the manifest, so a body
    edit fails even if the manifest is refreshed with it;
  * each driver's docstring opens with its own filename, the identity a copied
    variant used to lose;
  * an `edit: none` entry records the tracked hash as equal to the source hash.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import re
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
    "bench_agentic.py": "2727550b325d467809d4c98f7aab03f3eb18f8c278fd9dd1a3ec05bc4d54b46b",
    "bench_agentic2.py": "7c35b98908461875d8b0191f16c058f874aa05dd525b31580feb66e7954fd0bf",
    "bench_agentic2_cap.py": "4931cfa414f625beee870e07a3812dd0e16ba53109599641c94b4a18f9de87e4",
    "bench_agentic2_cap6.py": "0d9146dfcb59015ae5df8c3653ce7005c8eab09d41122f4523363e3df1617168",
    "bench_agentic2_cap2.py": "1da9a92567e12ff5f7cf588bdb2e765613bf8de4043ded6c73fad921350428ab",
    "bench_agentic3.py": "9fe01ceb9603855ac87412c32a108bad99662e03e9d00bcf57c95d69fd85b1f2",
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
