// Runtime-VALUE scenarios against the REAL cordis4j runtime
// (github.com/1na-ko/cordis4j, cordis4j-core compiled from source).
//
// RunRealScenarios.java proves the lifecycle *ordering* (G7/A8/reactive/async);
// this harness proves the *values* the consolidated expression renderer, the
// stdlib overloads and the Pool host runtime produce (RuntimeValueChecks),
// plus method-time `compensate` ordering on a REAL EffectScope — coverage that
// was previously only javac-compiled or golden-matched.
//
// Driven by test_emit_java.py::test_runtime_values_on_real_cordis4j. The
// offline mirror is RunRuntimeValuesStub (stub EffectScope), the same split as
// RunRealScenarios/RunScenarios: the real Context is an interface built via
// Contexts.create(), the stub is a concrete `new Context()`.

import io.cordis4j.core.Context;
import io.cordis4j.core.Contexts;
import io.cordis4j.core.Disposable;
import io.cordis4j.core.ServiceKey;

public final class RunRuntimeValues {
    private static final java.util.List<String> LOG = new java.util.ArrayList<>();

    // The method-time recorder tags emissions `do:` and compensations `undo:`
    // so the teardown ordering is legible in one flat log.
    static final class Rec implements revl.Components.Probe {
        public long mark(String m) { LOG.add("undo:" + m); return 0L; }
        public long send(String m) { LOG.add("do:" + m); return 0L; }
    }

    public static void main(String[] args) throws Exception {
        int failures = RuntimeValueChecks.run();

        // --- method-time compensation ordering on a real EffectScope ------
        Context root = Contexts.create();
        root.provide(ServiceKey.of(revl.Components.Probe.class), new Rec());
        Disposable comp = root.plugin(new revl.Components.CPlugin());
        revl.Components.N n = root.get(revl.Components.N.class);
        n.ping("a");
        n.ping("b");
        // Both emissions have run; no compensation has (teardown hasn't happened).
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
