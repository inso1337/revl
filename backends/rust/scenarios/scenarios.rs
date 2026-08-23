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
use revl_scenarios::{realm_kv_consumer_t, Kv, _revl_isolate_ctx, _revl_realm};
use revl_scenarios::{supervisor, Counter, Ctl};
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
    // The realm placement is applied at plug time (isolate BEFORE plugin), the
    // same lowering `_revl_load` uses — not inside the plugin body.
    let store = _revl_isolate_ctx(&root, "realm_store_t").plugin(realm_store_t(), ());
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

#[test]
fn isolated_consumer_reactively_links_to_isolated_provider_same_realm() {
    // The realm-isolation reactive-linking regression: a consumer that
    // `requires kv in realm("t")` and a provider that `isolate kv in
    // realm("t")` are plugged on SEPARATE isolated branches. The consumer must
    // start Pending (no kv yet) and reactively activate once the provider
    // supplies kv in the SAME realm. Before the emitter applied isolation at
    // plug time, the consumer's Inject gate was evaluated on the un-isolated
    // root context and it hung Pending forever.
    let (root, log) = root_with_probe();

    // Plug the consumer FIRST, isolated in realm("t"): its gate must hold it
    // because nothing provides kv in that realm yet.
    let consumer =
        _revl_isolate_ctx(&root, "realm_kv_consumer_t").plugin(realm_kv_consumer_t(), ());
    std::thread::sleep(std::time::Duration::from_millis(50));
    assert_eq!(
        consumer.state(),
        cordis::FiberState::Pending,
        "consumer must stay Pending until an in-realm provider exists"
    );
    assert!(
        !marks(&log).contains(&"consumer_t:up".to_string()),
        "consumer must not activate before the provider: {:?}",
        marks(&log)
    );

    // Now plug the isolated provider in the SAME realm("t").
    let provider = _revl_isolate_ctx(&root, "realm_store_t").plugin(realm_store_t(), ());
    provider.wait().unwrap();
    consumer.wait().unwrap();

    assert_eq!(
        consumer.state(),
        cordis::FiberState::Active,
        "consumer must reactively activate once kv is provided in realm(\"t\")"
    );
    let after = marks(&log);
    assert!(
        after.contains(&"consumer_t:up".to_string()),
        "consumer activation must have run its effect: {after:?}"
    );

    // Withdrawing the provider deactivates the consumer (reactive lifecycle).
    provider.dispose().unwrap();
    let after = marks(&log);
    assert!(
        after.contains(&"consumer_t:down".to_string()),
        "withdrawing the in-realm provider must deactivate the consumer: {after:?}"
    );
}

#[test]
fn spawn_instances_coexist_dispose_is_scoped_and_lifo() {
    // Instance-parametric components (docs/design-v2-instances.md) driven on
    // the REAL cordis-rs runtime. The Supervisor's activation body spawns two
    // Workers; each `spawn` isolates the Worker's `counter` provision into a
    // fresh LOCAL realm (`Context::isolate`, no label) and plugs it as a child
    // fiber of the supervisor. This proves the four properties by RUNNING:
    //   1. two live instances coexist in distinct local realms (non-colliding);
    //   2. disposing one runs ITS LIFO teardown and leaves the others live;
    //   3. a request-scoped instance is reclaimed at dispose(), NOT deferred to
    //      the parent component's teardown (the anti-leak property);
    //   4. supervision-tree addressing: the spawner reaches its instance, a
    //      sibling (here the root, standing in for any outside party) cannot.
    let (root, log) = root_with_probe();
    let supervisor = root.plugin(supervisor(), ());
    supervisor.wait().unwrap();

    // (1) Two live instances, each carrying the config that flowed through its
    // spawn: worker A (tag "a") then worker B (tag "b") each activated and ran
    // both effects. Their order is the spawn order in the activation body.
    let up = marks(&log);
    assert_eq!(
        up,
        ["up:a", "up2", "up:b", "up2"],
        "both instances must activate with their spawned configs: {up:?}"
    );

    // (1/4) Distinct local realms + supervision-tree addressing: neither
    // worker's `counter` provision escaped to the shared/root realm, so the
    // root — a stand-in for any sibling or outside party — cannot resolve it.
    // Two providers of the SAME key coexisting without a DuplicateService
    // collision is only possible because each lives in its own local realm.
    assert!(
        root.get_unchecked::<Box<dyn Counter>>("counter")
            .unwrap()
            .is_none(),
        "an instance's provision must stay private to its local realm (a sibling cannot reach it)"
    );
    assert_eq!(supervisor.state(), cordis::FiberState::Active);

    // (2/3/4) Retire worker A through the supervisor — the spawner reaching its
    // OWN instance through the handle it alone holds. Worker A's teardown runs
    // NOW and in LIFO order (`d2` before `d1:a`), not deferred to the
    // supervisor's teardown, and leaves the supervisor and worker B live.
    log.lock().unwrap().clear();
    let ctl = root
        .get_unchecked::<Box<dyn Ctl>>("ctl")
        .unwrap()
        .expect("the supervisor provides `ctl` in the shared realm");
    assert_eq!(ctl.retire_a(), 1);
    let after_retire = marks(&log);
    assert_eq!(
        after_retire,
        ["d2", "d1:a"],
        "worker A's own LIFO teardown must run at retire, not at supervisor teardown: {after_retire:?}"
    );
    assert_eq!(
        supervisor.state(),
        cordis::FiberState::Active,
        "the supervisor and the sibling instance stay live after retiring worker A"
    );

    // (3) The remaining worker (B) is reclaimed ONLY when the supervisor tears
    // down — the `ctx.plugin()` parent-effect safety net, so an un-disposed
    // instance never leaks past its spawner — and only it: worker A is already
    // gone, so its teardown does not run a second time (idempotent dispose).
    log.lock().unwrap().clear();
    supervisor.dispose().unwrap();
    let after_dispose = marks(&log);
    assert_eq!(
        after_dispose,
        ["d2", "d1:b"],
        "only worker B is reclaimed at supervisor teardown (A already gone): {after_dispose:?}"
    );
}
