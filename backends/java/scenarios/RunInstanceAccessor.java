// Instance accessor (`s.<key>`) against the REAL cordis4j runtime
// (github.com/1na-ko/cordis4j, cordis4j-core compiled from source).
//
// Drives the emitted composition from scenarios/instance_accessor.rvl and
// proves the three DoD properties of docs/design-v2-instances.md's
// "Instance accessor — frozen" by RUNNING:
//
//   1. positive — `s.<key>.method(..)` through a spawn handle returns THAT
//      spawned instance's provision: read_a (w1's handle) -> 7, read_b
//      (w2's handle) -> 9;
//   2. per-instance — the two handles resolve DISTINCT realms (7 != 9), so
//      neither reaches the other's provision (sibling isolation);
//   3. negative — the root (a stand-in for any sibling/outside party) cannot
//      resolve `Counter` at all: each worker isolated its provision into its
//      own private local realm.
//
// Runs when compiled cordis4j classes are supplied (see
// test_emit_java.py::test_instance_accessor_on_real_cordis4j).

import io.cordis4j.core.Context;
import io.cordis4j.core.Contexts;

public final class RunInstanceAccessor {
    private static void fail(String message) {
        System.err.println(message);
        System.exit(1);
    }

    public static void main(String[] args) {
        Context root = Contexts.create();
        root.plugin(new revl.Components.SupervisorPlugin());

        // (1) positive supervision-tree direction: each read goes through the
        // handle its spawner alone holds and resolves THAT instance's counter.
        revl.Components.Reader reader = root.get(revl.Components.Reader.class);
        long a = reader.read_a();
        long b = reader.read_b();
        if (a != 7L) {
            fail("read_a expected 7 (w1's own provision) but got " + a);
        }
        if (b != 9L) {
            fail("read_b expected 9 (w2's own provision) but got " + b);
        }
        // (2) the handles resolve DISTINCT realms — a shared/root realm would
        // collapse both onto one value.
        if (a == b) {
            fail("both handles resolved the same provision (" + a + ") — "
                + "the accessor is not resolving per-instance realms");
        }

        // (3) negative: the instance's provision stays private to its local
        // realm — the root cannot resolve Counter (throws NoSuchService).
        boolean rootBlocked = false;
        try {
            root.get(revl.Components.Counter.class);
        } catch (RuntimeException e) {
            rootBlocked = true;
        }
        if (!rootBlocked) {
            fail("root resolved Counter — the instance's provision escaped its "
                + "private local realm (supervision-tree addressing broken)");
        }

        System.out.println("INSTANCE_ACCESSOR_OK a=" + a + " b=" + b);
    }
}
