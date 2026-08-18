// Characterization harness for a KNOWN, documented divergence of the Java
// tier from revl's realm contract (docs/contract-errata.md,
// "cordis4j global-realm divergence"; cross-tier gate:
// tests/test_realm_conformance.py). This is NOT a conformance test — it
// PINS the current (non-conforming) behavior so it cannot change silently.
//
// revl's contract (docs/design-v2-realms.md): equal realm-label strings
// denote the SAME realm. Two components that both `isolate kv in
// realm("t")` share one realm, so a second provider of `kv` in realm "t"
// is a G2 conflict and a consumer in realm "t" resolves the provider in
// realm "t". True at runtime on cordis-py / cordis (TS) / cordis-rs.
//
// FALSE on cordis4j: revl's emitter emits `ctx.isolate(Svc.class, "t")`
// inside each component's apply() (backends/java/emit.py:2127-2129), and
// core Context.isolate always mints a fresh child with its own store
// (core/internal/ContextImpl.java:160-168, ServiceRegistry.java:41). The
// label-keyed interning lives one layer up in Loader (core/Loader.java:67,
// :341-359), which revl's emitted plugins never reach. So equal strings
// SEPARATE instead of sharing. This harness asserts that divergence, plus
// the one direction that DOES conform (distinct strings separate).
//
// Driven by test_emit_java.py::test_global_realm_divergence_characterized,
// which emits examples/tenants.rvl and runs this against the real jar.
//
// Build + run (same shape as RunRealScenarios):
//   javac --release 21 -cp <c4j-classes> -d <out> Components.java RunRealmDivergence.java
//   java -cp <c4j-classes>:<out> RunRealmDivergence

import io.cordis4j.core.Context;
import io.cordis4j.core.Contexts;
import io.cordis4j.core.Disposable;

public final class RunRealmDivergence {

    private static void fail(String msg) {
        System.err.println("realm-divergence: " + msg);
        System.exit(1);
    }

    /** Present-and-visible kv at a context, for the "hidden in the isolate
     *  child" observation. */
    private static boolean kvVisible(Context ctx) {
        return ctx.find(revl.Components.Kv.class).isPresent();
    }

    public static void main(String[] args) {
        // ---- DIVERGENCE 1: two providers, SAME realm string --------------
        // Contract: one label = one realm, so the second provide of `kv` in
        // realm "tenant_a" is a G2 conflict (SupplyConflictException).
        // cordis4j: each apply() isolates into its OWN child store, so both
        // load and neither is visible at the root.
        Context root = Contexts.create();
        Disposable p1 = root.plugin(new revl.Components.TenantAStorePlugin());
        String conflict = null;
        Disposable p2 = null;
        try {
            p2 = root.plugin(new revl.Components.TenantAStorePlugin());
        } catch (RuntimeException e) {
            conflict = e.getClass().getSimpleName();
        }
        if (conflict != null) {
            fail("two same-realm providers conflicted (" + conflict + ") — Java now "
                + "shares by label; the divergence is CLOSED. Re-check the errata "
                + "entry and the cross-tier xfail: Java may now conform.");
        }
        System.out.println("D1/two providers, realm \"tenant_a\" -> BOTH LOADED "
            + "(contract wanted a G2 conflict)");
        if (kvVisible(root)) {
            fail("D1: expected the provision to be hidden in the isolate child, "
                + "but kv is visible at the root");
        }
        System.out.println("D1/root.find(kv) -> <absent> (each provision hides in its own child)");
        if (p2 != null) p2.dispose();
        p1.dispose();

        // ---- DIVERGENCE 2: consumer in realm "t" after provider in realm "t"
        // Contract: the consumer resolves the provider (same realm). cordis4j:
        // the consumer's isolate child is a DIFFERENT context, so its
        // ctx.get(Kv.class) misses -> NoSuchServiceException out of apply().
        root = Contexts.create();
        Disposable prov = root.plugin(new revl.Components.TenantAStorePlugin());
        String consumerErr = null;
        try {
            root.plugin(new revl.Components.TenantAAppPlugin());
        } catch (RuntimeException e) {
            consumerErr = e.getClass().getSimpleName();
        }
        if (consumerErr == null) {
            fail("consumer in realm \"tenant_a\" RESOLVED the provider in realm "
                + "\"tenant_a\" — Java now shares by label; the divergence is CLOSED. "
                + "Re-check the errata entry and the cross-tier xfail: Java may now conform.");
        }
        System.out.println("D2/consumer(realm \"tenant_a\") after provider(realm \"tenant_a\") -> "
            + consumerErr + " (contract wanted RESOLVED)");
        prov.dispose();

        // ---- CONFORMS: DISTINCT realm strings separate ------------------
        // This direction matches the contract on every tier, Java included:
        // tenant_a and tenant_b are different realms, so both providers load
        // with no conflict.
        root = Contexts.create();
        Disposable a = root.plugin(new revl.Components.TenantAStorePlugin());
        String distinctErr = null;
        Disposable b = null;
        try {
            b = root.plugin(new revl.Components.TenantBStorePlugin());
        } catch (RuntimeException e) {
            distinctErr = e.getClass().getSimpleName();
        }
        if (distinctErr != null) {
            fail("distinct realms (tenant_a / tenant_b) must both load, but got "
                + distinctErr);
        }
        System.out.println("OK/distinct realms tenant_a + tenant_b -> BOTH LOADED (conforms)");
        if (b != null) b.dispose();
        a.dispose();

        System.out.println("REALM_DIVERGENCE_CHARACTERIZED");
    }
}
