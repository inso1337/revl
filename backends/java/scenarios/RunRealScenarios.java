// A1/G7 runtime scenarios against the REAL cordis4j runtime
// (github.com/1na-ko/cordis4j, cordis4j-core compiled from source).
//
// Mirrors the Rust scenarios (backends/rust/scenarios/scenarios.rs):
// - G7: teardown runs inverses in reverse acquisition order
// - A8: a failing activation reverts its partial domain and rethrows
// - reactive lifecycle: an inject-gated consumer activates when its
//   provider appears and deactivates (before the provider's own effects
//   revert — Theorem 63) on withdrawal
// - A1 boundary shape via pluginAsync
//
// The stub harness (RunScenarios.java) stays as the always-on gate; this
// one runs when compiled cordis4j classes are supplied (see
// test_emit_java.py::test_runtime_scenarios_on_real_cordis4j).

import io.cordis4j.core.Context;
import io.cordis4j.core.Contexts;
import io.cordis4j.core.Disposable;
import io.cordis4j.core.ServiceKey;

public final class RunRealScenarios {
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

    private static Context freshRoot() {
        LOG.clear();
        Context root = Contexts.create();
        root.provide(ServiceKey.of(revl.Components.Probe.class), new Rec());
        return root;
    }

    public static void main(String[] args) throws Exception {
        // G7: LIFO teardown on the real fiber domain.
        Context root = freshRoot();
        Disposable d = root.plugin(new revl.Components.TwoStepsPlugin());
        expect("g7 activation", "do:a", "do:b");
        d.dispose();
        expect("g7 teardown", "do:a", "do:b", "undo:b", "undo:a");

        // A8: failing activation reverts the accumulated effect and the
        // failure routes to the caller (paper §4.3.4).
        root = freshRoot();
        boolean raised = false;
        try {
            root.plugin(new revl.Components.FailsAfterPlugin());
        } catch (RuntimeException e) {
            raised = true;
        }
        if (!raised) {
            System.err.println("a8: activation failure must reach the caller");
            System.exit(1);
        }
        expect("a8 revert", "do:a", "undo:a");

        // Reactive lifecycle: consumer gated on kv, deactivates on
        // withdrawal BEFORE the provider's own effects revert.
        root = freshRoot();
        java.util.Set<ServiceKey<?>> deps = java.util.Set.of(
            ServiceKey.of(revl.Components.Kv.class),
            ServiceKey.of(revl.Components.Probe.class));
        final Context injectRoot = root;
        Disposable consumer = root.inject(deps,
            ctx -> new revl.Components.KvConsumerPlugin().apply(ctx));
        if (!LOG.isEmpty()) {
            System.err.println("consumer must not activate before kv exists: " + LOG);
            System.exit(1);
        }
        Disposable provider = root.plugin(new revl.Components.KvProviderPlugin());
        expect("reactive activation", "provider:up", "consumer:up");
        provider.dispose();
        var after = new java.util.ArrayList<>(LOG);
        int c = after.indexOf("consumer:down");
        int p = after.indexOf("provider:down");
        if (c < 0 || p < 0 || c > p) {
            System.err.println("withdrawal must deactivate the consumer before the "
                + "provider's effects revert (Theorem 63): " + after);
            System.exit(1);
        }
        consumer.dispose();

        // A1 boundary shape through the real async path.
        root = freshRoot();
        revl.Components.Job.reset();
        d = root.pluginAsync(new revl.Components.BoundaryPlugin());
        expect("boundary activation", "do:a", "do:b", "emit:post");
        // Crossing the boundary means *joining* the handle: the emitter used
        // to lower the step to a bare `Job.run("boundary");`, which starts the
        // job and drops the handle, so activation returned with the job still
        // in flight on cordis4j. Job.pending() makes that residue countable.
        expectNoPendingJobs("boundary activation");
        d.dispose();
        expect("boundary teardown", "do:a", "do:b", "emit:post", "undo:b", "undo:a");
        expectNoPendingJobs("boundary teardown");

        System.out.println("REAL_SCENARIOS_OK");
    }
}
