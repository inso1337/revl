// item 130 Slices 3-4 — the emitted java `Stream[T]` lowering, RUN on the JVM.
//
// docs/design/130-stream-reactive-types.md §4.6 puts go, java and rust in their
// own slice because those tiers ERASE the async color: `next` becomes a blocking
// park that the CANCEL signal also ends, and `close` trips that signal. That park
// is the whole point — it is what keeps the bracket inverse reachable off the
// teardown thread (§9 Part A), so a `next` parked on a provider that never emits
// cannot make teardown deadlock behind it.
//
// The compile-shape assertions live in tests/test_stream_reactive.py. THIS is the
// half that proves the park actually unparks, that a provider fault reaches an
// outstanding `next` through a real activation, and that `merge`'s multi-source
// teardown leaves nothing behind. Driven on the stub reference runtime in
// ../stubs (LIFO EffectScope/composite, per the DESIGN §7 contract).

import io.cordis4j.core.Context;
import io.cordis4j.core.CordisException;
import io.cordis4j.core.Disposable;
import io.cordis4j.core.ServiceKey;

public final class RunStream130 {
    private static final java.util.List<String> DELIVERED =
        java.util.Collections.synchronizedList(new java.util.ArrayList<>());

    static final class Recorder implements revl.Components.Sink {
        public void write(String v) {
            DELIVERED.add(v);
        }
    }

    private static void fail(String scenario, String why) {
        System.err.println(scenario + ": " + why
            + " (marks=" + revl.Components.Stream.marks()
            + ", delivered=" + DELIVERED + ")");
        System.exit(1);
    }

    private static void expect(String scenario, boolean ok, String why) {
        if (!ok) {
            fail(scenario, why);
        }
    }

    private static void expectNoResidue(String scenario) {
        int pending = revl.Components.Stream.pendingResources();
        if (pending != 0) {
            fail(scenario, pending + " unreleased stream resource(s) — a bracket "
                + "inverse did not run, so a host listener outlived its owner");
        }
    }

    private static void expectDelivered(String scenario, String... want) {
        java.util.List<String> wanted = java.util.List.of(want);
        if (!DELIVERED.equals(wanted)) {
            fail(scenario, "expected delivered " + wanted);
        }
    }

    private static int markAt(String scenario, String mark) {
        java.util.List<String> marks = revl.Components.Stream.marks();
        int at = marks.indexOf(mark);
        if (at < 0) {
            fail(scenario, "no `" + mark + "` mark");
        }
        return at;
    }

    private static Context fresh() {
        DELIVERED.clear();
        revl.Components.Stream.reset();
        Context ctx = new Context();
        ctx.provide(ServiceKey.of(revl.Components.Sink.class, "sink"), new Recorder());
        return ctx;
    }

    // Wait for a condition another thread is driving. Bounded, so a hung park
    // fails the scenario instead of hanging the suite.
    private static void await(String scenario, String what,
                             java.util.function.BooleanSupplier ready) {
        long deadline = System.nanoTime() + 10_000_000_000L;
        while (!ready.getAsBoolean()) {
            if (System.nanoTime() > deadline) {
                fail(scenario, "timed out waiting for " + what);
            }
            try {
                Thread.sleep(2L);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
                fail(scenario, "interrupted waiting for " + what);
            }
        }
    }

    private static boolean marked(String mark) {
        return revl.Components.Stream.marks().contains(mark);
    }

    private static int markCount(String mark) {
        int n = 0;
        for (String seen : revl.Components.Stream.marks()) {
            if (seen.equals(mark)) {
                n = n + 1;
            }
        }
        return n;
    }

    // An activation run on its own thread, so the main thread can drive the
    // provider while the activation is parked in `next`.
    static final class Activation {
        private final Thread thread;
        private volatile Disposable disposable;
        private volatile Throwable failure;

        Activation(String name, java.util.concurrent.Callable<Disposable> body) {
            this.thread = new Thread(() -> {
                try {
                    disposable = body.call();
                } catch (Throwable caught) {
                    failure = caught;
                }
            }, name);
            this.thread.setDaemon(true);
            this.thread.start();
        }

        boolean done() {
            return !thread.isAlive();
        }
    }

    public static void main(String[] args) throws Exception {
        coreGuaranteeIsLifoAndResidueFree();
        aParkedNextIsUnparkedByTheOwnersOwnClose();
        aProviderFaultFailsTheActivationAndClosesTheSubscription();
        theFanInRidesTheSubscriptionsSingleBracket();
        oneSourcesCloseDoesNotStrandTheConsumerOnTheOther();
        aFullBoundedBufferFaultsRatherThanDropping();
        dropNewestDiscardsTheIncomingItemAndRecordsIt();
        dropOldestEvictsTheBufferHeadAndRecordsIt();
        blockPausesTheProviderUntilTheConsumerDrains();
        aPausedSubscriptionStillClosesCleanly();
        aBlockPauseDoesNotSpendTheTakeBudget();
        theIterationFormRunsTheBodyPerItemAndEndsOnClosed();
        theCombinatorChainFiltersMapsAndEndsOnTake();
        theCombinatorChainUnwindsOffTheOneBracket();
        System.out.println("STREAM_130_OK");
    }

    // §10.2 the core guarantee: unloading the owner closes the stream, LIFO, with
    // no residue. `Consumer` acquires a Pool, then a source, then a subscription;
    // teardown must run the subscription's `close` FIRST (it was registered last)
    // and leave nothing unreleased.
    private static void coreGuaranteeIsLifoAndResidueFree() throws Exception {
        String scenario = "core guarantee";
        Context ctx = fresh();
        Disposable activation = new revl.Components.ConsumerPlugin().apply(ctx);
        expect(scenario, revl.Components.Stream.liveSubscriptions() == 1,
            "the subscription is not live after activation");
        activation.dispose();
        expect(scenario, revl.Components.Stream.liveSubscriptions() == 0,
            "the subscription survived its owner");
        expectNoResidue(scenario);
        // LIFO: the consumer's `close` precedes the provider's, so the provider is
        // never torn down under a live listener.
        expect(scenario,
            markAt(scenario, "stream.close") < markAt(scenario, "stream.source close"),
            "the provider closed before its consumer (teardown was not LIFO)");
    }

    // §9 Part A: a `next` parked on a provider that never emits is resolved by the
    // SUBSCRIPTION OWNER's own bracket inverse, running on another thread, and
    // that inverse returns without waiting for the park to drain. This drives the
    // runtime directly, because the emitted activation cannot hand back its
    // disposable until its own `next` has already returned — which is exactly the
    // deadlock the cancellation-first `next` exists to prevent.
    private static void aParkedNextIsUnparkedByTheOwnersOwnClose() throws Exception {
        String scenario = "parked next unparked by close";
        fresh();
        revl.Components.Stream provider = revl.Components.Stream.source();
        revl.Components.Subscription sub =
            revl.Components.Stream.subscribe(provider, "error", 0);
        java.util.concurrent.atomic.AtomicReference<Object> landed =
            new java.util.concurrent.atomic.AtomicReference<>();
        java.util.concurrent.CountDownLatch entered =
            new java.util.concurrent.CountDownLatch(1);
        Thread consumer = new Thread(() -> {
            entered.countDown();
            landed.set(sub.next());
        }, "parked-consumer");
        consumer.setDaemon(true);
        consumer.start();
        entered.await();
        // The park is a condition wait, so give the consumer a moment to reach it;
        // `close` must win either way (a close racing an unentered park still
        // trips the flag the park re-checks on entry).
        Thread.sleep(20L);
        long before = System.nanoTime();
        sub.close();
        long elapsed = System.nanoTime() - before;
        consumer.join(10_000L);
        expect(scenario, !consumer.isAlive(), "the parked `next` never returned");
        expect(scenario, revl.Components.Stream.isClosed(landed.get()),
            "the parked `next` did not return the `Closed` terminal");
        expect(scenario, elapsed < 5_000_000_000L,
            "`close` waited for the park to drain");
        provider.close();
        expectNoResidue(scenario);
    }

    // §9 Part B: a provider abort terminates an outstanding `next` as `Faulted`
    // through a REAL activation. The throw is not caught by the loop, so the
    // activation fails, its A8 self-revert disposes the accumulated prefix — the
    // subscription bracket included — and nothing is left behind.
    private static void aProviderFaultFailsTheActivationAndClosesTheSubscription()
            throws Exception {
        String scenario = "provider fault fails the activation";
        Context ctx = fresh();
        Activation run = new Activation("parked-activation",
            () -> new revl.Components.ParkedPlugin().apply(ctx));
        await(scenario, "the subscription", () -> marked("stream.subscribe"));
        Thread.sleep(20L);
        revl.Components.Stream provider = revl.Components.Stream.providers().get(0);
        provider.fault("provider died");
        await(scenario, "the activation to fail", run::done);
        expect(scenario, run.failure instanceof CordisException,
            "the `Faulted` terminal did not fail the activation: " + run.failure);
        expect(scenario, run.failure.getMessage().contains("provider died"),
            "the fault reason was lost: " + run.failure.getMessage());
        expect(scenario, revl.Components.Stream.liveSubscriptions() == 0,
            "the failed activation left its subscription active (A8, §4.7)");
        provider.close();
        expectNoResidue(scenario);
    }

    // `merge` (§1): the fan-in opens INSIDE the subscription's acquisition, so
    // multi-source teardown rides the ONE bracket the subscribe registered. An
    // item from either source reaches the one consumer; disposal closes the
    // subscription, then the derived merge, and leaves each source to its own
    // bracket.
    private static void theFanInRidesTheSubscriptionsSingleBracket() throws Exception {
        String scenario = "merge rides one bracket";
        Context ctx = fresh();
        Activation run = new Activation("fanin-activation",
            () -> new revl.Components.FaninPlugin().apply(ctx));
        await(scenario, "the subscription", () -> marked("stream.subscribe"));
        Thread.sleep(20L);
        // the SECOND source: an item from either upstream reaches the consumer
        revl.Components.Stream.providers().get(1).emit("from-b");
        await(scenario, "the activation to land", run::done);
        expect(scenario, run.failure == null, "the activation failed: " + run.failure);
        expect(scenario, revl.Components.Stream.liveSubscriptions() == 1,
            "the subscription is not live after activation");
        run.disposable.dispose();
        expectNoResidue(scenario);
        expect(scenario,
            markAt(scenario, "stream.close") < markAt(scenario, "stream.merge close"),
            "the derived merge closed before the subscription that owns it");
        expect(scenario,
            markAt(scenario, "stream.merge close")
                < markAt(scenario, "stream.source close"),
            "a plain source closed before the merge detached from it");
    }

    // A fan-in stays live while ANY source is: one source's orderly close only
    // counts down, so it never strands a consumer the other source can still
    // feed. Only the LAST close delivers the merged `Closed`.
    private static void oneSourcesCloseDoesNotStrandTheConsumerOnTheOther()
            throws Exception {
        String scenario = "one source closing does not strand the consumer";
        fresh();
        revl.Components.Stream a = revl.Components.Stream.source();
        revl.Components.Stream b = revl.Components.Stream.source();
        revl.Components.Stream merged = revl.Components.Stream.merge(a, b);
        revl.Components.Subscription sub =
            revl.Components.Stream.subscribe(merged, "error", 0);
        a.close();
        b.emit("still-feeding");
        Object item = sub.next();
        expect(scenario, "still-feeding".equals(item),
            "the surviving source's item did not reach the consumer: " + item);
        b.close();
        expect(scenario, revl.Components.Stream.isClosed(sub.next()),
            "the LAST source's close did not deliver the merged `Closed`");
        sub.close();
        expectNoResidue(scenario);
    }

    // Backpressure `error` (§4.4): every buffer is bounded, and an overflow under
    // the default policy is a deterministic `Faulted(overflow)` terminal, never a
    // silent drop. The declared `buffer 1` is honoured, so the second delivery
    // into a full buffer faults.
    private static void aFullBoundedBufferFaultsRatherThanDropping() throws Exception {
        String scenario = "bounded buffer faults on overflow";
        fresh();
        revl.Components.Stream provider = revl.Components.Stream.source();
        revl.Components.Subscription sub =
            revl.Components.Stream.subscribe(provider, "error", 1);
        provider.emit("first");
        provider.emit("overflowing");
        expect(scenario, marked("stream.overflow"), "the overflow was not recorded");
        Object first = sub.next();
        expect(scenario, "first".equals(first),
            "the buffered item was lost: " + first);
        boolean faulted = false;
        try {
            sub.next();
        } catch (CordisException expected) {
            faulted = expected.getMessage().contains("overflow");
        }
        expect(scenario, faulted, "the overflow did not fault the consumer");
        sub.close();
        provider.close();
        expectNoResidue(scenario);
    }

    // Backpressure: the three NON-DEFAULT policies (item 130 Slice 2, §4.4).
    //
    // Each case below mirrors the py reference's own case in
    // backends/python/tests/test_stream_runtime.py statement for statement — same
    // capacity, same emit sequence, same drained values, same trace marks. That
    // is deliberate: a policy that COMPILES on this tier and behaves differently
    // from the reference is strictly worse than one that is refused by name, so
    // the reference's case IS this tier's specification.

    // `drop_newest` discards the INCOMING item and records the loss. A drop
    // policy never blocks the provider and never pauses the subscription.
    private static void dropNewestDiscardsTheIncomingItemAndRecordsIt() throws Exception {
        String scenario = "drop_newest";
        fresh();
        revl.Components.Stream provider = revl.Components.Stream.source();
        revl.Components.Subscription sub =
            revl.Components.Stream.subscribe(provider, "drop_newest", 2);
        for (String item : new String[] {"i0", "i1", "i2", "i3"}) {
            expect(scenario, provider.emit(item),
                "a drop policy never blocks the provider (" + item + ")");
        }
        expect(scenario, "i0".equals(sub.next()), "the buffered prefix was lost");
        expect(scenario, "i1".equals(sub.next()), "the buffered prefix was lost");
        // the buffered prefix survives; the overflow is DISCARDED and recorded,
        // so the loss is explicit rather than silent.
        expect(scenario, marked("stream.drop_newest i2") && marked("stream.drop_newest i3"),
            "the discarded items were not recorded");
        expect(scenario, "active".equals(sub.state()), "a drop policy never pauses");
        sub.close();
        provider.close();
        expectNoResidue(scenario);
    }

    // `drop_oldest` evicts the buffer HEAD and keeps the newest item
    // (latest-wins gauges), recording the eviction.
    private static void dropOldestEvictsTheBufferHeadAndRecordsIt() throws Exception {
        String scenario = "drop_oldest";
        fresh();
        revl.Components.Stream provider = revl.Components.Stream.source();
        revl.Components.Subscription sub =
            revl.Components.Stream.subscribe(provider, "drop_oldest", 2);
        for (String item : new String[] {"i0", "i1", "i2"}) {
            expect(scenario, provider.emit(item),
                "a drop policy never blocks the provider (" + item + ")");
        }
        // latest-wins: the head was evicted, the newest item is buffered
        expect(scenario, "i1".equals(sub.next()), "the head was not the evicted item");
        expect(scenario, "i2".equals(sub.next()), "the newest item was not kept");
        expect(scenario, marked("stream.drop_oldest i0"),
            "the eviction was not recorded");
        sub.close();
        provider.close();
        expectNoResidue(scenario);
    }

    // `block` REFUSES the delivery and puts the subscription in the reserved
    // `Paused` state. No implicit retry — `emit` returns false, so the provider
    // knows it is suspended, and the refusal is TRACED. Draining resumes it
    // eagerly, which is what the reference does with no `drain` window declared;
    // a declared window is refused by this tier's emitter rather than resumed
    // early.
    private static void blockPausesTheProviderUntilTheConsumerDrains() throws Exception {
        String scenario = "block";
        fresh();
        revl.Components.Stream provider = revl.Components.Stream.source();
        revl.Components.Subscription sub =
            revl.Components.Stream.subscribe(provider, "block", 2);
        expect(scenario, provider.emit("i0") && provider.emit("i1"),
            "the first two items fit the bounded buffer");
        expect(scenario, "active".equals(sub.state()), "paused before the buffer filled");
        expect(scenario, !provider.emit("i2"),
            "the provider must be TOLD it is blocked - a `true` here is a silent loss");
        expect(scenario, "paused".equals(sub.state()),
            "the reserved `Paused` state index was not entered");
        expect(scenario, marked("stream.paused"), "the pause was not recorded");
        // the refused delivery is traced as such, exactly as the py reference and
        // the ts tier trace it - a refusal is never silent.
        expect(scenario, marked("stream.emit i2 refused"),
            "the refused emit was not recorded");

        expect(scenario, "i0".equals(sub.next()), "the buffered item was lost");
        expect(scenario, "active".equals(sub.state()),
            "draining did not resume the provider");
        expect(scenario, marked("stream.resume"), "the resume was not recorded");
        expect(scenario, provider.emit("i2"),
            "the provider may emit again once the subscription is Active");
        expect(scenario, "i1".equals(sub.next()), "no silent loss: nothing was dropped");
        expect(scenario, "i2".equals(sub.next()), "no silent loss: nothing was dropped");
        sub.close();
        provider.close();
        expectNoResidue(scenario);
    }

    // A `block`-paused subscription is still torn down by its own bracket
    // inverse: the pause is a provider-side refusal, never a hold on the
    // consumer, so the core guarantee (§9 Part A) is untouched by the policy.
    private static void aPausedSubscriptionStillClosesCleanly() throws Exception {
        String scenario = "a paused subscription closes";
        fresh();
        revl.Components.Stream provider = revl.Components.Stream.source();
        revl.Components.Subscription sub =
            revl.Components.Stream.subscribe(provider, "block", 1);
        provider.emit("i0");
        provider.emit("i1"); // refused: paused
        expect(scenario, "paused".equals(sub.state()), "the buffer did not pause");
        sub.close();
        expect(scenario, "closed".equals(sub.state()), "close did not reach the state index");
        expect(scenario, revl.Components.Stream.isClosed(sub.next()),
            "next after close did not report the Closed terminal");
        provider.close();
        expectNoResidue(scenario);
    }

    // A lossy/blocking POLICY and a `take(n)` link on the SAME subscription - the
    // interaction between the two Slice 2 landings, which neither one tests alone.
    //
    // Both mechanisms ride the SAME boolean: the acceptance `deliver` answers.
    // `take(n)` reserves its slot under the lock and REFUNDS it when the forward
    // is refused, and a `block` pause is exactly such a refusal. So the pause must
    // not spend the budget: a consumer that asked for two items has to receive
    // two. If the two used different signalling, `take(2)` would exhaust after ONE
    // delivered item, push its `Closed` terminal early, and the second item would
    // be gone with no overflow, no drop mark and no fault - the silent loss §4.4
    // rules out.
    //
    // The sequence is the py reference's, asserted there by
    // `test_a_block_pause_does_not_spend_the_take_budget`.
    private static void aBlockPauseDoesNotSpendTheTakeBudget() throws Exception {
        String scenario = "a block pause does not spend take's budget";
        fresh();
        revl.Components.Stream provider = revl.Components.Stream.source();
        revl.Components.Stream chain = revl.Components.Stream.take(provider, 2);
        revl.Components.Subscription sub =
            revl.Components.Stream.subscribe(chain, "block", 1);

        expect(scenario, provider.emit("i0"), "the first item fits the buffer");
        // the buffer is full; `block` refuses and pauses, and the refusal travels
        // UP through the take link, which must hand its slot back.
        expect(scenario, !provider.emit("i1"),
            "the consumer's pause did not reach the provider through the chain");
        expect(scenario, "paused".equals(sub.state()), "the buffer did not pause");
        expect(scenario, marked("stream.emit i1 refused"),
            "the refusal was not traced");
        expect(scenario, !marked("stream.take exhausted"),
            "the refused item SPENT the take budget - take(2) exhausted after one "
            + "delivered item");

        expect(scenario, "i0".equals(sub.next()), "the buffered item was lost");
        expect(scenario, "active".equals(sub.state()),
            "draining did not resume the provider");
        // the budget survived the pause, so the second item is still admissible
        expect(scenario, provider.emit("i1"),
            "the take budget was not refunded: the second item was refused");
        expect(scenario, "i1".equals(sub.next()), "the second item was lost");
        expect(scenario, revl.Components.Stream.isClosed(sub.next()),
            "take(2) did not end the derived stream after the second item");
        expect(scenario, markCount("stream.take exhausted") == 1,
            "take's terminal fired more than once");
        sub.close();
        provider.close();
        expectNoResidue(scenario);
    }

    // Slice 4 (§4.7): `every o in sub { … }` runs the effectful body once per
    // delivered item, and a `Closed` terminal ends the loop WITHOUT entering the
    // body — running the callback on a terminal would be the silent-data invention
    // §1 forbids.
    private static void theIterationFormRunsTheBodyPerItemAndEndsOnClosed()
            throws Exception {
        String scenario = "iteration form";
        Context ctx = fresh();
        Activation run = new Activation("iterate-activation",
            () -> new revl.Components.IteratePlugin().apply(ctx));
        await(scenario, "the subscription", () -> marked("stream.subscribe"));
        revl.Components.Stream provider = revl.Components.Stream.providers().get(0);
        provider.emit("one");
        provider.emit("two");
        await(scenario, "both items", () -> DELIVERED.size() == 2);
        provider.close();
        await(scenario, "the loop to end", run::done);
        expect(scenario, run.failure == null, "the activation failed: " + run.failure);
        expectDelivered(scenario, "one", "two");
        expect(scenario, markCount("stream.emit one") == 1,
            "the item was delivered more than once");
        run.disposable.dispose();
        expectNoResidue(scenario);
    }

    // Slice 2 — the derived-stream combinator chain (§1), executed. The
    // compile-shape half proves the chain EMITS; only running it proves the three
    // answers agree with the py reference, which is the whole point of lowering
    // it here rather than leaving it refused:
    //
    //   * `filter(p)` drops an item without it ever reaching the body, and a
    //     rejection is NOT backpressure (the provider's emit still succeeds);
    //   * `map(f)` reaches the body TRANSFORMED;
    //   * `take(n)` ends the derived stream with a `Closed` TERMINAL after n
    //     accepted items — the loop ends on it, the terminal never enters the
    //     body, and a later emit is not delivered.
    private static void theCombinatorChainFiltersMapsAndEndsOnTake()
            throws Exception {
        String scenario = "combinator chain";
        Context ctx = fresh();
        Activation run = new Activation("chain-activation",
            () -> new revl.Components.ChainPlugin().apply(ctx));
        await(scenario, "the subscription", () -> marked("stream.subscribe"));
        // one PROVIDER plus three derived links (filter, map, take) — every link
        // is a live stream owned by the subscription below it.
        await(scenario, "the three combinator links",
            () -> revl.Components.Stream.providers().size() == 4);
        revl.Components.Stream provider = revl.Components.Stream.providers().get(0);
        provider.emit("skip");    // filtered: never reaches the body
        provider.emit("one");     // -> "one!"
        provider.emit("skip");    // filtered again
        provider.emit("two");     // -> "two!", which spends take(2)
        provider.emit("three");   // past take(2): never delivered
        await(scenario, "the loop to end on the spent take", run::done);
        expect(scenario, run.failure == null, "the activation failed: " + run.failure);
        expectDelivered(scenario, "one!", "two!");
        markAt(scenario, "stream.take exhausted");
        run.disposable.dispose();
        expectNoResidue(scenario);
    }

    // The core guarantee still holds THROUGH a chain: an orderly source close
    // reaches the parked consumer through every link, and the ONE bracket inverse
    // closes each derived link down to (but not including) the provider.
    private static void theCombinatorChainUnwindsOffTheOneBracket()
            throws Exception {
        String scenario = "combinator chain teardown";
        Context ctx = fresh();
        Activation run = new Activation("chain-teardown-activation",
            () -> new revl.Components.ChainPlugin().apply(ctx));
        await(scenario, "the subscription", () -> marked("stream.subscribe"));
        revl.Components.Stream.providers().get(0).close();
        await(scenario, "the terminal to reach the consumer", run::done);
        expect(scenario, run.failure == null, "the activation failed: " + run.failure);
        expectDelivered(scenario);
        run.disposable.dispose();
        markAt(scenario, "stream.stage close take");
        markAt(scenario, "stream.stage close map");
        markAt(scenario, "stream.stage close filter");
        expectNoResidue(scenario);
    }
}
