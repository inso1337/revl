package bench;

public final class Drive {

    public static final String NAME = "map_keys";
    public static final int N = 1;
    public static final int WARMUP = 20000;

    private static final int SIZE = 64;
    private static java.util.Map<String, Long> m;

    private Drive() {}

    public static void setup() {
        java.util.Map<String, Long> build = new java.util.HashMap<>();
        for (int i = 0; i < SIZE; i++) {
            build.put("key-" + i, (long) i);
        }
        m = java.util.Map.copyOf(build);

        if (!revl.Components.ks(m).equals(Hand.mapKeys(m))) {
            throw new AssertionError("emitted and hand-written disagree");
        }
    }

    public static long emitted(int n) {
        long sink = 0L;
        for (int i = 0; i < n; i++) {
            sink += revl.Components.ks(m).size();
        }
        return sink;
    }

    public static long hand(int n) {
        long sink = 0L;
        for (int i = 0; i < n; i++) {
            sink += Hand.mapKeys(m).size();
        }
        return sink;
    }
}
