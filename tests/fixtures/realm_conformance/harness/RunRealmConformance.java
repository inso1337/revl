// cordis4j realm-conformance runner (driven by tests/test_realm_conformance.py).
// Same contract as the other tiers, on the REAL cordis4j runtime.
//
// EXPECTED DIVERGENCE: on cordis4j `ctx.isolate(Kv.class, "shared")` forks a
// context whose ServiceRegistry.store is its own, so two providers isolated
// into the same realm string each register in their own store and BOTH LOAD
// instead of conflicting. That contradicts docs/design-v2-realms.md's
// "equal strings = same realm" / "the same realm is the conflict". The Python
// gate marks this tier xfail(strict); see
// docs/notes/runtime-parity-local-realms.md.
import io.cordis4j.core.Context;
import io.cordis4j.core.Contexts;
import io.cordis4j.core.Disposable;

public final class RunRealmConformance {
    public static void main(String[] args) {
        // (H) two providers of kv in the SAME realm string.
        Context root = Contexts.create();
        root.plugin(new revl.Components.SharedStoreAPlugin());
        boolean refused = false;
        try {
            root.plugin(new revl.Components.SharedStoreBPlugin());
        } catch (RuntimeException e) {
            refused = true;
        }
        System.out.println("H_VERDICT " + (refused ? "REFUSED" : "BOTH_ACTIVE"));

        // (S) two providers in DIFFERENT realm strings -> distinct, independent.
        Context root2 = Contexts.create();
        Disposable a2 = root2.plugin(new revl.Components.SharedStoreAPlugin());
        Disposable o2 = root2.plugin(new revl.Components.SharedStoreOtherPlugin());
        boolean bothLoaded = a2 != null && o2 != null;
        a2.dispose();
        System.out.println("S_VERDICT " + (bothLoaded ? "SEPARATE" : "FAIL"));

        System.out.println("RC_DONE");
    }
}
