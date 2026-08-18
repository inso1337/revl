//! cordis-rs realm-conformance scenarios (driven by
//! tests/test_realm_conformance.py). The emitted crate exposes the three
//! separately-compiled providers as plugin fns; these tests drive them on the
//! real cordis-rs runtime.
//!
//! H (sharing/conflict): SharedStoreA and SharedStoreB both isolate `kv` into
//!   realm("shared") -> `isolate_with("kv", _revl_realm("shared"))` yields the
//!   same Isolation, so the second provider of `kv` in that realm must be
//!   refused (its activation fails / it does not land Active). Equal strings =
//!   same realm (docs/design-v2-realms.md).
//! S (separation): realm("shared") vs realm("other") are distinct realms ->
//!   both active, and disposing one leaves the other untouched.

use revl_scenarios::{shared_store_a, shared_store_b, shared_store_other, _revl_isolate_ctx};

#[test]
fn h_same_realm_second_provider_is_refused() {
    let root = cordis::Context::new();
    let a = _revl_isolate_ctx(&root, "shared_store_a").plugin(shared_store_a(), ());
    a.wait().unwrap();
    assert_eq!(a.state(), cordis::FiberState::Active, "first provider must activate");

    let b = _revl_isolate_ctx(&root, "shared_store_b").plugin(shared_store_b(), ());
    let result = b.wait();
    assert!(
        result.is_err() || b.state() != cordis::FiberState::Active,
        "equal realm strings denote the SAME realm, so the second provider of \
         `kv` in realm(\"shared\") must be REFUSED (G2 per-(key,realm)); \
         instead it landed {:?}",
        b.state()
    );
}

#[test]
fn s_distinct_realms_are_separate_and_dispose_independent() {
    let root = cordis::Context::new();
    let a = _revl_isolate_ctx(&root, "shared_store_a").plugin(shared_store_a(), ());
    let other = _revl_isolate_ctx(&root, "shared_store_other").plugin(shared_store_other(), ());
    a.wait().unwrap();
    other.wait().unwrap();
    assert_eq!(a.state(), cordis::FiberState::Active);
    assert_eq!(other.state(), cordis::FiberState::Active, "distinct realms must both activate");

    a.dispose().unwrap();
    assert_eq!(
        other.state(),
        cordis::FiberState::Active,
        "disposing the realm(\"shared\") provider must not affect realm(\"other\")"
    );
}
