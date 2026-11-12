"""A TDX quote is judged on its runtime registers, TCB and quoting enclave
(issue #3156).

The verifier parsed RTMR0..3, ``tee_tcb_svn``, ``qe_svn``, ``pce_svn`` and the QE
report's ``mr_signer``/``mr_enclave``, and ``tee_admits`` compared none of them.
A TD whose MRTD was permitted was admitted whatever the kernel, root file system
and application measured into the runtime registers, with any TCB and any quoting
enclave, and the SEV-SNP path ignored its TCB the same way.

Each refusal below is one line of the issue's probe, built with the module's own
attester and the pinned root the hardware tests already use; the controls in
those tests refuse, so these builders can produce a refusal and only the fields
the verifier never read changed nothing. The last group pins the fail-closed
default: a TDX quote is refused unless the requirement states a TDX policy, so an
operator cannot get MRTD-only acceptance by saying nothing.
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
from revl.tee_attestation import (  # noqa: E402
    TeeError,
    TeeRequirement,
    TdxPolicy,
    build_sev_snp_evidence,
)
from test_tee_hardware_root import (  # noqa: E402
    BUNDLE,
    MRTD,
    NOW,
    NONCE,
    PEER_KEY,
    REGION,
    SEV_MEASUREMENT,
    SEV_VCEK,
    SEV_VCEK_ID,
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

# A TD's runtime-register quadruple: the four runtime-extended registers hold the
# kernel, root file system and application the TD loaded, as the TDX module
# extends them.
RTMRS = ("11" * 48, "22" * 48, "33" * 48, "44" * 48)
ARBITRARY_RTMRS = ("aa" * 48, "bb" * 48, "cc" * 48, "dd" * 48)

QE_SIGNER = "55" * 32
QE_ENCLAVE = "66" * 32
OTHER_QE_SIGNER = "77" * 32
OTHER_QE_ENCLAVE = "88" * 32

TEE_TCB_FLOOR = "01" + "00" * 15


def rtmr_bytes(values: tuple[str, ...]) -> tuple[bytes, ...]:
    """The register tuple `build_tdx_quote` takes, from its hex spelling."""
    return tuple(bytes.fromhex(value) for value in values)


def tdx_policy(**overrides) -> TdxPolicy:
    fields: dict = {"runtime_measurements": frozenset({RTMRS})}
    fields.update(overrides)
    return TdxPolicy(**fields)


def tdx_requirement(**overrides) -> TeeRequirement:
    fields: dict = {"tdx": tdx_policy()}
    fields.update(overrides)
    return requirement(**fields)


# ------------------------------------- the runtime registers, compared at last


def test_a_tdx_quote_with_the_permitted_runtime_registers_is_admitted():
    ok, reason = admits(tdx_proof(quote_rtmrs=rtmr_bytes(RTMRS)),
                        tdx_requirement())
    assert ok, reason


def test_a_tdx_quote_is_refused_when_the_requirement_names_no_tdx_policy():
    """The headline: MRTD is the TD's initial build, not the workload it runs.
    A requirement that says nothing about the runtime registers refuses the
    quote rather than admitting it on MRTD alone (the issue's first probe line).
    """
    silent = TeeRequirement(bundle=BUNDLE, measurements=frozenset({MRTD}),
                            regions=frozenset({REGION}), nonce=NONCE,
                            max_age_s=120.0)
    ok, reason = admits(tdx_proof(), silent)
    assert not ok
    assert "MRTD" in reason and "runtime" in reason


def test_a_tdx_quote_with_arbitrary_runtime_registers_is_refused():
    """`TDX arbitrary RTMR0-3 (kernel/rootfs/app regs)` was admitted before this
    issue: the MRTD was permitted, so whatever the TD loaded afterwards passed.
    """
    ok, reason = admits(tdx_proof(quote_rtmrs=rtmr_bytes(ARBITRARY_RTMRS)),
                        tdx_requirement())
    assert not ok
    assert "runtime registers" in reason
    for value in ARBITRARY_RTMRS:
        assert value in reason


def test_mrtd_only_acceptance_must_be_stated_explicitly():
    """The narrower claim stays available, by name, so a deployment that knows
    what it is giving up can still take it. It is not the default."""
    ok, reason = admits(tdx_proof(quote_rtmrs=rtmr_bytes(ARBITRARY_RTMRS)),
                        requirement(tdx=TdxPolicy(accept_mrtd_only=True)))
    assert ok, reason


# ------------------------------------------------------- the TCB, floored by name


def test_a_tdx_quote_below_the_tee_tcb_svn_floor_is_refused():
    ok, reason = admits(
        tdx_proof(quote_rtmrs=rtmr_bytes(RTMRS),
                  quote_tee_tcb_svn=b"\x00" * 16),
        tdx_requirement(tdx=tdx_policy(min_tee_tcb_svn=TEE_TCB_FLOOR)))
    assert not ok
    assert "tee_tcb_svn" in reason and "below" in reason


def test_a_tdx_quote_with_svns_below_the_floor_is_refused():
    """`TDX tee_tcb_svn=ff*16 / qe_svn=0 / pce_svn=0` was admitted before this
    issue: the SVNs were parsed out of the header and never read."""
    ok, reason = admits(
        tdx_proof(quote_rtmrs=rtmr_bytes(RTMRS),
                  quote_tee_tcb_svn=b"\xff" * 16,
                  quote_qe_svn=0, quote_pce_svn=0),
        tdx_requirement(tdx=tdx_policy(min_qe_svn=2, min_pce_svn=3)))
    assert not ok
    assert ("qe_svn" in reason or "pce_svn" in reason) and "below" in reason


def test_a_tdx_quote_at_the_tcb_floor_is_admitted():
    ok, reason = admits(
        tdx_proof(quote_rtmrs=rtmr_bytes(RTMRS),
                  quote_tee_tcb_svn=bytes.fromhex(TEE_TCB_FLOOR),
                  quote_qe_svn=2, quote_pce_svn=3),
        tdx_requirement(tdx=tdx_policy(min_tee_tcb_svn=TEE_TCB_FLOOR,
                                       min_qe_svn=2, min_pce_svn=3)))
    assert ok, reason


# --------------------------------------------------- the quoting enclave, pinned


def test_a_tdx_quote_from_an_unpermitted_quoting_enclave_signer_is_refused():
    ok, reason = admits(
        tdx_proof(quote_rtmrs=rtmr_bytes(RTMRS),
                  quote_qe_mr_signer=bytes.fromhex(OTHER_QE_SIGNER)),
        tdx_requirement(tdx=tdx_policy(qe_mr_signers=frozenset({QE_SIGNER}))))
    assert not ok
    assert "quoting enclave" in reason and "mr_signer" in reason


def test_a_tdx_quote_from_an_unpermitted_quoting_enclave_image_is_refused():
    ok, reason = admits(
        tdx_proof(quote_rtmrs=rtmr_bytes(RTMRS),
                  quote_qe_mr_enclave=bytes.fromhex(OTHER_QE_ENCLAVE)),
        tdx_requirement(tdx=tdx_policy(qe_mr_enclaves=frozenset({QE_ENCLAVE}))))
    assert not ok
    assert "quoting enclave" in reason and "mr_enclave" in reason


def test_a_tdx_quote_from_the_permitted_quoting_enclave_is_admitted():
    ok, reason = admits(
        tdx_proof(quote_rtmrs=rtmr_bytes(RTMRS),
                  quote_qe_mr_signer=bytes.fromhex(QE_SIGNER),
                  quote_qe_mr_enclave=bytes.fromhex(QE_ENCLAVE)),
        tdx_requirement(tdx=tdx_policy(
            qe_mr_signers=frozenset({QE_SIGNER}),
            qe_mr_enclaves=frozenset({QE_ENCLAVE}))))
    assert ok, reason


# ------------------------------------------------- the SEV-SNP TCB, floored too


def test_a_sev_snp_report_below_the_tcb_floor_is_refused():
    """`SEV current_tcb/reported_tcb = 0, platform_info=0` was admitted before
    this issue."""
    ok, reason = admits(
        sev_proof(),
        requirement(measurements=frozenset({SEV_MEASUREMENT}), sev_min_tcb=1))
    assert not ok
    assert "TCB" in reason and "below" in reason


def test_a_sev_snp_report_at_the_tcb_floor_is_admitted():
    proof = build_sev_snp_evidence(
        evidence(measurement=SEV_MEASUREMENT), vcek_private_key=SEV_VCEK,
        platform_key_id=SEV_VCEK_ID, current_tcb=1, reported_tcb=1)
    ok, reason = admits(
        proof,
        requirement(measurements=frozenset({SEV_MEASUREMENT}), sev_min_tcb=1))
    assert ok, reason


def test_a_sev_snp_report_below_the_tee_svn_is_refused():
    """The floor is compared on every SVN component, not only the low byte: the
    TEE SVN lives in byte 1 of the TCB version."""
    ok, reason = admits(
        sev_proof(),
        requirement(measurements=frozenset({SEV_MEASUREMENT}),
                    sev_min_tcb=0x0100))
    assert not ok
    assert "TCB" in reason and "below" in reason


def test_reserved_tcb_bits_cannot_satisfy_a_floor():
    """Byte 2 of the TCB version is reserved. A report that sets it must not slip
    past a floor the real SVN components do not meet."""
    proof = build_sev_snp_evidence(
        evidence(measurement=SEV_MEASUREMENT), vcek_private_key=SEV_VCEK,
        platform_key_id=SEV_VCEK_ID, current_tcb=1 << 16, reported_tcb=1 << 16)
    ok, reason = admits(
        proof,
        requirement(measurements=frozenset({SEV_MEASUREMENT}),
                    sev_min_tcb=0x0100))
    assert not ok
    assert "TCB" in reason


def test_a_high_sev_svn_does_not_offset_a_low_one():
    """The floor is per component, not one number: a report with bootloader 0 and
    TEE 2 must not pass a floor of bootloader 1 and TEE 1 just because its packed
    value is larger."""
    proof = build_sev_snp_evidence(
        evidence(measurement=SEV_MEASUREMENT), vcek_private_key=SEV_VCEK,
        platform_key_id=SEV_VCEK_ID, current_tcb=0x0200, reported_tcb=0x0200)
    ok, reason = admits(
        proof,
        requirement(measurements=frozenset({SEV_MEASUREMENT}),
                    sev_min_tcb=0x0101))
    assert not ok
    assert "below" in reason and "component byte 0" in reason


# A SEV-SNP TCB_VERSION as a Milan or Genoa report carries it, in report byte
# order: bootloader (byte 0), TEE (byte 1), four reserved bytes, SNP (byte 6),
# microcode (byte 7). The floor an operator writes is the same eight bytes read
# as a little-endian integer, the way AMD's tools print it, so microcode is the
# most significant byte.
COMPONENT_BYTES = {"bootloader": 0, "TEE": 1, "SNP": 6, "microcode": 7}


def tcb_bytes(bootloader, tee_svn, snp, microcode, reserved=b"\x00" * 4):
    return bytes([bootloader, tee_svn]) + reserved + bytes([snp, microcode])


def tcb_value(raw):
    return int.from_bytes(raw, "little")


FLOOR_BYTES = tcb_bytes(bootloader=0x04, tee_svn=0x01, snp=0x16, microcode=0x73)
FLOOR = 0x7316000000000104


def sev_proof_at(raw):
    """A SEV-SNP proof whose current_tcb and reported_tcb carry ``raw``, checked
    against the report bytes the verifier reads (offsets 0x038 and 0x180)."""
    proof = build_sev_snp_evidence(
        evidence(measurement=SEV_MEASUREMENT), vcek_private_key=SEV_VCEK,
        platform_key_id=SEV_VCEK_ID, current_tcb=tcb_value(raw),
        reported_tcb=tcb_value(raw))
    report = bytes.fromhex(proof[tee.QUOTE_FIELD])
    assert report[0x038:0x040] == raw and report[0x180:0x188] == raw
    return proof


def sev_floor(floor):
    return requirement(measurements=frozenset({SEV_MEASUREMENT}),
                       sev_min_tcb=floor)


def test_the_example_floor_is_the_layout_spelled_as_an_integer():
    assert tcb_value(FLOOR_BYTES) == FLOOR


def test_a_sev_snp_report_at_every_svn_floor_is_admitted():
    ok, reason = admits(sev_proof_at(FLOOR_BYTES), sev_floor(FLOOR))
    assert ok, reason


@pytest.mark.parametrize("component", sorted(COMPONENT_BYTES))
def test_a_report_below_the_floor_in_one_component_is_refused(component):
    """One below the floor in ``component`` and one above it in every other: the
    others cannot offset it, so a higher bootloader does not hide an older
    microcode, and the refusal names the component."""
    raw = bytearray(FLOOR_BYTES)
    for name, byte in COMPONENT_BYTES.items():
        raw[byte] += -1 if name == component else 1
    ok, reason = admits(sev_proof_at(bytes(raw)), sev_floor(FLOOR))
    assert not ok
    assert "below" in reason
    assert f"the {component} SVN, component byte {COMPONENT_BYTES[component]}" \
        in reason


@pytest.mark.parametrize("floor, component", [
    (0x0100000000000000, "microcode"),
    (0x0005000000000000, "SNP"),
])
def test_a_report_with_tcb_zero_is_refused_by_a_microcode_or_snp_floor(
        floor, component):
    """The review's probes: a report whose TCB is 0 was admitted against both."""
    ok, reason = admits(sev_proof_at(bytes(8)), sev_floor(floor))
    assert not ok
    assert f"the {component} SVN" in reason


def test_reserved_report_bytes_do_not_count_toward_the_floor():
    raw = tcb_bytes(bootloader=0x04, tee_svn=0x01, snp=0x15, microcode=0x73,
                    reserved=b"\xff" * 4)
    ok, reason = admits(sev_proof_at(raw), sev_floor(FLOOR))
    assert not ok
    assert "the SNP SVN" in reason


@pytest.mark.parametrize("byte", [2, 3, 4, 5])
def test_a_floor_with_a_reserved_byte_set_is_refused(byte):
    raw = bytearray(FLOOR_BYTES)
    raw[byte] = 1
    with pytest.raises(TeeError, match=f"reserved byte {byte}"):
        sev_floor(tcb_value(bytes(raw)))


def test_the_placement_file_refuses_a_floor_with_a_reserved_byte_set():
    table = attest_table(sev_min_tcb=0x0403000201)
    _, problem = plc.parse_tee_requirement(placement_file(table), "worker")
    assert problem and "reserved byte 3" in problem


# ------------------------------------ a demand cannot be met by another format


def test_a_tdx_register_policy_cannot_be_met_by_a_sev_snp_report():
    ok, reason = admits(
        sev_proof(),
        requirement(measurements=frozenset({SEV_MEASUREMENT}), tdx=tdx_policy()))
    assert not ok
    assert "TDX" in reason and "SEV-SNP" in reason


def test_a_sev_tcb_floor_cannot_be_met_by_a_tdx_quote():
    ok, reason = admits(tdx_proof(quote_rtmrs=rtmr_bytes(RTMRS)),
                        tdx_requirement(sev_min_tcb=1))
    assert not ok
    assert "SEV-SNP" in reason


def test_a_tdx_register_policy_cannot_be_met_by_a_mac_record():
    dev = tee.sign_evidence(evidence(), b"an attestation authority key")
    ok, reason = admits(dev, tdx_requirement(), root=None,
                        attester_key=b"an attestation authority key")
    assert not ok
    assert "symmetric-MAC" in reason or "hardware" in reason


# ----------------------------------------- the policy is validated when it is built


def test_a_tdx_policy_must_name_rtmrs_or_accept_mrtd_only():
    with pytest.raises(TeeError, match="accept_mrtd_only"):
        TdxPolicy()
    with pytest.raises(TeeError, match="contradictory"):
        TdxPolicy(runtime_measurements=frozenset({RTMRS}),
                  accept_mrtd_only=True)


@pytest.mark.parametrize("fields", [
    {"runtime_measurements": frozenset({("11" * 48, "22" * 48, "33" * 48)})},
    {"runtime_measurements": frozenset({RTMRS[:3] + ("zz" * 48,)})},
    {"runtime_measurements": frozenset({("11" * 32, "22" * 48, "33" * 48,
                                         "44" * 48)})},
    {"runtime_measurements": frozenset({RTMRS}), "min_tee_tcb_svn": "01" * 8},
    {"runtime_measurements": frozenset({RTMRS}),
     "qe_mr_signers": frozenset({"11" * 16})},
    {"runtime_measurements": frozenset({RTMRS}),
     "qe_mr_enclaves": frozenset({"not-hex"})},
])
def test_a_tdx_policy_refuses_a_malformed_field(fields):
    with pytest.raises(TeeError):
        TdxPolicy(**fields)


@pytest.mark.parametrize("overrides", [
    {"min_qe_svn": -1},
    {"min_pce_svn": 0x10000},
    {"min_qe_svn": "3"},
    {"min_pce_svn": True},
])
def test_a_tdx_policy_refuses_a_svn_outside_the_header_width(overrides):
    with pytest.raises(TeeError):
        tdx_policy(**overrides)


@pytest.mark.parametrize("value", [-1, 1 << 64, "1", True])
def test_a_sev_tcb_floor_is_an_unsigned_64_bit_number(value):
    with pytest.raises(TeeError):
        requirement(sev_min_tcb=value)


# --------------------------------------------- the placement file can spell it


def test_the_placement_file_can_state_the_runtime_registers():
    table = attest_table(runtime_measurements=[list(RTMRS)])
    ok, reason = plc.admit_peer_for_process(
        placement_file(table), "worker",
        offer(proof=tdx_proof(quote_rtmrs=rtmr_bytes(RTMRS))),
        offer_key=PEER_KEY, root=hardware_root(), tee_ledger=set(), now=NOW)
    assert ok, reason


def test_the_placement_file_without_a_tdx_policy_refuses_a_tdx_quote():
    """Fail-closed at the file surface too: the default spelling names no TDX
    policy, so it does not silently admit a quote on MRTD alone."""
    table = {k: v for k, v in attest_table().items()
             if k != "runtime_measurements"}
    ok, reason = plc.admit_peer_for_process(
        placement_file(table), "worker", offer(proof=tdx_proof()),
        offer_key=PEER_KEY, root=hardware_root(), tee_ledger=set(), now=NOW)
    assert not ok
    assert "MRTD" in reason and "runtime" in reason


def test_the_placement_file_can_accept_mrtd_only_explicitly():
    table = {k: v for k, v in attest_table().items()
             if k != "runtime_measurements"}
    table["accept_mrtd_only"] = True
    ok, reason = plc.admit_peer_for_process(
        placement_file(table), "worker", offer(proof=tdx_proof()),
        offer_key=PEER_KEY, root=hardware_root(), tee_ledger=set(), now=NOW)
    assert ok, reason


@pytest.mark.parametrize("overrides", [
    {"runtime_measurements": [["11" * 48] * 3]},
    {"runtime_measurements": [["11" * 48] * 4], "accept_mrtd_only": "yes"},
    {"runtime_measurements": [["11" * 48] * 4], "accept_mrtd_only": True},
    {"runtime_measurements": [["11" * 48] * 4], "min_qe_svn": "3"},
    {"min_qe_svn": 3},
    {"sev_min_tcb": -1},
])
def test_the_placement_file_refuses_a_malformed_tdx_policy(overrides):
    table = {k: v for k, v in attest_table().items()
             if k != "runtime_measurements"}
    table.update(overrides)
    _, problem = plc.parse_tee_requirement(placement_file(table), "worker")
    assert problem, "a malformed TDX policy must be refused by name"


def test_the_placement_file_round_trips_the_tdx_policy():
    table = attest_table(runtime_measurements=[list(RTMRS)],
                         min_tee_tcb_svn=TEE_TCB_FLOOR,
                         min_qe_svn=2, min_pce_svn=3,
                         qe_mr_signers=[QE_SIGNER],
                         qe_mr_enclaves=[QE_ENCLAVE],
                         sev_min_tcb=1)
    parsed, problem = plc.parse_tee_requirement(
        placement_file(table), "worker")
    assert problem is None, problem
    assert parsed is not None
    assert parsed.tdx == TdxPolicy(
        runtime_measurements=frozenset({RTMRS}),
        min_tee_tcb_svn=TEE_TCB_FLOOR, min_qe_svn=2, min_pce_svn=3,
        qe_mr_signers=frozenset({QE_SIGNER}),
        qe_mr_enclaves=frozenset({QE_ENCLAVE}))
    assert parsed.sev_min_tcb == 1
    text = plc.render_tee_requirement(parsed)
    again, problem = plc.parse_tee_requirement(tomllib.loads(text), "worker")
    assert problem is None, problem
    assert again == parsed
    assert plc.render_tee_requirement(again) == text
