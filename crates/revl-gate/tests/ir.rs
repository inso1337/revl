//! The IR-boundary decode guard's own integration tests (`cargo test`), run
//! with no Python in the loop (roadmap item 479).
//!
//! The crate refuses an unknown top-level field / unknown schema revision at
//! the IR boundary BY NAME, the mirror of the Python frontend. This drives the
//! public surface from a consumer's vantage; the cross-tier agreement — that
//! both tiers refuse the SAME shapes and derive the SAME known sets — is held
//! by `tests/test_gate_ir_boundary_drift.py`, which needs no toolchain.

use revl_gate::{check_ir_boundary, KNOWN_IR_FIELDS, KNOWN_IR_REVISIONS};

#[test]
fn a_known_document_passes_the_boundary() {
    assert!(check_ir_boundary(
        "{\"ir_version\": 3, \"services\": {}, \"components\": []}"
    )
    .is_ok());
}

#[test]
fn an_unknown_field_is_refused_by_name_with_the_wire_shape() {
    let refusal = check_ir_boundary("{\"services\": {}, \"surprise\": 1}").unwrap_err();
    assert_eq!(refusal.code, "IR_UNKNOWN_FIELD");
    assert!(refusal.message.contains("`surprise`"), "{}", refusal.message);
    assert!(
        refusal
            .message
            .contains("refuses an IR document with a field it does not know"),
        "{}",
        refusal.message
    );
    assert!(refusal.message.contains("known fields:"), "{}", refusal.message);
    // never an admission
    assert!(refusal.to_json().contains("\"admitted\":false"));
}

#[test]
fn an_unknown_revision_is_refused_naming_the_known_set() {
    let refusal = check_ir_boundary("{\"ir_version\": 99}").unwrap_err();
    assert_eq!(refusal.code, "IR_UNKNOWN_REVISION");
    assert!(refusal.message.contains("`ir_version` 99"), "{}", refusal.message);
    let known: Vec<String> = KNOWN_IR_REVISIONS.iter().map(|r| r.to_string()).collect();
    assert!(
        refusal.message.contains(&format!("known revisions: {}", known.join(", "))),
        "{}",
        refusal.message
    );
}

#[test]
fn a_non_object_document_fails_closed() {
    assert_eq!(check_ir_boundary("42").unwrap_err().code, "IR_MALFORMED");
    assert_eq!(check_ir_boundary("{").unwrap_err().code, "IR_MALFORMED");
}

#[test]
fn the_known_field_set_is_the_frontend_surface() {
    // a spot-check that the derived set carries the load-bearing members; the
    // exact set is pinned against the Python frontend in the drift test.
    assert!(KNOWN_IR_FIELDS.contains(&"ir_version"));
    assert!(KNOWN_IR_FIELDS.contains(&"manifest"));
    assert!(KNOWN_IR_FIELDS.contains(&"components"));
}
