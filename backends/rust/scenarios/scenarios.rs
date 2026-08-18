//! A1/G7 runtime scenarios: emitted components driven by the REAL cordis-rs
//! runtime (the crate from crates.io, not a stub). The Probe service records
//! every effect/undo/emission; assertions are on exact call order.
//!
//! Semantics pinned here (and documented in docs/v2.0-roadmap.md):
//! - G7: teardown runs inverses in reverse acquisition order.
//! - A8/L-Raise: a failing activation reverts the accumulated effects and
//!   lands FAILED.
//! - Reactive lifecycle: an Inject-gated consumer stays pending until its
//!   provider activates, and deactivates (running its inverses) when the
//!   provider is disposed.
//! - A1 divergence: cordis-rs 0.3.0 drives async activation to completion
//!   under the fiber transition lock, so a divert during `await` DEFERS until
//!   activation finishes — the post-boundary steps run, then everything
//!   reverts LIFO. (cordis-py diverts AT the boundary and skips them; the
//!   wasm tier physically rolls back.) The invariant that survives on every
//!   tier — and is asserted here under a concurrent-divert race loop — is
//!   no torn state: never a `do:` without its `undo:` after disposal.

use revl_scenarios::Probe;
use revl_scenarios::{boundary, fails_after, kv_consumer, kv_provider, realm_store_t, two_steps};
use revl_scenarios::{Kv, _revl_realm};
use std::sync::{Arc, Mutex};

struct Recorder {
    log: Arc<Mutex<Vec<String>>>,
}

impl Probe for Recorder {
    fn mark(&self, m: String) -> i64 {
        self.log.lock().unwrap().push(m);
        0
    }
    fn send(&self, m: String) -> i64 {
        self.log.lock().unwrap().push(format!("emit:{m}"));
        0
    }
}

fn root_with_probe() -> (cordis::Context, Arc<Mutex<Vec<String>>>) {
    let root = cordis::Context::new();
    let log = Arc::new(Mutex::new(Vec::new()));
    let probe: Box<dyn Probe> = Box::new(Recorder { log: log.clone() });
    root.provide("probe", probe).unwrap();
    (root, log)
}

fn marks(log: &Arc<Mutex<Vec<String>>>) -> Vec<String> {
    log.lock().unwrap().clone()
}

#[test]
fn g7_teardown_is_lifo() {
    let (root, log) = root_with_probe();
    let fiber = root.plugin(two_steps(), ());
    fiber.wait().unwrap();
    assert_eq!(marks(&log), ["do:a", "do:b"]);
    fiber.dispose().unwrap();
    assert_eq!(marks(&log), ["do:a", "do:b", "undo:b", "undo:a"],
        "inverses must run in reverse acquisition order (G7)");
}

#[test]
fn a8_fail_reverts_accumulated_effects() {
    let (root, log) = root_with_probe();
    let fiber = root.plugin(fails_after(), ());
    let result = fiber.wait();
    assert!(result.is_err(), "activation must surface the L-Raise");
    assert_eq!(marks(&log), ["do:a", "undo:a"],
        "the accumulated effect reverts before the fiber lands FAILED (A8)");
    assert_eq!(fiber.state(), cordis::FiberState::Failed);
}

#[test]
fn consumer_waits_for_provider_and_deactivates_on_withdrawal() {
    let (root, log) = root_with_probe();
    let consumer = root.plugin(kv_consumer(), ());
    // `kv` is not provided yet: the Inject gate must hold the consumer.
    std::thread::sleep(std::time::Duration::from_millis(50));
    assert!(marks(&log).is_empty(), "consumer must not activate before kv exists");

    let provider = root.plugin(kv_provider(), ());
    provider.wait().unwrap();
    consumer.wait().unwrap();
    assert_eq!(marks(&log), ["provider:up", "consumer:up"]);

    provider.dispose().unwrap();
    let after = marks(&log);
    assert!(after.contains(&"consumer:down".to_string()),
        "withdrawing kv must deactivate the consumer (reactive lifecycle): {after:?}");
    assert!(after.contains(&"provider:down".to_string()));
    let c = after.iter().position(|m| m == "consumer:down").unwrap();
    let p = after.iter().position(|m| m == "provider:down").unwrap();
    assert!(c < p, "consumer tears down before the provider it depends on: {after:?}");
}

#[test]
fn a1_boundary_completes_and_tears_down_lifo() {
    // Sequential form: activation crosses the await, the post-boundary
    // emission runs, disposal reverts LIFO.
    let (root, log) = root_with_probe();
    let fiber = root.plugin(boundary(), ());
    fiber.wait().unwrap();
    assert_eq!(marks(&log), ["do:a", "do:b", "emit:post"]);
    fiber.dispose().unwrap();
    assert_eq!(marks(&log), ["do:a", "do:b", "emit:post", "undo:b", "undo:a"]);
}

#[test]
fn a1_concurrent_divert_leaves_no_torn_state() {
    // cordis-rs serializes dispose against activation (block_on under the
    // transition lock), so a divert during the await defers. The cross-tier
    // invariant is torn-state freedom; race dispose against activation many
    // times and check it.
    for i in 0..50 {
        let (root, log) = root_with_probe();
        let fiber = root.plugin(boundary(), ());
        let disposer = {
            let fiber = fiber.clone();
            std::thread::spawn(move || {
                if i % 3 == 0 {
                    std::thread::yield_now();
                }
                let _ = fiber.dispose();
            })
        };
        let _ = fiber.wait();
        disposer.join().unwrap();
        let _ = fiber.dispose();

        let after = marks(&log);
        let dos = after.iter().filter(|m| m.starts_with("do:")).count();
        let undos = after.iter().filter(|m| m.starts_with("undo:")).count();
        assert_eq!(dos, undos,
            "every completed effect must be reverted after divert (iteration {i}): {after:?}");
        if after.iter().any(|m| m == "undo:a") && dos == 2 {
            let ub = after.iter().position(|m| m == "undo:b");
            let ua = after.iter().position(|m| m == "undo:a").unwrap();
            if let Some(ub) = ub {
                assert!(ub < ua, "LIFO order under divert (iteration {i}): {after:?}");
            }
        }
    }
}

#[test]
fn realm_label_lowering_is_stable_and_collision_free() {
    // The emitted lowering `_revl_realm` must map a label string to a STABLE
    // cordis Isolation: equal strings to one identity, distinct strings to
    // distinct identities, and never onto cordis's monotonic scope counter.
    const REALM_TAG: u64 = 0x8000_0000_0000_0000;

    // Deterministic, value-stable: the same label lowers identically.
    assert_eq!(_revl_realm("t"), _revl_realm("t"), "equal labels = one identity");
    assert_ne!(_revl_realm("t"), _revl_realm("other"), "distinct labels = distinct identity");

    // Disjoint from the framework counter: realm labels live in the reserved
    // top-bit region; cordis mints scopes from 1 upward in the low region.
    assert_ne!(_revl_realm("t").as_raw() & REALM_TAG, 0, "realm labels are top-bit tagged");
    let root = cordis::Context::new();
    for _ in 0..8 {
        let framework = root.new_isolation();
        assert_eq!(framework.as_raw() & REALM_TAG, 0, "counter stays in the low region");
        assert_ne!(framework, _revl_realm("t"), "no framework scope equals a realm label");
    }
}

#[test]
fn realm_labels_share_within_and_separate_across() {
    // The realm contract (docs/design-v2-realms.md) driven on the REAL
    // cordis-rs runtime: an emitted provider provides `kv` isolated in
    // realm("t"); a context branch keyed on the same label resolves it (equal
    // labels = same realm), a branch on a different label does not (distinct
    // labels = distinct realms). Uses the same `_revl_realm` lowering the
    // emitted plugin uses, so the provide side and the probe side agree only
    // if the lowering is deterministic.
    let (root, log) = root_with_probe();
    let store = root.plugin(realm_store_t(), ());
    store.wait().unwrap();
    assert!(marks(&log).contains(&"store_t:up".to_string()), "provider must activate");

    // Same label => same isolation slot: the provision is visible.
    let same = root.isolate_with("kv", _revl_realm("t"));
    let seen = same.get_unchecked::<Box<dyn Kv>>("kv").unwrap();
    assert!(
        seen.is_some(),
        "a branch naming realm(\"t\") must see the realm(\"t\") provision (equal labels = same realm)"
    );

    // Different label => disjoint slot: the provision is invisible.
    let other = root.isolate_with("kv", _revl_realm("other"));
    let unseen = other.get_unchecked::<Box<dyn Kv>>("kv").unwrap();
    assert!(
        unseen.is_none(),
        "a branch naming realm(\"other\") must NOT see the realm(\"t\") provision (distinct labels = distinct realms)"
    );
}
