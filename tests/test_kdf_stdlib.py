"""The stdlib KDF module (`stdlib/kdf.rvl`).

Password storage needs exactly two primitives that the tree kept re-deriving:
PBKDF2-HMAC-SHA256 with parameters at the call site, and a constant-time digest
compare. `stdlib/crypto.rvl` deliberately ships no KDF ("`hmac_sha256` is a MAC,
not a password hash: no work factor"), so this module is that missing primitive,
ONCE, as a classified `pure` kit.

Checked here:
  * the module imports through `use`; its one extern reaches the IR as `pure`
    with py + ts bodies (the module's tier bar), and it is NON-PUB: a named
    import is refused;
  * module privacy is not enforced on the bare-reference path (revl issue
    #3004), so a consumer importing only `KdfError` can still call
    `pbkdf2_sha256_raw` by bare name: the boundary extern validates its own
    inputs and those bare-name calls above the cap are REFUSED, not executed;
  * the bound constants are pinned (`max_iterations` 2000000, `max_length` 64),
    so a future policy change is a deliberate edit rather than a silent one;
  * the py tier EXECUTES the published PBKDF2-HMAC-SHA256 vectors and the
    validate/verify behaviour (the reference tier);
  * the ts tier runs the SAME consumer under vitest when the toolchain is
    present, and is skipped-with-reason otherwise (never reported as a pass);
  * validation refusals are the named codes, in the documented ORDER; boundary
    inputs are accepted; a wrong iteration count, salt or password answers
    `Ok(false)` (data), never an `Err` that names which step disagreed, and the
    compare goes through `ct_equal`;
  * tiers without a body refuse BY NAME at emit time.

The vectors are the PBKDF2-HMAC-SHA256 set published in Appendix B of the scrypt
KDF draft (draft-josefsson-scrypt-kdf, the set RFC 7914 inherited), re-derived
here with Python's `hashlib.pbkdf2_hmac` before being pinned so a transcription
error cannot pass as a vector.
"""

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from revl import compile_files  # noqa: E402
from revl.__main__ import main  # noqa: E402
from revl.test import run_py, run_ts  # noqa: E402

STDLIB = ROOT / "stdlib" / "kdf.rvl"
CRYPTO = ROOT / "stdlib" / "crypto.rvl"

#: A consumer that drives the kit from `test` blocks (the RUNNERS execute these)
#: plus the small helpers those blocks need. Every assertion is expressed as a
#: Bool so one program runs unchanged on py and ts.
CONSUMER = """\
use "stdlib/kdf.rvl" {
  KdfError,
  pbkdf2_sha256,
  pbkdf2_sha256_verify,
  max_iterations,
  max_length,
}

fn hex_is(r: Result[Str, KdfError], expected: Str) -> Bool {
  return match r {
    Ok(actual) => actual == expected,
    Err(_) => false,
  }
}

fn code_of(r: Result[Str, KdfError]) -> Str {
  return match r {
    Ok(_) => "ok",
    Err(e) => e.code,
  }
}

fn verify_matches(r: Result[Bool, KdfError]) -> Bool {
  return match r {
    Ok(matches) => matches,
    Err(_) => false,
  }
}

fn verify_answered(r: Result[Bool, KdfError]) -> Bool {
  return match r {
    Ok(_) => true,
    Err(_) => false,
  }
}

fn verify_codes(r: Result[Bool, KdfError], expected: Str) -> Bool {
  return match r {
    Ok(_) => false,
    Err(e) => e.code == expected,
  }
}

// ----------------------------------------------- the published vectors

test "PBKDF2-HMAC-SHA256 vector 1: password/salt, c=1, dkLen=32" {
  assert hex_is(pbkdf2_sha256("password", "salt", 1, 32),
    "120fb6cffcf8b32c43e7225256c4f837a86548c92ccc35480805987cb70be17b")
}

test "PBKDF2-HMAC-SHA256 vector 2: password/salt, c=2, dkLen=32" {
  assert hex_is(pbkdf2_sha256("password", "salt", 2, 32),
    "ae4d0c95af6b46d32d0adff928f06dd02a303f8ef3c251dfd6e2d85a95474c43")
}

test "PBKDF2-HMAC-SHA256 vector 3: password/salt, c=4096, dkLen=32" {
  assert hex_is(pbkdf2_sha256("password", "salt", 4096, 32),
    "c5e478d59288c841aa530db6845c4c8d962893a001ce4e11a4963873aa98134a")
}

test "PBKDF2-HMAC-SHA256 vector 4: long password and salt, c=4096, dkLen=40" {
  assert hex_is(
    pbkdf2_sha256("passwordPASSWORDpassword",
                  "saltSALTsaltSALTsaltSALTsaltSALTsalt", 4096, 40),
    "348c89dbcbd32b2f32d814b8116e84cf2b17347ebc1800181c4e2a1fb8dd53e1c635518c7dac47e9")
}

test "dkLen truncates the same derivation: c=4096, dkLen=16 is the 32-byte prefix" {
  assert hex_is(pbkdf2_sha256("password", "salt", 4096, 16),
    "c5e478d59288c841aa530db6845c4c8d")
}

// ----------------------------------------------- bounds and boundaries

test "the declared bounds are the kdf kit's, not a hidden policy default" {
  assert max_iterations() == 2000000
  assert max_length() == 64
}

test "boundary inputs inside the bounds are accepted" {
  assert match pbkdf2_sha256("p", "s", 1, 1) {
    Ok(hex) => hex.length() == 2,
    Err(_) => false,
  }
  assert match pbkdf2_sha256("p", "s", 1, max_length()) {
    Ok(hex) => hex.length() == 128,
    Err(_) => false,
  }
  // a whitespace salt is non-empty: the kit refuses only the EMPTY salt
  assert match pbkdf2_sha256("p", " ", 1, 32) {
    Ok(hex) => hex.length() == 64,
    Err(_) => false,
  }
}

// ----------------------------------------------- validation, by named code

test "iterations below 1 are refused as iterations_out_of_range" {
  assert code_of(pbkdf2_sha256("p", "s", 0, 32)) == "iterations_out_of_range"
  assert code_of(pbkdf2_sha256("p", "s", -1, 32)) == "iterations_out_of_range"
}

test "iterations above the declared cap are refused, not executed" {
  assert code_of(pbkdf2_sha256("p", "s", max_iterations() + 1, 32))
    == "iterations_out_of_range"
}

test "length outside 1..max_length is refused as length_out_of_range" {
  assert code_of(pbkdf2_sha256("p", "s", 1, 0)) == "length_out_of_range"
  assert code_of(pbkdf2_sha256("p", "s", 1, -1)) == "length_out_of_range"
  assert code_of(pbkdf2_sha256("p", "s", 1, max_length() + 1))
    == "length_out_of_range"
}

test "an empty salt is refused as salt_empty" {
  assert code_of(pbkdf2_sha256("p", "", 1, 32)) == "salt_empty"
}

test "the checks run in the documented order: iterations, then length, then salt" {
  // all three invalid: the FIRST rule wins, so two call sites agree which
  // refusal a doubly-bad call earns
  assert code_of(pbkdf2_sha256("p", "", 0, 0)) == "iterations_out_of_range"
  assert code_of(pbkdf2_sha256("p", "", 1, 0)) == "length_out_of_range"
}

// ----------------------------------------------- verify: mismatch is data

test "verify accepts the digest derived with the stored parameters" {
  let stored = "c5e478d59288c841aa530db6845c4c8d962893a001ce4e11a4963873aa98134a"
  assert verify_matches(pbkdf2_sha256_verify("password", "salt", 4096, 32, stored))
}

test "a wrong password is Ok(false), never an error naming the step" {
  let stored = "c5e478d59288c841aa530db6845c4c8d962893a001ce4e11a4963873aa98134a"
  assert verify_answered(pbkdf2_sha256_verify("password!", "salt", 4096, 32, stored))
  assert !verify_matches(pbkdf2_sha256_verify("password!", "salt", 4096, 32, stored))
}

test "a wrong salt is the same indistinguishable Ok(false)" {
  let stored = "c5e478d59288c841aa530db6845c4c8d962893a001ce4e11a4963873aa98134a"
  assert verify_answered(pbkdf2_sha256_verify("password", "pepper", 4096, 32, stored))
  assert !verify_matches(pbkdf2_sha256_verify("password", "pepper", 4096, 32, stored))
}

test "a wrong iteration count is the same indistinguishable Ok(false)" {
  let stored = "c5e478d59288c841aa530db6845c4c8d962893a001ce4e11a4963873aa98134a"
  assert verify_answered(pbkdf2_sha256_verify("password", "salt", 4095, 32, stored))
  assert !verify_matches(pbkdf2_sha256_verify("password", "salt", 4095, 32, stored))
}

test "verify passes invalid parameters through as the named errors" {
  assert verify_codes(pbkdf2_sha256_verify("p", "s", 0, 32, "00"),
                      "iterations_out_of_range")
  assert verify_codes(pbkdf2_sha256_verify("p", "", 1, 32, "00"), "salt_empty")
}

// ------------------------------------- the boundary cannot be bypassed
// Module privacy is not enforced for BARE references today (revl issue #3004):
// a consumer that imports only `KdfError` can still call the non-pub extern by
// bare name. These tests drive that door, so the boundary itself must refuse.

test "a bare-name raw call above the iteration cap refuses, never executes" {
  assert code_of(pbkdf2_sha256_raw("p", "s", max_iterations() + 1, 32))
    == "iterations_out_of_range"
}

test "a bare-name raw call with an invalid length or empty salt refuses" {
  assert code_of(pbkdf2_sha256_raw("p", "s", 1, 0)) == "length_out_of_range"
  assert code_of(pbkdf2_sha256_raw("p", "s", 1, 65)) == "length_out_of_range"
  assert code_of(pbkdf2_sha256_raw("p", "", 1, 32)) == "salt_empty"
}

test "a bare-name raw call below the bounds still computes the vector" {
  assert match pbkdf2_sha256_raw("password", "salt", 1, 32) {
    Ok(hex) => hex == "120fb6cffcf8b32c43e7225256c4f837a86548c92ccc35480805987cb70be17b",
    Err(_) => false,
  }
}
"""


@pytest.fixture(scope="module")
def kdf_ir(tmp_path_factory):
    d = tmp_path_factory.mktemp("kdf_consumer")
    (d / "stdlib").mkdir()
    shutil.copy(STDLIB, d / "stdlib" / "kdf.rvl")
    shutil.copy(CRYPTO, d / "stdlib" / "crypto.rvl")
    consumer = d / "main.rvl"
    consumer.write_text(CONSUMER, encoding="utf-8")
    return compile_files([str(consumer)])


# ---------------------------------------------------------------- the module

def test_module_file_is_the_documented_surface():
    text = STDLIB.read_text(encoding="utf-8")
    # the public surface, spelled
    assert "pub fn pbkdf2_sha256(" in text
    assert "pub fn pbkdf2_sha256_verify(" in text
    assert "pub fn max_iterations() -> Int" in text
    assert "pub fn max_length() -> Int" in text
    assert "pub type KdfError = { code: Str, message: Str }" in text
    # the classification decision is the design: the boundary is `pure`, and it
    # returns the same typed error the wrapper does (enforced inside the body)
    assert "extern pure fn pbkdf2_sha256_raw(" in text
    assert "-> Result[Str, KdfError]" in text
    assert "extern emission" not in text
    # the verify path compares through the kit's constant-time primitive
    assert "Ok(ct_equal(actual, expected_hex))" in text


def test_module_imports_and_the_extern_reaches_the_ir(kdf_ir):
    externs = {e["name"]: e for e in kdf_ir["externs"]}
    raw = externs["pbkdf2_sha256_raw"]
    assert raw["class"] == "pure"
    assert raw["returns"] == "Result[Str, KdfError]"
    assert [p["name"] for p in raw["params"]] == [
        "password", "salt", "iterations", "length"]
    # py + ts are the tier bar; every other tier refuses by name (tested below)
    assert set(raw["bodies"]) == {"py", "ts"}
    # the wrapper functions are the public surface (the IR also carries the
    # consumer's own helpers, so this is a subset check)
    assert {
        "max_iterations", "max_length", "pbkdf2_sha256", "pbkdf2_sha256_verify"
    } <= {f["name"] for f in kdf_ir["functions"]}
    assert kdf_ir["types"]["KdfError"]["kind"] == "record"
    assert set(kdf_ir["types"]["KdfError"]["fields"]) == {"code", "message"}


def test_the_caller_passes_the_parameters_no_module_default():
    # "no policy in the module": the derive call carries salt, iterations and
    # length from the caller, so the signature itself is the proof there is no
    # hidden default. A defaulted variant would have to change this list.
    text = STDLIB.read_text(encoding="utf-8")
    assert ("pub fn pbkdf2_sha256(password: Str, salt: Str, iterations: Int, "
            "length: Int)") in text
    # the recommended 600k default lives in deployment configuration, not here
    assert "600000" not in text


def test_the_boundary_bodies_enforce_the_declared_bounds(kdf_ir):
    """Both bodies validate the same rules, so neither door trusts the other.

    This is the property the review's repro falsified for the wrapper-only
    design: a consumer can call the non-pub extern by bare name (issue #3004),
    so the body itself must carry the three checks and the two bounds.
    """
    raw = {e["name"]: e for e in kdf_ir["externs"]}["pbkdf2_sha256_raw"]
    for tier, body in raw["bodies"].items():
        assert "MAX_ITERATIONS = 2000000" in body, tier
        assert "MAX_LENGTH = 64" in body, tier
        for code in ("iterations_out_of_range", "length_out_of_range",
                     "salt_empty"):
            assert code in body, (tier, code)


def test_named_import_of_the_boundary_extern_is_refused(tmp_path):
    """The import site already refuses the non-pub name, and must keep doing so.

    The compiler gap issue #3004 is the BARE-reference path, not this one; this
    test pins the half that is enforced so a later privacy fix does not silently
    publish the raw extern.
    """
    from revl.errors import RevlError

    d = tmp_path
    (d / "stdlib").mkdir()
    shutil.copy(STDLIB, d / "stdlib" / "kdf.rvl")
    shutil.copy(CRYPTO, d / "stdlib" / "crypto.rvl")
    consumer = d / "main.rvl"
    consumer.write_text(
        'use "stdlib/kdf.rvl" { pbkdf2_sha256_raw }\n'
        'test "x" { assert true }\n',
        encoding="utf-8")
    with pytest.raises(RevlError) as raised:
        compile_files([str(consumer)])
    message = str(raised.value)
    assert "pbkdf2_sha256_raw" in message
    assert "module-private" in message
    assert "cannot be imported" in message


# ---------------------------------------------------------------- py tier

def test_py_tier_computes_the_vectors_and_the_refusals(kdf_ir):
    verdict, detail = run_py(kdf_ir)
    assert verdict == "pass", detail


def test_py_body_is_the_hashlib_reference(tmp_path):
    out = tmp_path / "out.py"
    assert main(["emit", "--backend", "python", str(STDLIB), "-o", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "hashlib.pbkdf2_hmac(" in text
    assert '.hex()' in text


# ---------------------------------------------------------------- ts tier

def test_ts_tier_computes_the_vectors_and_the_refusals(kdf_ir):
    verdict, detail = run_ts(kdf_ir)
    if verdict == "skip":
        pytest.skip(detail)
    assert verdict == "pass", detail


def test_ts_body_is_node_crypto_pbkdf2sync(tmp_path):
    out = tmp_path / "out.ts"
    assert main(["emit", "--backend", "typescript", str(STDLIB), "-o", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert 'getBuiltinModule("node:crypto")' in text
    assert "pbkdf2Sync(" in text
    assert '"sha256"' in text


# ------------------------------------------------- tiers with no body

@pytest.mark.parametrize("backend, tag", [
    ("rust", "rs"),
    ("java", "java"),
    ("go", "go"),
    ("wasm", "wasm"),
])
def test_tiers_without_a_kdf_body_refuse_by_name(tmp_path, capsys, backend, tag):
    out = tmp_path / f"out.{backend}"
    rc = main(["emit", "--backend", backend, str(STDLIB), "-o", str(out)])
    assert rc != 0
    err = capsys.readouterr().err
    assert f"`pbkdf2_sha256_raw` has no @{tag} body" in err
    assert not out.exists()
