"""Issue #3432: every refusal of the untrusted-author profile carries a plain
sentence for the person who wrote the source.

The sentence rides beside `message`, `hint` and `code` as `plain`, in the
`--json-diagnostics` record and in the MCP answer's diagnostic and `refusal`.
It is written for someone who has never read the compiler's design, so it names
no code, no item number and none of the profile's own vocabulary.

The list of codes is read from the profile's own module: every `RevlError` it
raises, with the code that raise names. A new refusal there without a sentence
fails (1); a sentence that leaks an internal term fails (2).
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from revl import admit_profile
from revl.admit_profile import AdmissionProfile
from revl.compiler import compile_files
from revl.diagnostics import report
from revl.errors import RevlError
from revl.mcp import server

_MODULE = Path(admit_profile.__file__)
# read, not imported by name, so the base tree fails test by test rather than
# collecting nothing
PLAIN: dict = getattr(admit_profile, "PLAIN", {})

_SOURCE = (
    "service Compute { fn run(x: Str) -> Str }\n"
    "extern pure fn peek(x: Str) -> Str = @py { return x }\n"
    "component C provides compute: Compute {\n"
    "  provide compute { fn run(x) = peek(x) }\n"
    "}\n")


def _raise_sites() -> list[ast.Call]:
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    return [node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name) and node.func.id == "RevlError"]


def _keyword(call: ast.Call, name: str):
    return next((k.value for k in call.keywords if k.arg == name), None)


def _profile_codes() -> set[str]:
    codes = set()
    for call in _raise_sites():
        code = _keyword(call, "code")
        assert isinstance(code, ast.Constant), (
            f"admit_profile.py:{call.lineno}: a refusal names no literal code")
        codes.add(code.value)
    return codes


# ------------------------------------------------------- (1) every code covered

def test_the_profile_raises_refusals_with_codes():
    # the census is not empty: the reader below sees the real raise sites
    assert len(_raise_sites()) >= 11
    assert {"G8", "G9", "G-RETAIN", "R2"} <= _profile_codes()


def test_every_code_the_profile_refuses_with_has_a_plain_sentence():
    missing = sorted(code for code in _profile_codes()
                     if not any(s.strip() for s in PLAIN.get(code, {}).values()))
    assert missing == []


def test_every_refusal_site_passes_its_plain_sentence():
    bare = [call.lineno for call in _raise_sites()
            if _keyword(call, "plain") is None]
    assert bare == [], f"admit_profile.py refusals with no plain=: lines {bare}"


def test_every_plain_kind_belongs_to_a_code_the_profile_refuses_with():
    assert sorted(set(PLAIN) - _profile_codes()) == []


# ------------------------------------------------------ (2) no internal terms

# codes are matched case-sensitively (`G8`, `G-RETAIN`, `R2`), the words in
# any case
_FORBIDDEN = re.compile(
    r"\bG[0-9]+\b|\bG-[A-Z]+\b|\bR[0-9]+\b|\bT[0-9]+\b"
    r"|(?i:item\s*#?\s*[0-9]+|#[0-9]+|profile|admission|host-block|extern)")


def _sentences():
    return [(code, kind, text) for code, kinds in PLAIN.items()
            for kind, text in kinds.items()]


@pytest.mark.parametrize("code,kind,text", _sentences(),
                         ids=[f"{c}-{k}" for c, k, _t in _sentences()])
def test_a_plain_sentence_names_no_internal_term(code, kind, text):
    rendered = text.format(name="peek")
    assert _FORBIDDEN.search(rendered) is None, (code, kind, rendered)
    assert code not in rendered
    assert "{name}" in text, "the sentence names the author's own identifier"


def test_the_term_check_catches_what_it_is_for():
    # a control: the reader above is aimed, not silent
    for leaked in ("see G8", "item 24 says", "the untrusted-author profile",
                   "admission refused", "a host-block body", "declare an extern",
                   "code G-RETAIN", "R2 allowlist"):
        assert _FORBIDDEN.search(leaked), leaked


# --------------------------------------- (3) the field rides both surfaces

def _plain_of(record: dict) -> str:
    assert record["code"] == "G8"
    plain = record.get("plain")
    assert isinstance(plain, str) and plain, record
    return plain


def test_the_extern_refusal_carries_plain_in_json_diagnostics(tmp_path):
    path = tmp_path / "rule.rvl"
    path.write_text(_SOURCE, encoding="utf-8")
    with pytest.raises(RevlError) as refused:
        compile_files([str(path)], profile=AdmissionProfile.untrusted_author(()))
    record = report(refused.value)["diagnostics"][0]
    plain = _plain_of(record)
    assert "`peek`" in plain
    assert _FORBIDDEN.search(plain) is None
    # the full message, hint and code are kept for whoever wants them
    assert "untrusted-author profile forbids new `extern`" in record["message"]
    assert "G8" in record["hint"]


@pytest.fixture
def _untrusted_server(tmp_path, monkeypatch):
    from revl.mcp.session import Session

    before = server.AUTHORING
    monkeypatch.setattr(server, "SESSION", Session())
    server.set_authoring_trust(host_code=False, granted=None, providers=None,
                               roots=(str(tmp_path),))
    yield
    server.AUTHORING = before


def test_the_extern_refusal_carries_plain_in_the_mcp_answer(_untrusted_server):
    response = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "revl_check", "arguments": {"source": _SOURCE}}})
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["ok"] is False
    plain = _plain_of(payload["diagnostics"][0])
    assert "`peek`" in plain
    assert payload["refusal"]["plain"] == plain
    assert payload["refusal"]["message"] == payload["diagnostics"][0]["message"]
