// Runtime-independent VALUE checks over emitted revl.Components — shared by
// the real-jar harness (RunRuntimeValues) and the offline stub harness
// (RunRuntimeValuesStub). None of these touch cordis4j: they execute the
// output of the consolidated expression renderer and the Pool host runtime
// and assert the produced values, coverage that was previously only
// javac-compiled or golden-matched.
//
// Returns the number of failed assertions (0 == all passed).

public final class RuntimeValueChecks {
    private RuntimeValueChecks() {}

    private static int failures = 0;

    private static void eq(String what, Object got, Object want) {
        if (!java.util.Objects.equals(got, want)) {
            System.err.println("FAIL " + what + ": got <" + got + "> want <" + want + ">");
            failures++;
        }
    }

    private static void expectThrows(String what, Runnable body) {
        try {
            body.run();
            System.err.println("FAIL " + what + ": expected an exception, none thrown");
            failures++;
        } catch (IllegalStateException expected) {
            // ok
        }
    }

    public static int run() {
        failures = 0;

        // --- match: branch selection + payload binding --------------------
        var row = new revl.Components.Row(7L, "revl");
        eq("match Ok binds the record and reads its field",
            revl.Components.describe(new revl.Components.Outcome.Ok(row)), "revl");
        eq("match NotFound selects the nullary arm",
            revl.Components.describe(new revl.Components.Outcome.NotFound()), "none");
        eq("match Invalid binds the string payload",
            revl.Components.describe(new revl.Components.Outcome.Invalid("bad input")), "bad input");

        // --- string interpolation with a literal % ------------------------
        eq("interpolation carries a literal %",
            revl.Components.greet("x", 42L), "hi x: 42% done");

        // --- stdlib typed overloads: String family ------------------------
        eq("String.slice", revl.Components.head("revl"), "r");
        eq("String.concat", revl.Components.catS("re", "vl"), "revl");
        eq("String.indexOf (hit)", revl.Components.findS("revl", "ev"), 1L);
        eq("String.indexOf (miss = -1)", revl.Components.findS("revl", "zz"), -1L);
        eq("String.split keeps a trailing empty", revl.Components.parts("a,", ",").size(), 2);
        eq("String.split empty-sep -> chars", revl.Components.parts("abc", "").size(), 3);
        eq("List<String>.join round-trips split",
            revl.Components.glue(revl.Components.parts("a-b", "-"), "+"), "a+b");
        eq("String.repeat", revl.Components.rep("ab", 3L), "ababab");

        // --- stdlib typed overloads: List family --------------------------
        eq("List.concat length",
            revl.Components.catL(java.util.List.of(1L), java.util.List.of(2L, 3L)).size(), 3);
        eq("List.indexOf (hit)", revl.Components.findL(java.util.List.of(4L, 5L, 6L), 6L), 2L);
        eq("List.indexOf (miss = -1)", revl.Components.findL(java.util.List.of(4L, 5L, 6L), 9L), -1L);
        var pushed = revl.Components.pushL(java.util.List.of(1L, 2L), 3L);
        eq("List.push length", pushed.size(), 3);
        eq("List.push appends at the tail", pushed.get(2), 3L);

        // --- Pool: statement lowering + capacity accounting ---------------
        // execute() reports rows-affected; the golden pinned the STRING `1L`,
        // this runs it. It also exercises the field-call lowering fix (the
        // emitter used to produce `Pool.open.apply(..)`, which never compiled).
        eq("Pool.execute reports rows-affected",
            revl.Components.poolExec("postgres://db"), 1L);

        // The accounting the golden only string-checked, now executed.
        var pool = revl.Components.Pool.open("postgres://db", 2L);
        eq("Pool.capacity", pool.capacity(), 2L);
        eq("Pool.available (fresh)", pool.available(), 2L);
        long c1 = pool.acquire();
        pool.acquire();
        eq("Pool.inUse after two acquires", pool.inUse(), 2L);
        eq("Pool.available after two acquires", pool.available(), 0L);
        expectThrows("Pool.acquire past capacity throws", pool::acquire);
        pool.release(c1);
        eq("Pool.available after release", pool.available(), 1L);
        eq("Pool.inUse after release", pool.inUse(), 1L);
        pool.close();
        eq("Pool.capacity after close", pool.capacity(), 0L);
        expectThrows("Pool.acquire after close is use-after-free", pool::acquire);

        return failures;
    }
}
