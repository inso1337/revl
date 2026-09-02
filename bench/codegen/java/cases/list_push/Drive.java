package bench;

public final class Drive {

    public static final String NAME = "list_push";
    public static final int N = 1;
    public static final int WARMUP = 2000;

    /** One op builds a 256-element list, so 256 persistent appends. */
    private static final long SIZE = 256L;

    private Drive() {}

    public static void setup() {
        java.util.List<Long> a = revl.Components.build(SIZE);
        java.util.List<Long> b = Hand.build(SIZE);
        if (!a.equals(b)) {
            throw new AssertionError("emitted and hand-written disagree");
        }
    }

    public static long emitted(int n) {
        long sink = 0L;
        for (int i = 0; i < n; i++) {
            sink += revl.Components.build(SIZE).size();
        }
        return sink;
    }

    public static long hand(int n) {
        long sink = 0L;
        for (int i = 0; i < n; i++) {
            sink += Hand.build(SIZE).size();
        }
        return sink;
    }
}
