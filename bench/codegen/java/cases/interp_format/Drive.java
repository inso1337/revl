package bench;

import io.cordis4j.core.Context;

public final class Drive {

    public static final String NAME = "interp_format";
    public static final int N = 1000;
    public static final int WARMUP = 2000;

    private static revl.Components.Logger emittedSvc;
    private static Hand.Logger handSvc;

    private Drive() {}

    public static void setup() {
        Context emittedCtx = new Context();
        new revl.Components.LPlugin().apply(emittedCtx);
        emittedSvc = emittedCtx.get(revl.Components.Logger.class);

        Context handCtx = new Context();
        new Hand.LPlugin().apply(handCtx);
        handSvc = handCtx.get(Hand.Logger.class);

        String a = emittedSvc.log("payload", 7L);
        String b = handSvc.log("payload", 7L);
        if (!a.equals(b)) {
            throw new AssertionError("emitted and hand-written disagree: " + a + " != " + b);
        }
    }

    public static long emitted(int n) {
        long sink = 0L;
        for (int i = 0; i < n; i++) {
            sink += emittedSvc.log("payload", i).length();
        }
        return sink;
    }

    public static long hand(int n) {
        long sink = 0L;
        for (int i = 0; i < n; i++) {
            sink += handSvc.log("payload", i).length();
        }
        return sink;
    }
}
