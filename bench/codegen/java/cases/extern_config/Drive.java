package bench;

public final class Drive {

    public static final String NAME = "extern_config";
    public static final int N = 1000;
    public static final int WARMUP = 2000;

    private Drive() {}

    public static void setup() {
        String a = revl.Components.go("body");
        String b = Hand.go("body");
        if (!a.equals(b)) {
            throw new AssertionError("emitted and hand-written disagree: " + a + " != " + b);
        }
    }

    public static long emitted(int n) {
        long sink = 0L;
        for (int i = 0; i < n; i++) {
            sink += revl.Components.go("body").length();
        }
        return sink;
    }

    public static long hand(int n) {
        long sink = 0L;
        for (int i = 0; i < n; i++) {
            sink += Hand.go("body").length();
        }
        return sink;
    }
}
