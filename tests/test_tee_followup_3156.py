"""What #3281 left open on issue #3156: platform-key revocation, a SEV-SNP report
with no stated TCB floor, and the Turin TCB layout.

#3281 made a TDX quote answer for its runtime registers, TCB and quoting enclave,
and compared a SEV-SNP report's TCB when the requirement stated a floor. Three
gaps remained, each pinned below:

* a pinned or endorsed platform key kept vouching after the operator knew the
  platform was vulnerable, because a root had no way to say "not this one";
* a SEV-SNP report was admitted when the requirement said nothing about the TCB,
  the same silence a TDX quote no longer gets away with;
* the floor read the Milan and Genoa bytes of `TCB_VERSION` only, so a Turin
  report, which AMD lays out differently, was compared on the wrong bytes.

The controls matter as much as the refusals: every refusal has a twin that admits
under the same construction with the one fact changed, so a refusal cannot come
from something else being wrong.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from revl import placement as plc  # noqa: E402
from revl import tee_attestation as tee  # noqa: E402
from revl import tee_quote as q  # noqa: E402
from revl.tee_attestation import (  # noqa: E402
    TdxPolicy,
    TeeError,
    build_sev_snp_evidence,
)
from test_tee_hardware_root import (  # noqa: E402
    NOW,
    PEER_KEY,
    SEV_MEASUREMENT,
    SEV_VCEK,
    SEV_VCEK_ID,
    SEV_VCEK_PUB,
    TDX_PCK_ID,
    TDX_PCK_PUB,
    admits,
    attest_table,
    evidence,
    hardware_root,
    offer,
    placement_file,
    requirement,
    sev_proof,
    tdx_proof,
)

# ----------------------------------------------------------------- revocation

AMD_ROOT_PRIVATE = q.private_key_from_seed(q.CURVE_P384, b"revl-test/amd-root-3156")
AMD_ROOT_PUB = q.derive_public_key(q.CURVE_P384, AMD_ROOT_PRIVATE)
AMD_ROOT = q.PinnedKey("P-384", AMD_ROOT_PUB, "amd-root")

OTHER_KEY_ID = "0123456789abcdef"


def endorsement():
    """A vendor-root endorsement of the SEV-SNP VCEK, valid at NOW."""
    return q.sign_endorsement(
        q.PlatformEndorsement(
            vendor=q.VENDOR_AMD_SEV_SNP, curve="P-384",
            platform_key=SEV_VCEK_PUB.hex(),
            not_before="2026-01-01T00:00:00+00:00",
            not_after="2027-01-01T00:00:00+00:00"),
        AMD_ROOT, AMD_ROOT_PRIVATE)


def endorsed_sev_proof(**quote_fields):
    return build_sev_snp_evidence(
        evidence(measurement=SEV_MEASUREMENT), vcek_private_key=SEV_VCEK,
        platform_key_id=SEV_VCEK_ID, endorsement=endorsement(), **quote_fields)


def endorsing_root(**overrides):
    fields = {"endorsement_roots": [AMD_ROOT]}
    fields.update(overrides)
    return q.HardwareRoot(**fields)


def sev_requirement(**overrides):
    fields = {"measurements": frozenset({SEV_MEASUREMENT}), "sev_min_tcb": 0}
    fields.update(overrides)
    return requirement(**fields)


def test_an_endorsed_platform_key_is_admitted_until_it_is_revoked():
    """The control: the same endorsement, the same root and the same report admit
    when nothing is revoked, so the refusal below is the revocation's."""
    ok, reason = admits(endorsed_sev_proof(), sev_requirement(),
                        root=endorsing_root())
    assert ok, reason


def test_a_revoked_endorsed_platform_key_is_refused():
    """The case revocation exists for: one pinned vendor root vouches for every
    platform it has ever endorsed, and the operator knows one of them is
    vulnerable."""
    ok, reason = admits(
        endorsed_sev_proof(), sev_requirement(),
        root=endorsing_root(revoked_platform_keys=[SEV_VCEK_ID]))
    assert not ok
    assert f"platform key {SEV_VCEK_ID} is revoked" in reason


def test_a_revoked_pinned_platform_key_is_refused():
    ok, reason = admits(tdx_proof(), requirement(),
                        root=hardware_root(revoked_platform_keys=[TDX_PCK_ID]))
    assert not ok
    assert f"platform key {TDX_PCK_ID} is revoked" in reason


def test_revocation_wins_over_a_pin():
    """A key left in `platform_keys` by mistake while it is also revoked is
    refused: the safe reading of a contradictory configuration."""
    root = q.HardwareRoot(
        platform_keys=[q.PinnedKey("P-256", TDX_PCK_PUB)],
        revoked_platform_keys=[TDX_PCK_ID])
    ok, reason = admits(tdx_proof(), requirement(), root=root)
    assert not ok and "revoked" in reason


def test_revoking_another_key_leaves_this_one_admitted():
    ok, reason = admits(tdx_proof(), requirement(), root=hardware_root(
        revoked_platform_keys=[OTHER_KEY_ID]))
    assert ok, reason


def test_a_revoked_key_is_refused_by_the_quote_verifier_itself():
    """Not only through `tee_admits`: the verdict a caller reads directly is a
    refusal too."""
    record = sev_proof()
    blob = bytes.fromhex(record[tee.QUOTE_FIELD])
    verdict = q.verify_quote(
        blob, sign_alg=q.SIGN_ALG_SEV_SNP,
        root=hardware_root(revoked_platform_keys=[SEV_VCEK_ID]),
        expect_report_data=tee.evidence_report_data(record),
        platform_key_id=SEV_VCEK_ID, now=NOW)
    assert not verdict.ok and "revoked" in verdict.reason


def test_a_revoked_key_can_be_named_by_its_pin():
    pin = q.PinnedKey("P-256", TDX_PCK_PUB)
    root = hardware_root(revoked_platform_keys=[pin])
    assert root.revoked_platform_keys == frozenset({TDX_PCK_ID})
    assert "1 revoked platform key(s)" in root.describe()


@pytest.mark.parametrize("bad", [
    "not-a-fingerprint", "ABCDEF0123456789", "0123456789abcde", 12, None, b"bytes"])
def test_a_revoked_key_must_be_a_fingerprint(bad):
    with pytest.raises(q.RootError, match="fingerprint"):
        hardware_root(revoked_platform_keys=[bad])


@pytest.mark.parametrize("single", [OTHER_KEY_ID, b"0123456789abcdef"])
def test_a_single_revoked_value_is_not_a_collection(single):
    with pytest.raises(q.RootError, match="collection"):
        hardware_root(revoked_platform_keys=single)


def test_a_root_revokes_nothing_by_default():
    assert hardware_root().revoked_platform_keys == frozenset()


# --------------------------------------------- a SEV-SNP report states its floor


def test_a_sev_snp_report_is_refused_when_the_requirement_states_no_tcb_floor():
    """`SEV current_tcb/reported_tcb = 0, platform_info=0` was admitted before this
    issue when the requirement said nothing about the TCB: silence read as no
    demand."""
    ok, reason = admits(sev_proof(),
                        requirement(measurements=frozenset({SEV_MEASUREMENT})))
    assert not ok
    assert "no sev_min_tcb" in reason and "0 to accept any TCB" in reason


def test_a_zero_floor_is_the_explicit_statement_of_no_minimum():
    ok, reason = admits(sev_proof(), sev_requirement(sev_min_tcb=0))
    assert ok, reason


def test_a_tdx_policy_does_not_stand_in_for_a_sev_snp_floor():
    """A requirement that accepts a TD on MRTD alone and says nothing about SEV-SNP
    used to admit any SEV-SNP report, at any TCB, with no demand stated."""
    ok, reason = admits(
        sev_proof(),
        requirement(measurements=frozenset({SEV_MEASUREMENT}),
                    tdx=TdxPolicy(accept_mrtd_only=True)))
    assert not ok and "no sev_min_tcb" in reason


def test_the_placement_file_without_a_sev_floor_refuses_a_sev_snp_report():
    table = {k: v for k, v in attest_table().items() if k != "runtime_measurements"}
    table["measurements"] = [SEV_MEASUREMENT]
    ok, reason = plc.admit_peer_for_process(
        placement_file(table), "worker", offer(proof=sev_proof()),
        offer_key=PEER_KEY, root=hardware_root(), tee_ledger=set(), now=NOW)
    assert not ok and "no sev_min_tcb" in reason


def test_the_placement_file_can_state_a_zero_floor():
    table = {k: v for k, v in attest_table().items() if k != "runtime_measurements"}
    table["measurements"] = [SEV_MEASUREMENT]
    table["sev_min_tcb"] = 0
    ok, reason = plc.admit_peer_for_process(
        placement_file(table), "worker", offer(proof=sev_proof()),
        offer_key=PEER_KEY, root=hardware_root(), tee_ledger=set(), now=NOW)
    assert ok, reason


def test_a_tdx_quote_still_needs_no_sev_floor():
    """The fail-closed rule is per format: a deployment that places on TDX states
    nothing about SEV-SNP."""
    ok, reason = admits(tdx_proof(), requirement())
    assert ok, reason


# ------------------------------------------------------- the TCB layout by CPUID


@pytest.mark.parametrize("version, family, model, layout", [
    (2, 0, 0, q.SEV_TCB_LAYOUT_MILAN_GENOA),     # predates the CPUID fields
    (3, 0x19, 0x01, q.SEV_TCB_LAYOUT_MILAN_GENOA),   # Milan
    (3, 0x19, 0x0F, q.SEV_TCB_LAYOUT_MILAN_GENOA),
    (3, 0x19, 0x10, q.SEV_TCB_LAYOUT_MILAN_GENOA),   # Genoa
    (3, 0x19, 0x1F, q.SEV_TCB_LAYOUT_MILAN_GENOA),
    (3, 0x19, 0xA0, q.SEV_TCB_LAYOUT_MILAN_GENOA),   # Genoa, second range
    (3, 0x19, 0xAF, q.SEV_TCB_LAYOUT_MILAN_GENOA),
    (5, 0x1A, 0x00, q.SEV_TCB_LAYOUT_TURIN),
    (5, 0x1A, 0x02, q.SEV_TCB_LAYOUT_TURIN),
    (5, 0x1A, 0x1F, q.SEV_TCB_LAYOUT_TURIN),
    (3, 0x19, 0x20, ""),      # between the Genoa ranges: not a known model
    (3, 0x19, 0xB0, ""),
    (5, 0x1A, 0x20, ""),
    (5, 0x1A, 0x50, ""),      # Venice has its own layout, which is not read
    (5, 0x1A, 0xC0, ""),
    (3, 0x00, 0x00, ""),      # a version 3 report whose CPUID was never stamped
    (5, 0x17, 0x01, ""),
])
def test_the_layout_is_decided_by_the_stamped_processor(version, family, model,
                                                          layout):
    assert q.sev_tcb_layout(version, family, model) == layout


def test_the_parser_reads_the_cpuid_the_firmware_stamped():
    proof = build_sev_snp_evidence(
        evidence(measurement=SEV_MEASUREMENT), vcek_private_key=SEV_VCEK,
        platform_key_id=SEV_VCEK_ID, version=5, cpuid_family=0x1A,
        cpuid_model=0x02, cpuid_stepping=0x03)
    report = q.parse_sev_snp_report(bytes.fromhex(proof[tee.QUOTE_FIELD]))
    assert (report.cpuid_family, report.cpuid_model, report.cpuid_stepping) \
        == (0x1A, 0x02, 0x03)
    assert report.tcb_layout == q.SEV_TCB_LAYOUT_TURIN


def test_a_version_2_report_does_not_read_a_cpuid_it_does_not_have():
    """Version 2 leaves those bytes reserved, so a stray value in them must not
    pick a layout."""
    proof = build_sev_snp_evidence(
        evidence(measurement=SEV_MEASUREMENT), vcek_private_key=SEV_VCEK,
        platform_key_id=SEV_VCEK_ID, version=2, cpuid_family=0x1A,
        cpuid_model=0x02)
    report = q.parse_sev_snp_report(bytes.fromhex(proof[tee.QUOTE_FIELD]))
    assert report.cpuid_family == 0 and report.tcb_layout \
        == q.SEV_TCB_LAYOUT_MILAN_GENOA


@pytest.mark.parametrize("version", [2, 3, 5])
def test_the_report_versions_this_root_reads(version):
    assert version in q.SEV_SNP_VERSIONS


@pytest.mark.parametrize("version", [1, 4, 6, 9])
def test_any_other_report_version_is_refused(version):
    proof = build_sev_snp_evidence(
        evidence(measurement=SEV_MEASUREMENT), vcek_private_key=SEV_VCEK,
        platform_key_id=SEV_VCEK_ID, version=version)
    with pytest.raises(q.QuoteFormatError, match="unsupported SEV-SNP report"):
        q.parse_sev_snp_report(bytes.fromhex(proof[tee.QUOTE_FIELD]))


# ----------------------------------------------------------- the Turin TCB floor

# A Turin `TCB_VERSION` in report byte order: FMC (byte 0), bootloader (1), TEE (2),
# SNP (3), three reserved bytes (4 to 6) and microcode (7).
TURIN_COMPONENTS = {"FMC": 0, "bootloader": 1, "TEE": 2, "SNP": 3, "microcode": 7}


def turin_bytes(fmc, bootloader, tee_svn, snp, microcode, reserved=b"\x00" * 3):
    return bytes([fmc, bootloader, tee_svn, snp]) + reserved + bytes([microcode])


def tcb_int(raw):
    return int.from_bytes(raw, "little")


TURIN_FLOOR_BYTES = turin_bytes(fmc=0x02, bootloader=0x04, tee_svn=0x01,
                                snp=0x16, microcode=0x73)
TURIN_FLOOR = 0x7300000016010402


def turin_proof(raw, **fields):
    """A Turin report (version 5, family 0x1A, model 0x02) whose current and
    reported TCB carry ``raw``, checked against the bytes the verifier reads."""
    values = {"version": 5, "cpuid_family": 0x1A, "cpuid_model": 0x02,
              "current_tcb": tcb_int(raw), "reported_tcb": tcb_int(raw)}
    values.update(fields)
    proof = build_sev_snp_evidence(
        evidence(measurement=SEV_MEASUREMENT), vcek_private_key=SEV_VCEK,
        platform_key_id=SEV_VCEK_ID, **values)
    report = bytes.fromhex(proof[tee.QUOTE_FIELD])
    assert report[0x038:0x040] == raw and report[0x180:0x188] == raw
    return proof


def turin_requirement(floor=TURIN_FLOOR, **overrides):
    return sev_requirement(sev_min_tcb=None, sev_turin_min_tcb=floor, **overrides)


def test_the_example_turin_floor_is_the_layout_spelled_as_an_integer():
    assert tcb_int(TURIN_FLOOR_BYTES) == TURIN_FLOOR


def test_a_turin_report_at_every_component_floor_is_admitted():
    ok, reason = admits(turin_proof(TURIN_FLOOR_BYTES), turin_requirement())
    assert ok, reason


@pytest.mark.parametrize("component", sorted(TURIN_COMPONENTS))
def test_a_turin_report_below_the_floor_in_one_component_is_refused(component):
    """One below the floor in ``component`` and one above it in every other, so a
    high SVN elsewhere cannot offset it, and the refusal names the component and
    the byte the Turin layout puts it in."""
    raw = bytearray(TURIN_FLOOR_BYTES)
    for name, byte in TURIN_COMPONENTS.items():
        raw[byte] += -1 if name == component else 1
    ok, reason = admits(turin_proof(bytes(raw)), turin_requirement())
    assert not ok
    assert (f"the {component} SVN, component byte {TURIN_COMPONENTS[component]}"
            in reason)


def test_the_milan_bytes_are_not_what_a_turin_floor_reads():
    """The defect: the Milan reading puts SNP in byte 6 and microcode in byte 7,
    so a Turin report with SNP 0x00 in byte 3 and 0x16 left in a reserved byte
    would pass a Milan-layout SNP floor. Read on the Turin layout it is refused."""
    raw = turin_bytes(fmc=0x02, bootloader=0x04, tee_svn=0x01, snp=0x00,
                      microcode=0x73, reserved=b"\x00\x00\x16")
    ok, reason = admits(turin_proof(raw), turin_requirement())
    assert not ok and "the SNP SVN, component byte 3" in reason


def test_reserved_turin_bytes_do_not_count_toward_the_floor():
    raw = turin_bytes(fmc=0x02, bootloader=0x04, tee_svn=0x01, snp=0x15,
                      microcode=0x73, reserved=b"\xff" * 3)
    ok, reason = admits(turin_proof(raw), turin_requirement())
    assert not ok and "the SNP SVN" in reason


def test_a_turin_report_is_refused_by_a_requirement_that_floors_only_milan():
    """A floor in the wrong layout is not a floor for this report: reading a Turin
    TEE SVN as a Milan reserved byte would compare nothing."""
    ok, reason = admits(turin_proof(TURIN_FLOOR_BYTES),
                        sev_requirement(sev_min_tcb=0))
    assert not ok
    assert "turin report" in reason and "no sev_turin_min_tcb" in reason


def test_a_milan_report_is_refused_by_a_requirement_that_floors_only_turin():
    ok, reason = admits(sev_proof(), turin_requirement(floor=0))
    assert not ok
    assert "milan-genoa report" in reason and "no sev_min_tcb" in reason


def test_one_requirement_can_floor_both_generations():
    both = sev_requirement(sev_min_tcb=0, sev_turin_min_tcb=TURIN_FLOOR)
    ok, reason = admits(turin_proof(TURIN_FLOOR_BYTES), both)
    assert ok, reason
    ok, reason = admits(sev_proof(), both)
    assert ok, reason


def test_a_zero_turin_floor_is_the_explicit_statement_of_no_minimum():
    ok, reason = admits(turin_proof(bytes(8)), turin_requirement(floor=0))
    assert ok, reason


@pytest.mark.parametrize("family, model", [(0x1A, 0x50), (0x1A, 0xC0), (0x00, 0x00)])
def test_a_report_of_an_unknown_generation_is_refused(family, model):
    """Venice and an unstamped report have no layout this root reads; comparing
    their TCB on Milan or Turin bytes would be a guess."""
    ok, reason = admits(
        turin_proof(TURIN_FLOOR_BYTES, cpuid_family=family, cpuid_model=model),
        sev_requirement(sev_turin_min_tcb=TURIN_FLOOR))
    assert not ok and "does not know" in reason


def test_an_unknown_generation_is_refused_even_under_a_zero_floor():
    ok, reason = admits(
        turin_proof(TURIN_FLOOR_BYTES, cpuid_family=0x1A, cpuid_model=0x50),
        sev_requirement(sev_min_tcb=0, sev_turin_min_tcb=0))
    assert not ok and "does not know" in reason


@pytest.mark.parametrize("byte", [4, 5, 6])
def test_a_turin_floor_with_a_reserved_byte_set_is_refused(byte):
    raw = bytearray(TURIN_FLOOR_BYTES)
    raw[byte] = 1
    with pytest.raises(TeeError, match=f"reserved byte {byte}.*turin"):
        turin_requirement(tcb_int(bytes(raw)))


@pytest.mark.parametrize("byte", [2, 3])
def test_a_turin_floor_may_set_the_bytes_milan_reserves(byte):
    """TEE and SNP live in bytes 2 and 3 on Turin, so a floor there is a real
    demand and the same value is refused as a Milan floor."""
    raw = bytearray(8)
    raw[byte] = 1
    assert turin_requirement(tcb_int(bytes(raw))).sev_turin_min_tcb
    with pytest.raises(TeeError, match=f"reserved byte {byte}.*milan-genoa"):
        sev_requirement(sev_min_tcb=tcb_int(bytes(raw)))


@pytest.mark.parametrize("value", [-1, 1 << 64, "1", True])
def test_a_turin_floor_is_an_unsigned_64_bit_number(value):
    with pytest.raises(TeeError):
        sev_requirement(sev_turin_min_tcb=value)


# -------------------------------------- the placement file spells the Turin floor


def test_the_placement_file_round_trips_the_turin_floor():
    table = {k: v for k, v in attest_table().items() if k != "runtime_measurements"}
    table.update(measurements=[SEV_MEASUREMENT], sev_min_tcb=0x0403,
                 sev_turin_min_tcb=TURIN_FLOOR)
    parsed, problem = plc.parse_tee_requirement(placement_file(table), "worker")
    assert problem is None, problem
    assert parsed is not None and parsed.sev_turin_min_tcb == TURIN_FLOOR
    text = plc.render_tee_requirement(parsed)
    again, problem = plc.parse_tee_requirement(tomllib.loads(text), "worker")
    assert problem is None, problem
    assert again == parsed
    assert plc.render_tee_requirement(again) == text


def test_the_placement_file_admits_a_turin_report_at_its_floor():
    table = {k: v for k, v in attest_table().items() if k != "runtime_measurements"}
    table.update(measurements=[SEV_MEASUREMENT], sev_turin_min_tcb=TURIN_FLOOR)
    ok, reason = plc.admit_peer_for_process(
        placement_file(table), "worker",
        offer(proof=turin_proof(TURIN_FLOOR_BYTES)),
        offer_key=PEER_KEY, root=hardware_root(), tee_ledger=set(), now=NOW)
    assert ok, reason


def test_the_placement_file_refuses_a_turin_floor_with_a_reserved_byte_set():
    table = {k: v for k, v in attest_table().items() if k != "runtime_measurements"}
    table.update(measurements=[SEV_MEASUREMENT], sev_turin_min_tcb=1 << 40)
    _, problem = plc.parse_tee_requirement(placement_file(table), "worker")
    assert problem and "reserved byte 5" in problem


def test_the_placement_file_refuses_a_turin_floor_that_is_not_an_integer():
    table = {k: v for k, v in attest_table().items() if k != "runtime_measurements"}
    table.update(measurements=[SEV_MEASUREMENT], sev_turin_min_tcb="0x02")
    _, problem = plc.parse_tee_requirement(placement_file(table), "worker")
    assert problem and "sev_turin_min_tcb" in problem and "integer" in problem

