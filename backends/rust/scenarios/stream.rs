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
    consumer, fanin, parked, revl_stream_live_subscriptions, revl_stream_marks,
    revl_stream_pending, revl_stream_providers, revl_stream_reset, Stream, StreamNext,
    STREAM_BUFFER_CAPACITY,
};
use std::time::{Duration, Instant};

fn reset() {
    revl_stream_reset();
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
        let outcome = match p.wait() {
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
        let outcome = match p.wait() {
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
    c.wait().expect("Consumer did not reach ACTIVE");

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
