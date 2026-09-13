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
        theIterationFormRunsTheBodyPerItemAndEndsOnClosed();
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
}
