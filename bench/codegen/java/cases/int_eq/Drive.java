package bench;

public final class Drive {

    public static final String NAME = "int_eq";
    public static final int N = 1;
    public static final int WARMUP = 3000;

    /**
     * Values are pushed well past the `Long.valueOf` cache (-128..127) so the
     * boxing the emitted form performs is a real heap allocation rather than a
     * cache hit. A workload that stayed inside the cache would understate the
     * cost by exactly the amount that matters.
     */
    private static final int SIZE = 4096;
    private static final long BASE = 1_000_000L;
    private static java.util.List<Long> xs;

    private Drive() {}

    public static void setup() {
        java.util.List<Long> build = new java.util.ArrayList<>(SIZE);
        for (int i = 0; i < SIZE; i++) {
            build.add(BASE + i);
        }
        xs = java.util.List.copyOf(build);

        long a = revl.Components.count_eq(xs, BASE + 17L);
        long b = Hand.countEq(xs, BASE + 17L);
        if (a != b || a != 1L) {
            throw new AssertionError("emitted and hand-written disagree: " + a + " != " + b);
        }
    }

    public static long emitted(int n) {
        long sink = 0L;
        for (int i = 0; i < n; i++) {
            sink += revl.Components.count_eq(xs, BASE + i);
        }
        return sink;
    }

    public static long hand(int n) {
        long sink = 0L;
        for (int i = 0; i < n; i++) {
            sink += Hand.countEq(xs, BASE + i);
        }
        return sink;
    }
}
