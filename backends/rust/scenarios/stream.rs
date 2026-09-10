//! item 130 Slice 3 exit test on the rust tier — the EMITTED stream components
//! (from scenarios/stream.rvl) driven by the REAL cordis-rs runtime.
//! docs/design/130-stream-reactive-types.md §4.6 (the rust row), §9 Parts A and
//! B, §10.2 / §10.4 / §10.5.
//!
//! This tier ERASES the async color: `next` blocks on a race between the item
//! queue and the subscription's CANCEL signal, and `close` trips that signal.
//! The whole point of the race is that the bracket inverse stays reachable off
//! the teardown thread, so a `next` parked on a provider that never emits can
//! neither deadlock teardown nor leak.

use revl_stream_scn::{
    consumer, fanin, handler, iterate, parked, revl_stream_live_subscriptions,
    revl_stream_marks, revl_stream_pending, revl_stream_providers, revl_stream_reset, Sink,
    Stream, StreamNext, STREAM_BUFFER_CAPACITY,
};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

fn reset() {
    revl_stream_reset();
}

fn count_mark(marks: &[String], want: &str) -> usize {
    marks.iter().filter(|m| m.as_str() == want).count()
}

fn mark_index(marks: &[String], want: &str) -> Option<usize> {
    marks.iter().position(|m| m == want)
}

/// Spin until `cond` holds or the budget runs out. Used only to observe another
/// thread reaching its park; every assertion below is on a state that is stable
/// once reached.
fn wait_for(what: &str, cond: impl Fn() -> bool) {
    let deadline = Instant::now() + Duration::from_secs(3);
    while Instant::now() < deadline {
        if cond() {
            return;
        }
        std::thread::sleep(Duration::from_millis(1));
    }
    panic!("timed out waiting for {}", what);
}

/// Load an ASYNC component on its own thread and report the outcome.
///
/// This tier erases the async color, so a `next` in an activation body BLOCKS
/// the loading thread until an item or a terminal arrives (design §4.6, the
/// rust row) — cordis-rs drives the plugin future on the caller. Loading from a
/// worker is therefore not test scaffolding but the shape the tier promises:
/// the park occupies one thread, and the provider terminal (or the owner's
/// close) arrives on ANOTHER — which is precisely why the bracket inverse stays
/// reachable.
fn drive(
    what: &'static str,
    component: fn() -> cordis::PluginHandle,
) -> std::sync::mpsc::Receiver<String> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let root = cordis::Context::new();
        let p = root.plugin(component(), ());
        let outcome = match p.try_wait() {
            Ok(_) => format!("{} activated", what),
            Err(e) => format!("{:?}", e),
        };
        let _ = tx.send(outcome);
    });
    rx
}

/// The same, but the worker also UNLOADS the component once it activates, so
/// the teardown loop runs on the thread that owned the activation. Reports
/// "disposed" once the inverses have run.
fn drive_and_dispose(
    what: &'static str,
    component: fn() -> cordis::PluginHandle,
) -> std::sync::mpsc::Receiver<String> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let root = cordis::Context::new();
        let p = root.plugin(component(), ());
        let outcome = match p.try_wait() {
            Err(e) => format!("{} failed to activate: {:?}", what, e),
            Ok(_) => match p.dispose() {
                Err(e) => format!("{} dispose failed: {:?}", what, e),
                Ok(_) => String::from("disposed"),
            },
        };
        let _ = tx.send(outcome);
    });
    rx
}

// -------------------------------------------------------------------------
// §10.2 — THE CORE GUARANTEE: unloading the owner CLOSES the stream, LIFO.
// -------------------------------------------------------------------------

#[test]
fn unload_closes_the_stream_lifo() {
    reset();
    let root = cordis::Context::new();
    let c = root.plugin(consumer(), ());
    c.try_wait().expect("Consumer did not reach ACTIVE");

    assert_eq!(
        revl_stream_live_subscriptions(),
        1,
        "the subscription is live after activation"
    );

    c.dispose().expect("Consumer dispose failed");

    let marks = revl_stream_marks();
    let sub_close = mark_index(&marks, "stream.close").expect("subscription never closed");
    let src_close =
        mark_index(&marks, "stream.source close").expect("provider never closed");
    // LIFO: the subscription (acquired last) closes first, then the source it
    // listens to. The pool acquired before either is on the same stack.
    assert!(
        sub_close < src_close,
        "teardown was not LIFO: {:?}",
        marks
    );
    assert_eq!(
        revl_stream_pending(),
        0,
        "no listener outlived the owner: {:?}",
        marks
    );
}

// -------------------------------------------------------------------------
// §9 Part A — a parked `next` is ALWAYS terminated by the owner's own close.
// The call driven here is exactly what the emitted bracket inverse runs
// (`ctx.effect("…", move || { sub_undo.close(); Ok(()) })`, pinned in
// backends/rust/test_emit_rust.py), from a DIFFERENT thread — which is where
// the teardown loop runs it.
// -------------------------------------------------------------------------

#[test]
fn parked_next_is_terminated_by_the_owners_close() {
    reset();
    let src = Stream::source();
    let sub = Stream::subscribe(&src, "error", 0);

    let parked_sub = sub.clone();
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(parked_sub.next());
    });

    // The consumer is parked: the provider never emits and never terminates.
    assert!(
        rx.recv_timeout(Duration::from_millis(50)).is_err(),
        "next resolved with no item and no terminal"
    );

    // Teardown: the bracket inverse. It must return IMMEDIATELY — it never
    // waits for the parked next to drain — and it must resolve that park.
    let started = Instant::now();
    assert!(sub.close(), "the bracket inverse must run once");
    assert!(
        started.elapsed() < Duration::from_secs(1),
        "close waited on the parked next — delivery-behind, not cancellation-first"
    );

    match rx.recv_timeout(Duration::from_secs(3)) {
        Ok(Ok(StreamNext::Closed)) => {}
        other => panic!(
            "the parked next was not resolved as Closed by the owner's close: {:?}",
            other
        ),
    }

    assert!(!sub.close(), "close must be idempotent");
    src.close();
    assert_eq!(revl_stream_pending(), 0, "residue after teardown");
}

/// A `close` that races a buffered item still wins: cancellation-first means the
/// cancel signal is checked BEFORE the buffer, so a withdrawn owner never
/// observes one more item after its teardown began.
#[test]
fn close_wins_over_a_buffered_item() {
    reset();
    let src = Stream::source();
    let sub = Stream::subscribe(&src, "error", 0);
    src.emit(String::from("buffered"));
    sub.close();
    assert_eq!(sub.next(), Ok(StreamNext::Closed));
    src.close();
    assert_eq!(revl_stream_pending(), 0);
}

// -------------------------------------------------------------------------
// §9 Part B — provider death is a terminal, never silence, and it reaches an
// outstanding `next` end-to-end through a real activation.
// -------------------------------------------------------------------------

#[test]
fn provider_fault_terminates_a_parked_next_and_closes_the_bracket() {
    reset();
    let rx = drive("Parked", parked);

    // The activation body parks in the item/terminal/cancel race — and it parks
    // its whole LOADING THREAD, which is the erasure this tier promises (A1
    // family 2, async-extern.md §2). Nothing has completed.
    wait_for("the activation to park in next", || {
        revl_stream_live_subscriptions() == 1
    });
    assert!(
        rx.recv_timeout(Duration::from_millis(50)).is_err(),
        "the activation completed without an item or a terminal"
    );

    // The provider aborts. Its terminal must reach the parked `next`, from a
    // DIFFERENT thread than the one the park occupies.
    let providers = revl_stream_providers();
    assert_eq!(providers.len(), 1, "one provider");
    providers[0].fault(String::from("provider gone"));

    let outcome = rx
        .recv_timeout(Duration::from_secs(5))
        .expect("the parked next was never terminated by the provider's fault");
    assert!(
        outcome.contains("provider gone"),
        "a Faulted terminal must fail the activation with its reason: {}",
        outcome
    );

    // The failed activation reverted its prefix LIFO, so the subscription
    // bracket closed — a fault never leaves a subscription active.
    let marks = revl_stream_marks();
    assert!(
        mark_index(&marks, "stream.close").is_some(),
        "the failed activation did not close the subscription: {:?}",
        marks
    );
    assert_eq!(
        revl_stream_pending(),
        0,
        "residue after the fault: {:?}",
        marks
    );
}

/// The orderly twin: the provider emits, the parked activation carries on, and
/// the ordinary unload path closes the stream.
#[test]
fn parked_next_resumes_on_an_item() {
    reset();
    let rx = drive_and_dispose("Parked", parked);
    wait_for("the activation to park in next", || {
        revl_stream_live_subscriptions() == 1
    });
    revl_stream_providers()[0].emit(String::from("order-1"));

    let outcome = rx
        .recv_timeout(Duration::from_secs(5))
        .expect("an emitted item never unparked the activation");
    assert_eq!(outcome, "disposed", "{}", outcome);
    assert_eq!(revl_stream_pending(), 0, "residue after unload");
}

// -------------------------------------------------------------------------
// `merge` — multi-source teardown, and the two fan-in terminal rules.
// -------------------------------------------------------------------------

#[test]
fn merge_multi_source_teardown_is_one_lifo_stack() {
    reset();
    let rx = drive_and_dispose("Fanin", fanin);
    wait_for("the fan-in consumer to park", || {
        revl_stream_live_subscriptions() == 1
    });

    // An item from EITHER source reaches the one consumer.
    let providers = revl_stream_providers();
    assert_eq!(
        providers.len(),
        3,
        "two sources plus the derived merge the subscription owns"
    );
    providers[1].emit(String::from("from-b"));

    let outcome = rx
        .recv_timeout(Duration::from_secs(5))
        .expect("an item from the second source never reached the merged consumer");
    assert_eq!(outcome, "disposed", "{}", outcome);

    let marks = revl_stream_marks();
    let sub_close = mark_index(&marks, "stream.close").expect("subscription never closed");
    let merge_close =
        mark_index(&marks, "stream.merge close").expect("the fan-in never closed");
    // LIFO: the subscription, then the merge it OWNS, then the two sources —
    // neither of which is left holding the merged stream.
    assert!(
        sub_close < merge_close,
        "the merge closed before its subscriber: {:?}",
        marks
    );
    let source_closes = marks.iter().filter(|m| *m == "stream.source close").count();
    assert_eq!(source_closes, 2, "both sources closed: {:?}", marks);
    for (i, m) in marks.iter().enumerate() {
        if m == "stream.source close" {
            assert!(
                i > merge_close,
                "a source closed before the merge detached from it: {:?}",
                marks
            );
        }
    }
    assert_eq!(revl_stream_pending(), 0, "fan-in residue: {:?}", marks);
}

/// One source closing does NOT strand the consumer on the other, and the LAST
/// source closing does deliver the merged `Closed` — so a parked next on a
/// fan-in is always terminated.
#[test]
fn merge_closed_is_delivered_only_when_every_source_is_done() {
    reset();
    let a = Stream::source();
    let b = Stream::source();
    let m = Stream::merge(&a, &b);
    let sub = Stream::subscribe(&m, "error", 0);

    let parked_sub = sub.clone();
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(parked_sub.next());
    });

    a.close(); // one source gone; `b` can still feed the consumer
    assert!(
        rx.recv_timeout(Duration::from_millis(50)).is_err(),
        "one source's close ended the fan-in early"
    );

    b.close(); // the last source: now the merged stream is done
    match rx.recv_timeout(Duration::from_secs(3)) {
        Ok(Ok(StreamNext::Closed)) => {}
        other => panic!("the last source's close did not terminate the park: {:?}", other),
    }

    // The subscription OWNS the fan-in: one close tears the merge down with it.
    sub.close();
    assert_eq!(revl_stream_pending(), 0);
}

/// A fan-in source's FAULT propagates at once: no silent loss, and no waiting on
/// the sibling source that is still live.
#[test]
fn merge_a_fault_propagates_immediately() {
    reset();
    let a = Stream::source();
    let b = Stream::source();
    let m = Stream::merge(&a, &b);
    let sub = Stream::subscribe(&m, "error", 0);

    let parked_sub = sub.clone();
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(parked_sub.next());
    });
    a.fault(String::from("kafka gone"));

    match rx.recv_timeout(Duration::from_secs(3)) {
        Ok(Err(reason)) => assert!(
            reason.contains("kafka gone"),
            "the fan-in fault lost its reason: {}",
            reason
        ),
        other => panic!("a source's fault never reached the merged consumer: {:?}", other),
    }

    sub.close();
    a.close();
    b.close();
    assert_eq!(revl_stream_pending(), 0);
}

/// Merging composes: a merged stream is itself a terminal-delivering provider,
/// so it can feed another merge — and ONE close on the subscription unwinds the
/// whole derived chain, leaving only the plain sources to their own brackets.
#[test]
fn merge_nests() {
    reset();
    let a = Stream::source();
    let b = Stream::source();
    let c = Stream::source();
    let inner = Stream::merge(&a, &b);
    let outer = Stream::merge(&inner, &c);
    let sub = Stream::subscribe(&outer, "error", 0);

    a.emit(String::from("deep"));
    assert_eq!(sub.next(), Ok(StreamNext::Item(String::from("deep"))));

    sub.close();
    a.close();
    b.close();
    c.close();
    assert_eq!(revl_stream_pending(), 0, "a nested fan-in left residue");
}

// -------------------------------------------------------------------------
// Backpressure: the default `error` policy faults on overflow — no silent loss.
// -------------------------------------------------------------------------
#[test]
fn backpressure_error_faults_on_overflow() {
    reset();
    let src = Stream::source();
    let sub = Stream::subscribe(&src, "error", 0);
    for _ in 0..(STREAM_BUFFER_CAPACITY + 1) {
        src.emit(String::from("x"));
    }
    // The buffered prefix is delivered first, then the overflow terminal.
    for i in 0..STREAM_BUFFER_CAPACITY {
        assert!(sub.next().is_ok(), "buffered item {} was lost", i);
    }
    match sub.next() {
        Err(reason) => assert!(
            reason.contains("overflow"),
            "overflow under the `error` policy = {}",
            reason
        ),
        other => panic!("a full bounded buffer must fault, got {:?}", other),
    }
    sub.close();
    src.close();
    assert_eq!(revl_stream_pending(), 0);
}

/// Subscribing to an already-terminal provider terminates at once, so the first
/// `next` cannot park on a provider that is already gone.
#[test]
fn subscribe_after_provider_close_terminates_immediately() {
    reset();
    let src = Stream::source();
    src.close();
    let sub = Stream::subscribe(&src, "error", 0);
    assert_eq!(sub.next(), Ok(StreamNext::Closed));
    sub.close();
    assert_eq!(revl_stream_pending(), 0);
}

// -------------------------------------------------------------------------
// §4.7/§6 — the iteration loop. `every x in sub { … }` and its typed-event
// sibling `on <Event> as e in sub { … }` lower to ONE loop, so they are driven
// here through the emitted components, and both surfaces are used.
//
// What the tier must show by RUNNING, not by shape:
//
//   * a `Closed` terminal ends the loop WITHOUT becoming an item — the body
//     never sees it, on either form — and it is not validated against the
//     event's schema, because the terminal test sits BEFORE the gate;
//   * a `Faulted` terminal is NOT caught: it fails the activation and the
//     prefix reverts LIFO with the subscription bracket on it. A schema
//     violation takes the same path, which is §6's "a failed handler leaves no
//     active subscription" without a line of teardown written for it;
//   * a conforming item reaches the typed body, and an in-window redelivery of
//     its identity key is COLLAPSED — traced, not silent.
//
// The tier erases the async color, so there is no `yield` and no second
// suspension point: the iteration boundary IS the return of the blocking
// `next`, and the gate between it and the body is pure (a JSON type check plus
// a bounded LRU probe), so no divert can interleave after the item was accepted
// and before the body ran. The emitted bytes pin that ordering —
// `test_typed_event_gate_sits_between_the_await_and_the_body` in
// backends/rust/test_emit_rust.py.
// -------------------------------------------------------------------------

/// The observable the iteration and handler bodies write into
/// (`emit sink.write(…)`): a hand-written impl of the emitted `Sink` service.
struct CollectSink {
    got: Arc<Mutex<Vec<String>>>,
}

impl Sink for CollectSink {
    fn write(&self, v: String) {
        self.got.lock().unwrap().push(v);
    }
}

fn snapshot(got: &Arc<Mutex<Vec<String>>>) -> Vec<String> {
    got.lock().unwrap().clone()
}

/// `drive`/`drive_and_dispose` with the `sink` service the iteration components
/// require provided, so their activation actually runs. `dispose` mirrors the
/// existing helpers: the teardown loop runs on the thread that owns the
/// activation, which is the only thread that may run it while the loop's park
/// holds the fiber's transition.
fn drive_stream_consumer(
    what: &'static str,
    component: fn() -> cordis::PluginHandle,
    sink: Arc<Mutex<Vec<String>>>,
    dispose: bool,
) -> std::sync::mpsc::Receiver<String> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let root = cordis::Context::new();
        let service: Box<dyn Sink> = Box::new(CollectSink { got: sink });
        root.provide("sink", service)
            .expect("the sink service is provided");
        let p = root.plugin(component(), ());
        let outcome = match p.try_wait() {
            Err(e) => format!("{} failed to activate: {:?}", what, e),
            Ok(_) if !dispose => format!("{} activated", what),
            Ok(_) => match p.dispose() {
                Err(e) => format!("{} dispose failed: {:?}", what, e),
                Ok(_) => String::from("disposed"),
            },
        };
        let _ = tx.send(outcome);
    });
    rx
}

#[test]
fn iteration_delivers_each_item_and_ends_on_the_closed_terminal() {
    reset();
    let got = Arc::new(Mutex::new(Vec::<String>::new()));
    let rx = drive_stream_consumer("Iterate", iterate, got.clone(), true);

    wait_for("the iteration to subscribe", || {
        revl_stream_live_subscriptions() == 1
    });
    let providers = revl_stream_providers();
    assert_eq!(providers.len(), 1, "the iteration's own source");

    // Two items, then an orderly close. Each item runs the body once; the
    // `Closed` terminal ends the loop and is never delivered as an item.
    providers[0].emit(String::from("a"));
    providers[0].emit(String::from("b"));
    providers[0].close();

    let outcome = rx
        .recv_timeout(Duration::from_secs(5))
        .expect("the Closed terminal never ended the iteration loop");
    assert_eq!(outcome, "disposed", "{}", outcome);
    assert_eq!(
        snapshot(&got),
        vec!["a", "b"],
        "each item runs the body once and the terminal never does"
    );

    // The orderly unload ran on the owning thread: the bracket inverses revert
    // LIFO, so the subscription is closed and nothing is left behind.
    let marks = revl_stream_marks();
    assert!(
        mark_index(&marks, "stream.close").is_some(),
        "unloading the iteration did not close the subscription: {:?}",
        marks
    );
    assert_eq!(revl_stream_pending(), 0, "residue: {:?}", marks);
}

#[test]
fn iteration_parked_next_is_ended_by_the_terminal_without_running_the_body() {
    reset();
    let got = Arc::new(Mutex::new(Vec::<String>::new()));
    let rx = drive_stream_consumer("Iterate", iterate, got.clone(), true);

    wait_for("the iteration to park in next", || {
        revl_stream_live_subscriptions() == 1
    });
    // The provider has emitted nothing and terminated nothing: the loop is
    // parked in the item/terminal race, holding its loading thread (A1 family
    // 2 — this tier parks a THREAD).
    assert!(
        rx.recv_timeout(Duration::from_millis(50)).is_err(),
        "the loop left the park with no item and no terminal"
    );

    revl_stream_providers()[0].close();

    let outcome = rx
        .recv_timeout(Duration::from_secs(5))
        .expect("the source close never unparked the iteration");
    assert_eq!(outcome, "disposed", "{}", outcome);
    assert!(
        snapshot(&got).is_empty(),
        "the body ran on a terminal: {:?}",
        snapshot(&got)
    );
    let marks = revl_stream_marks();
    assert!(
        mark_index(&marks, "stream.close").is_some(),
        "the parked iteration's subscription was not closed: {:?}",
        marks
    );
    assert_eq!(revl_stream_pending(), 0, "residue: {:?}", marks);
}

/// A `Faulted` terminal through a real activation: the loop does NOT catch it,
/// so the activation fails carrying the provider's reason and the prefix — the
/// subscription bracket on it — reverts LIFO. The gate's schema-violation return
/// is deliberately the same shape, so this is the property it rests on.
#[test]
fn iteration_fault_fails_the_activation_and_reverts_the_bracket() {
    reset();
    let got = Arc::new(Mutex::new(Vec::<String>::new()));
    let rx = drive_stream_consumer("Iterate", iterate, got.clone(), false);

    wait_for("the iteration to park in next", || {
        revl_stream_live_subscriptions() == 1
    });
    revl_stream_providers()[0].fault(String::from("provider gone"));

    let outcome = rx
        .recv_timeout(Duration::from_secs(5))
        .expect("the provider's fault never reached the parked iteration");
    assert!(
        outcome.contains("provider gone"),
        "a Faulted terminal must fail the activation with its reason: {}",
        outcome
    );
    assert!(
        snapshot(&got).is_empty(),
        "the body ran on a fault: {:?}",
        snapshot(&got)
    );

    let marks = revl_stream_marks();
    assert!(
        mark_index(&marks, "stream.close").is_some(),
        "the failed iteration did not close the subscription: {:?}",
        marks
    );
    assert_eq!(revl_stream_pending(), 0, "residue after the fault: {:?}", marks);
}

#[test]
fn typed_event_handler_dispatches_conforming_items_and_collapses_a_duplicate() {
    reset();
    let got = Arc::new(Mutex::new(Vec::<String>::new()));
    let rx = drive_stream_consumer("Handler", handler, got.clone(), true);

    wait_for("the handler to subscribe", || {
        revl_stream_live_subscriptions() == 1
    });
    let providers = revl_stream_providers();
    assert_eq!(providers.len(), 1, "the handler's own source");

    providers[0].emit(String::from(r#"{"order_id":"o1","quantity":1}"#));
    providers[0].emit(String::from(r#"{"order_id":"o2","quantity":2}"#));
    // A redelivery of `o1` INSIDE the window: collapsed, so its identity does
    // not run the body a second time.
    providers[0].emit(String::from(r#"{"order_id":"o1","quantity":9}"#));
    providers[0].close();

    let outcome = rx
        .recv_timeout(Duration::from_secs(5))
        .expect("the Closed terminal never ended the handler's loop");
    assert_eq!(outcome, "disposed", "{}", outcome);
    assert_eq!(
        snapshot(&got),
        vec!["o1", "o2"],
        "the redelivery must collapse, and the terminal must not dispatch"
    );

    let marks = revl_stream_marks();
    assert_eq!(
        count_mark(&marks, "event.OrderCreated admit"),
        2,
        "one admit per identity: {:?}",
        marks
    );
    assert_eq!(
        count_mark(&marks, "event.OrderCreated duplicate"),
        1,
        "the collapse is traced, not silent: {:?}",
        marks
    );
    assert_eq!(revl_stream_pending(), 0, "residue: {:?}", marks);
}

/// A schema violation is the SAME terminal a provider abort delivers: the gate
/// hands the error back, the loop returns it uncaught, the activation fails and
/// the prefix — the subscription bracket on it — reverts LIFO. The malformed
/// item never reaches the body, and nothing is left behind (§6, A8).
#[test]
fn typed_event_handler_schema_violation_faults_and_closes_the_subscription() {
    reset();
    let got = Arc::new(Mutex::new(Vec::<String>::new()));
    let rx = drive_stream_consumer("Handler", handler, got.clone(), false);

    wait_for("the handler to subscribe", || {
        revl_stream_live_subscriptions() == 1
    });

    // `quantity` is required by the derived schema and absent here: the item
    // fails validation BEFORE the body, so it is never dispatched.
    revl_stream_providers()[0].emit(String::from(r#"{"order_id":"o1"}"#));

    let outcome = rx
        .recv_timeout(Duration::from_secs(5))
        .expect("the schema violation never terminated the handler");
    assert!(
        outcome.contains("failed its schema"),
        "activation outcome = {}, want a schema-violation fault",
        outcome
    );
    assert!(
        snapshot(&got).is_empty(),
        "a malformed item reached the body: {:?}",
        snapshot(&got)
    );

    let marks = revl_stream_marks();
    assert!(
        mark_index(&marks, "stream.close").is_some(),
        "the failed handler did not close the subscription: {:?}",
        marks
    );
    assert_eq!(revl_stream_pending(), 0, "stream residue after the fault: {:?}", marks);
}

/// The contract the gate carries, driven directly: validation runs BEFORE the
/// dedup probe (so no malformed item ever reaches the table or the body), and
/// the table is a BOUNDED window, not a history — an identity that ages out
/// runs again, so memory stays constant in the length of the stream.
#[test]
fn event_contract_is_bounded_and_validates_before_it_dedups() {
    reset();
    let schema = concat!(
        r#"{"type":"object","properties":{"order_id":{"type":"string"},"#,
        r#""quantity":{"type":"integer"}},"required":["order_id","quantity"]}"#
    );
    let o1 = r#"{"order_id":"o1","quantity":1}"#;
    let mut c = Stream::contract("OrderCreated", schema, "order_id", 2);

    assert_eq!(c.admit(o1, "probe"), Ok(true), "first delivery runs the body");
    assert_eq!(c.admit(o1, "probe"), Ok(false), "a redelivery collapses");

    // Two further identities push `o1` out of a two-entry window: its next
    // delivery is admitted again rather than remembered forever.
    assert_eq!(
        c.admit(r#"{"order_id":"o2","quantity":2}"#, "probe"),
        Ok(true)
    );
    assert_eq!(
        c.admit(r#"{"order_id":"o3","quantity":3}"#, "probe"),
        Ok(true)
    );
    assert_eq!(c.admit(o1, "probe"), Ok(true), "the window is bounded");

    // Validation is FIRST: a malformed item carrying an admitted key faults
    // instead of collapsing, so the table is only ever fed sound items.
    let mut c = Stream::contract("OrderCreated", schema, "order_id", 2);
    assert_eq!(c.admit(o1, "probe"), Ok(true));
    match c.admit(r#"{"order_id":"o1"}"#, "probe") {
        Err(reason) => assert!(
            reason.contains("failed its schema"),
            "the reason must name the schema: {}",
            reason
        ),
        other => panic!("a malformed item did not fault: {:?}", other),
    }
}
