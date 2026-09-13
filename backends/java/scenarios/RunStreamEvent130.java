// item 130 Slice 5 — the emitted java `on … as` typed-event handler, RUN on the
// JVM. docs/design/130-stream-reactive-types.md §6, §6b.
//
// An event is a `Stream[T]` element with a contract, so the handler is the Slice 4
// loop plus ONE gate. This proves the gate, and only the gate: a conforming item
// is validated against the derived schema and reaches the typed body; an in-window
// redelivery of the same identity key is COLLAPSED (the handler runs once per
// identity, and the collapse is traced rather than silent); a schema violation
// FAULTS the activation — the same terminal a provider abort delivers — so no
// malformed item reaches the body and the failed activation closes the
// subscription with no residue (§6, A8).

import io.cordis4j.core.Context;
import io.cordis4j.core.CordisException;
import io.cordis4j.core.ServiceKey;

public final class RunStreamEvent130 {
    private static final java.util.List<String> DELIVERED =
        java.util.Collections.synchronizedList(new java.util.ArrayList<>());

    static final class Recorder implements revl.Components.Sink {
        public void write(String v) {
            DELIVERED.add(v);
        }
    }

    private static void fail(String why) {
        System.err.println("typed event handler: " + why
            + " (marks=" + revl.Components.Stream.marks()
            + ", delivered=" + DELIVERED + ")");
        System.exit(1);
    }

    private static void expect(boolean ok, String why) {
        if (!ok) {
            fail(why);
        }
    }

    private static boolean marked(String mark) {
        return revl.Components.Stream.marks().contains(mark);
    }

    private static void await(String what, java.util.function.BooleanSupplier ready) {
        long deadline = System.nanoTime() + 10_000_000_000L;
        while (!ready.getAsBoolean()) {
            if (System.nanoTime() > deadline) {
                fail("timed out waiting for " + what);
            }
            try {
                Thread.sleep(2L);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
                fail("interrupted waiting for " + what);
            }
        }
    }

    public static void main(String[] args) throws Exception {
        DELIVERED.clear();
        revl.Components.Stream.reset();
        Context ctx = new Context();
        ctx.provide(ServiceKey.of(revl.Components.Sink.class, "sink"), new Recorder());

        java.util.concurrent.atomic.AtomicReference<Throwable> failure =
            new java.util.concurrent.atomic.AtomicReference<>();
        Thread activation = new Thread(() -> {
            try {
                new revl.Components.HandlerPlugin().apply(ctx);
            } catch (Throwable caught) {
                failure.set(caught);
            }
        }, "handler-activation");
        activation.setDaemon(true);
        activation.start();
        await("the subscription", () -> marked("stream.subscribe"));
        revl.Components.Stream provider = revl.Components.Stream.providers().get(0);

        // a conforming item is validated and reaches the typed body
        provider.emit("{\"order_id\": \"A-1\", \"quantity\": 2}");
        await("the first admitted item", () -> DELIVERED.size() == 1);
        expect(marked("event.OrderCreated admit"), "the admission was not traced");
        expect("A-1".equals(DELIVERED.get(0)),
            "the body did not read the typed field: " + DELIVERED);

        // a second, DIFFERENT identity runs the body again
        provider.emit("{\"order_id\": \"A-2\", \"quantity\": 5}");
        await("the second admitted item", () -> DELIVERED.size() == 2);

        // an in-window redelivery of A-1 is collapsed: the body does not run again,
        // and the collapse is traced rather than silent
        provider.emit("{\"order_id\": \"A-1\", \"quantity\": 2}");
        await("the duplicate to be collapsed",
            () -> marked("event.OrderCreated duplicate"));
        Thread.sleep(30L);
        expect(DELIVERED.size() == 2,
            "a redelivery of an admitted key ran the handler again: " + DELIVERED);

        // a schema violation FAULTS: quantity is an `Int`, so a string fails the
        // derived schema at the boundary and never reaches the body
        provider.emit("{\"order_id\": \"A-3\", \"quantity\": \"many\"}");
        await("the activation to fail", () -> !activation.isAlive());
        Throwable caught = failure.get();
        expect(caught instanceof CordisException,
            "a schema violation did not fault the activation: " + caught);
        expect(caught.getMessage().contains("failed its schema"),
            "the fault did not name the schema violation: " + caught.getMessage());
        expect(caught.getMessage().contains("quantity"),
            "the fault did not name the offending field: " + caught.getMessage());
        expect(DELIVERED.size() == 2,
            "the malformed item reached the body: " + DELIVERED);
        expect(revl.Components.Stream.liveSubscriptions() == 0,
            "the failed handler left its subscription active (§6, A8)");
        provider.close();
        expect(revl.Components.Stream.pendingResources() == 0,
            revl.Components.Stream.pendingResources()
                + " unreleased stream resource(s) after the fault");
        System.out.println("STREAM_EVENT_130_OK");
    }
}
