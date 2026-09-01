"""The stdlib CRYPTO module (roadmap item 272, docs/stdlib-2.0.md,
stdlib/crypto.rvl).

Three independent lighthouse-workload components hand-rolled the same crypto
inside `@py`/`@ts` extern bodies within one wave — a constant-time compare, an
HMAC-SHA256 encrypt-then-MAC, and HMAC webhook signatures. This module is that
kit, ONCE, as a classified PRIMITIVE set (item 244's pattern).

Checked here:
  * the module imports through `use` and its four externs reach the IR with the
    designed CLASSIFICATIONS — `sha256`/`hmac_sha256`/`ct_equal` are `pure`
    (a hash / MAC / compare has no effect and no entropy), `random_token` is
    `emission` (an entropy draw is a non-revertible boundary crossing that
    reads the host CSPRNG — NOT `pure`, and NOT an `acquire`-with-fake-undo;
    see the module header's classification rationale);
  * py + ts bodies exist on every primitive (the item-272 exit bar);
  * the py tier EXECUTES each primitive and produces the RIGHT value against a
    known vector — SHA-256 NIST vectors, RFC 4231 HMAC test case 2, `ct_equal`
    true/false/length, and `random_token` width + distinctness;
  * the ts tier runs the same assertions when vitest is present, and is
    skipped-with-reason otherwise (never reported as a pass) — the standard
    toolchain-absent contract (revl.test, FR-5).
"""

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.test import run_py, run_ts  # noqa: E402

STDLIB = ROOT / "stdlib" / "crypto.rvl"

#: a consumer that drives every primitive from `test` blocks (the RUNNERS
#: execute these), plus `pub fn` wrappers so the externs are also reached
#: outside effect position (proving `random_token`, an `emission`, is callable
#: from a plain `fn`).
CONSUMER = """\
use "stdlib/crypto.rvl" { sha256, hmac_sha256, ct_equal, random_token }

pub fn digest(s: Str) -> Str { return sha256(s) }
pub fn sign(key: Str, body: Str) -> Str { return hmac_sha256(key, body) }
pub fn same(a: Str, b: Str) -> Bool { return ct_equal(a, b) }
pub fn token(n: Int) -> Str { return random_token(n) }

// A realistic webhook-verify site (one of the three the workload hand-rolled):
// recompute the signature over the body and constant-time compare it.
pub fn webhook_ok(secret: Str, body: Str, sig: Str) -> Bool {
  return ct_equal(hmac_sha256(secret, body), sig)
}

test "sha256 NIST vectors" {
  assert sha256("") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
  assert sha256("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
}

test "hmac_sha256 RFC 4231 test case 2" {
  assert hmac_sha256("Jefe", "what do ya want for nothing?")
    == "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
}

test "ct_equal: equal, differing, and length-differing" {
  assert ct_equal("a-secret-token", "a-secret-token")
  assert !ct_equal("a-secret-token", "a-secret-tokeX")
  assert !ct_equal("short", "short-plus")
}

test "webhook verify accepts the right signature and rejects a forgery" {
  let body = "event=ping&id=42"
  let sig = hmac_sha256("whsec", body)
  assert webhook_ok("whsec", body, sig)
  assert !webhook_ok("whsec", body, "deadbeef")
  assert !webhook_ok("wrong", body, sig)
}

test "random_token: 2n hex chars, empty at zero, distinct across draws" {
  assert random_token(16).length() == 32
  assert random_token(0) == ""
  assert random_token(16) != random_token(16)
}
"""


@pytest.fixture(scope="module")
def crypto_ir(tmp_path_factory):
    d = tmp_path_factory.mktemp("crypto_consumer")
    (d / "stdlib").mkdir()
    shutil.copy(STDLIB, d / "stdlib" / "crypto.rvl")
    main = d / "main.rvl"
    main.write_text(CONSUMER, encoding="utf-8")
    return compile_files([str(main)])


# ---------------------------------------------------------------- the module

def test_module_imports_and_externs_reach_the_ir(crypto_ir):
    externs = {e["name"]: e for e in crypto_ir["externs"]}
    assert set(externs) == {"sha256", "hmac_sha256", "ct_equal", "random_token"}
    # the DESIGNED classifications (the item's central decision)
    assert externs["sha256"]["class"] == "pure"
    assert externs["hmac_sha256"]["class"] == "pure"
    assert externs["ct_equal"]["class"] == "pure"
    # an entropy draw is a non-revertible boundary crossing, not `pure` and not
    # an `acquire`-with-trivial-undo (module header rationale)
    assert externs["random_token"]["class"] == "emission"
    # signatures
    assert externs["sha256"]["returns"] == "Str"
    assert externs["hmac_sha256"]["returns"] == "Str"
    assert externs["ct_equal"]["returns"] == "Bool"
    assert externs["random_token"]["returns"] == "Str"
    # py + ts are the item-272 exit bar
    for e in crypto_ir["externs"]:
        assert set(e["bodies"]) == {"py", "ts"}, e["name"]


def test_module_file_is_the_documented_surface():
    text = STDLIB.read_text(encoding="utf-8")
    assert "pub extern pure fn sha256(data: Str) -> Str" in text
    assert "pub extern pure fn hmac_sha256(key: Str, data: Str) -> Str" in text
    assert "pub extern pure fn ct_equal(a: Str, b: Str) -> Bool" in text
    # the classification decision is the design — pin its spelling
    assert "pub extern emission fn random_token(n: Int) -> Str" in text


# ---------------------------------------------------------------- py tier

def test_py_tier_computes_the_right_values(crypto_ir):
    # run_py executes the consumer's `test` blocks in-process (no cordis needed
    # for plain test blocks); every known-vector assertion must hold.
    verdict, detail = run_py(crypto_ir)
    assert verdict == "pass", detail


# ---------------------------------------------------------------- ts tier

def test_ts_tier_computes_the_right_values_or_skips(crypto_ir):
    # the same assertions under vitest when the toolchain is present; a missing
    # toolchain is a skip-with-reason, never a pass and never a fail (FR-5).
    verdict, detail = run_ts(crypto_ir)
    if verdict == "skip":
        pytest.skip(detail)
    assert verdict == "pass", detail
