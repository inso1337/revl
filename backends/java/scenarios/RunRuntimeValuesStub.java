// Offline mirror of RunRuntimeValues on the stub reference runtime (the
// concrete `new Context()` + stub EffectScope in ../stubs). Runs the same
// runtime-independent value checks (RuntimeValueChecks) and the method-time
// `compensate` ordering, driving the component directly through `apply(ctx)`
// (the stub has no Contexts/plugin façade). This is the always-on gate; the
// real-jar coverage is RunRuntimeValues, driven when REVL_CORDIS4J_CLASSES is
// supplied.

import io.cordis4j.core.Context;
import io.cordis4j.core.Disposable;
import io.cordis4j.core.ServiceKey;

public final class RunRuntimeValuesStub {
    private static final java.util.List<String> LOG = new java.util.ArrayList<>();

    static final class Rec implements revl.Components.Probe {
        public long mark(String m) { LOG.add("undo:" + m); return 0L; }
        public long send(String m) { LOG.add("do:" + m); return 0L; }
    }

    public static void main(String[] args) throws Exception {
        int failures = RuntimeValueChecks.run();

        // --- method-time compensation ordering on the stub EffectScope ----
        Context ctx = new Context();
        ctx.provide(ServiceKey.of(revl.Components.Probe.class), new Rec());
        Disposable comp = new revl.Components.CPlugin().apply(ctx);
        revl.Components.N n = ctx.get(revl.Components.N.class);
        n.ping("a");
        n.ping("b");
        var duringActivation = java.util.List.of("do:a", "do:b");
        if (!LOG.equals(duringActivation)) {
            System.err.println("FAIL method-time emissions must run eagerly, "
                + "compensations must not: " + LOG);
            failures++;
        }
        comp.dispose();
        // TCK A5 respec (docs/design/teardown-contract.md, exit test 1, a5a):
        // `comp.dispose()` here is a CLEAN unload (no exception ever propagated
        // out of apply()/ping()) — an implicit commit until item 245 lands the
        // explicit commit UX. A committed `compensation` entry is DISCHARGED,
        // never run: the forward emission was the deliverable, and best-effort
        // cleanup on success is wrong (247 decision 1). This inverts the OLD
        // placeholder assertion, which wrongly expected the compensations to
        // fire on every teardown including a clean one.
        var afterTeardown = java.util.List.of("do:a", "do:b");
        if (!LOG.equals(afterTeardown)) {
            System.err.println("FAIL a clean unload must DISCHARGE compensations, never run them: "
                + "got " + LOG + " want " + afterTeardown);
            failures++;
        }

        if (failures != 0) {
            System.err.println(failures + " runtime-value assertion(s) failed");
            System.exit(1);
        }
        System.out.println("RUNTIME_VALUES_OK");
    }
}
