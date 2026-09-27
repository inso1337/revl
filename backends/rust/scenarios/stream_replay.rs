//! item 130 §4.5 on the rust tier: the provider-declared last-n backlog, run on
//! the REAL cordis-rs runtime against the crate emitted from the inline
//! `_STREAM_REPLAY_RVL` document in backends/rust/test_emit_rust.py.
//!
//! Each case mirrors the py reference's own case in
//! backends/python/tests/test_stream_runtime.py: the provider holds its newest
//! n items whether or not anyone listens, a late subscriber receives the newest
//! k of them oldest first and BEFORE any live item, and the backlog rides the
//! provider's own forward path, so it takes the chain, the buffer and the
//! overflow policy exactly as a live item does. The durable `replay(from: …)`
//! cursor is refused by name at emit time and never reaches this runtime.

use revl_stream_replay_scn::{
    replayed, revl_stream_live_subscriptions, revl_stream_marks, revl_stream_pending,
    revl_stream_providers, revl_stream_reset, Sink, Stream, StreamNext, Subscription,
};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

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

fn take(sub: &Subscription, n: usize) -> Vec<String> {
    (0..n)
        .map(|i| match sub.next() {
            Ok(StreamNext::Item(item)) => item,
            other => panic!("next {} = {:?}, want an item", i, other),
        })
        .collect()
}

fn replay_marks() -> Vec<String> {
    revl_stream_marks()
        .into_iter()
        .filter(|m| m.starts_with("stream.replay"))
        .collect()
}

fn emit_all(src: &Stream, items: &[&str]) {
    for item in items {
        src.emit(String::from(*item));
    }
}

#[test]
fn a_late_subscriber_receives_the_held_backlog_then_live_items() {
    revl_stream_reset();
    let src = Stream::source_replay(3);
    emit_all(&src, &["a", "b", "c", "d"]); // nobody is listening yet
    let sub = Stream::subscribe_replay(&src, 3, "error", 0);
    assert_eq!(take(&sub, 3), vec!["b", "c", "d"], "the last three, oldest first");
    src.emit(String::from("e"));
    assert_eq!(take(&sub, 1), vec!["e"], "live items follow the backlog");
    assert_eq!(
        replay_marks(),
        vec!["stream.replay b", "stream.replay c", "stream.replay d"]
    );
    sub.close();
    src.close();
    assert_eq!(revl_stream_pending(), 0);
}

/// The non-vacuity control: the same timing with no declaration sees nothing of
/// what was emitted before `subscribe`, which is §4.5's default.
#[test]
fn without_a_declaration_there_is_no_backlog() {
    revl_stream_reset();
    let src = Stream::source();
    emit_all(&src, &["a", "b", "c", "d"]);
    let sub = Stream::subscribe(&src, "error", 0);
    src.emit(String::from("e"));
    assert_eq!(take(&sub, 1), vec!["e"]);
    assert!(replay_marks().is_empty());
    sub.close();
    src.close();
}

#[test]
fn a_smaller_request_takes_the_newest_items() {
    revl_stream_reset();
    let src = Stream::source_replay(4);
    emit_all(&src, &["1", "2", "3", "4", "5"]); // the bounded backlog trims "1"
    let sub = Stream::subscribe_replay(&src, 2, "error", 0);
    assert_eq!(take(&sub, 2), vec!["4", "5"]);
    sub.close();
    src.close();
}

/// A replayed item is delivered through the provider's own forward path, so a
/// backlog larger than the buffer is ordinary `error`-policy overflow.
#[test]
fn a_backlog_takes_the_buffer_and_the_policy() {
    revl_stream_reset();
    let src = Stream::source_replay(6);
    emit_all(&src, &["0", "1", "2", "3", "4", "5"]);
    let sub = Stream::subscribe_replay(&src, 6, "error", 2);
    assert_eq!(take(&sub, 2), vec!["0", "1"]);
    match sub.next() {
        Err(reason) => assert!(reason.contains("overflow"), "{}", reason),
        other => panic!("a backlog past the buffer must fault, got {:?}", other),
    }
    sub.close();
    src.close();
    assert_eq!(revl_stream_pending(), 0);
}

#[test]
fn the_backlog_rides_the_combinator_chain() {
    revl_stream_reset();
    let src = Stream::source_replay(4);
    emit_all(&src, &["1", "2", "3", "4"]);
    let chain = Stream::filter(&src, |x: String| x == "2" || x == "4");
    let sub = Stream::subscribe_replay(&chain, 4, "error", 0);
    assert_eq!(take(&sub, 2), vec!["2", "4"]);
    sub.close();
    src.close();
    assert_eq!(revl_stream_pending(), 0);
}

#[test]
fn a_declaration_alone_replays_nothing() {
    revl_stream_reset();
    let src = Stream::source_replay(4);
    src.emit(String::from("old"));
    let sub = Stream::subscribe(&src, "error", 0);
    src.emit(String::from("new"));
    assert_eq!(take(&sub, 1), vec!["new"]);
    sub.close();
    src.close();
}

/// The one ordering the single-threaded reference cannot exhibit. Live
/// emissions racing a late subscriber land either BEFORE the subscribe (inside
/// the held backlog) or AFTER the whole backlog, never in the middle of it and
/// never twice: the consumer sees one contiguous run of the provider's sequence.
#[test]
fn a_live_emit_never_overtakes_the_backlog() {
    const HELD: usize = 48;
    const LIVE: usize = 4000;
    for round in 0..10 {
        revl_stream_reset();
        let src = Stream::source_replay(HELD);
        for i in 0..HELD {
            src.emit(i.to_string());
        }
        let (started_tx, started_rx) = std::sync::mpsc::channel();
        let feeder = src.clone();
        let handle = std::thread::spawn(move || {
            for i in HELD..HELD + LIVE {
                feeder.emit(i.to_string());
                if i == HELD {
                    let _ = started_tx.send(());
                }
            }
        });
        started_rx.recv().expect("the feeder started");
        let sub = Stream::subscribe_replay(&src, HELD, "error", 2 * (HELD + LIVE));
        handle.join().expect("the feeder finished");
        src.close();
        let mut got: Vec<usize> = Vec::new();
        loop {
            match sub.next() {
                Ok(StreamNext::Item(item)) => got.push(item.parse().unwrap()),
                Ok(StreamNext::Closed) => break,
                Err(e) => panic!("round {}: {}", round, e),
            }
        }
        assert!(got.len() >= HELD, "round {}: {} items", round, got.len());
        for w in got.windows(2) {
            assert_eq!(
                w[1],
                w[0] + 1,
                "round {}: a live item overtook the backlog or was delivered twice",
                round
            );
        }
        assert_eq!(*got.last().unwrap(), HELD + LIVE - 1, "round {}", round);
        sub.close();
    }
}

struct CollectSink {
    got: Arc<Mutex<Vec<String>>>,
}

impl Sink for CollectSink {
    fn write(&self, v: String) {
        self.got.lock().unwrap().push(v);
    }
}

/// The emitted `Replayed` component compiles against this runtime, runs its
/// loop over the declared provider, and still tears down with no residue.
#[test]
fn the_emitted_replayed_component_runs_and_closes_its_bracket() {
    revl_stream_reset();
    let got = Arc::new(Mutex::new(Vec::<String>::new()));
    let sink = got.clone();
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let root = cordis::Context::new();
        let service: Box<dyn Sink> = Box::new(CollectSink { got: sink });
        root.provide("sink", service).expect("the sink service is provided");
        let p = root.plugin(replayed(), ());
        let outcome = match p.try_wait() {
            Err(e) => format!("Replayed failed to activate: {:?}", e),
            Ok(_) => match p.dispose() {
                Err(e) => format!("Replayed dispose failed: {:?}", e),
                Ok(_) => String::from("disposed"),
            },
        };
        let _ = tx.send(outcome);
    });
    wait_for("the replayed loop to subscribe", || {
        revl_stream_live_subscriptions() == 1
    });
    let providers = revl_stream_providers();
    assert_eq!(providers.len(), 1, "the component's own source");
    providers[0].emit(String::from("x"));
    providers[0].emit(String::from("y"));
    providers[0].close();
    let outcome = rx
        .recv_timeout(Duration::from_secs(5))
        .expect("the provider's Closed never ended the loop");
    assert_eq!(outcome, "disposed", "{}", outcome);
    assert_eq!(got.lock().unwrap().clone(), vec!["x", "y"]);
    assert_eq!(revl_stream_pending(), 0, "{:?}", revl_stream_marks());
}
