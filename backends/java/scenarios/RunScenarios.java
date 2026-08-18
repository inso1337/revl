// G7/lifecycle scenarios for emitted Java, driven by the stub reference
// runtime in ../stubs (LIFO EffectScope/composite, per the DESIGN §7
// contract). This verifies the EMITTED code — registration order, provision
// tracking, teardown order — against a spec-conforming runtime; validating
// against the real cordis4j jar is tracked in docs/v2.0-roadmap.md.
//
// A8 (fail-revert) and reactive Inject-gating need the real runtime's
// machinery and are deliberately absent here; the Rust backend covers them
// against real cordis-rs.

import io.cordis4j.core.Context;
import io.cordis4j.core.Disposable;
import io.cordis4j.core.ServiceKey;

public final class RunScenarios {
    private static final java.util.List<String> LOG = new java.util.ArrayList<>();

    static final class Rec implements revl.Components.Probe {
        public long mark(String m) { LOG.add(m); return 0L; }
        public long send(String m) { LOG.add("emit:" + m); return 0L; }
    }

    private static void expect(String scenario, String... want) {
        var wanted = java.util.List.of(want);
        if (!LOG.equals(wanted)) {
            System.err.println(scenario + ": expected " + wanted + " but got " + LOG);
            System.exit(1);
        }
    }

    private static void expectNoPendingJobs(String scenario) {
        long pending = revl.Components.Job.pending();
        if (pending != 0L) {
            System.err.println(scenario + ": " + pending + " job(s) still pending — "
                + "the `await` step evaluated the call without joining the handle");
            System.exit(1);
        }
    }

    private static Context freshContext() {
        LOG.clear();
        Context ctx = new Context();
        ctx.provide(ServiceKey.of(revl.Components.Probe.class), new Rec());
        return ctx;
    }

    public static void main(String[] args) throws Exception {
        // G7: teardown runs inverses in reverse acquisition order.
        Context ctx = freshContext();
        Disposable d = new revl.Components.TwoStepsPlugin().apply(ctx);
        expect("g7 activation", "do:a", "do:b");
        d.dispose();
        expect("g7 teardown", "do:a", "do:b", "undo:b", "undo:a");

        // A1 boundary shape: activation crosses the await, the
        // post-boundary emission runs, disposal reverts LIFO.
        ctx = freshContext();
        revl.Components.Job.reset();
        d = new revl.Components.BoundaryPlugin().apply(ctx);
        expect("boundary activation", "do:a", "do:b", "emit:post");
        // ...and *crossing* it means joining the handle. The emitter used to
        // lower the step to a bare `Job.run("boundary");`, which starts the
        // job and drops the handle, so activation returned with the job still
        // in flight. Job.pending() makes that residue countable.
        expectNoPendingJobs("boundary activation");
        d.dispose();
        expect("boundary teardown", "do:a", "do:b", "emit:post", "undo:b", "undo:a");
        expectNoPendingJobs("boundary teardown");

        // Provider/consumer: the provision disposable is tracked (review
        // finding J8) — after provider disposal the service is gone.
        ctx = freshContext();
        Disposable provider = new revl.Components.KvProviderPlugin().apply(ctx);
        Disposable consumer = new revl.Components.KvConsumerPlugin().apply(ctx);
        expect("pair activation", "provider:up", "consumer:up");
        consumer.dispose();
        provider.dispose();
        expect("pair teardown", "provider:up", "consumer:up",
               "consumer:down", "provider:down");
        boolean withdrawn;
        try {
            ctx.get(revl.Components.Kv.class);
            withdrawn = false;
        } catch (RuntimeException e) {
            withdrawn = true;
        }
        if (!withdrawn) {
            System.err.println("kv provision must be withdrawn after provider disposal");
            System.exit(1);
        }

        System.out.println("SCENARIOS_OK");
    }
}
