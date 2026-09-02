package bench;

import io.cordis4j.core.Context;

public final class Drive {

    public static final String NAME = "router";
    public static final int N = 1000;
    public static final int WARMUP = 2000;

    private static revl.Components.Worker emittedSvc;
    private static Hand.Worker handSvc;

    private Drive() {}

    public static void setup() {
        Context emittedCtx = new Context();
        new revl.Components.W1Plugin().apply(emittedCtx);
        new revl.Components.W2Plugin().apply(emittedCtx);
        new revl.Components.W3Plugin().apply(emittedCtx);
        new revl.Components.RouterPlugin().apply(emittedCtx);
        emittedSvc = emittedCtx.get(revl.Components.Worker.class);

        Context handCtx = new Context();
        new Hand.WPlugin("w1").apply(handCtx);
        new Hand.WPlugin("w2").apply(handCtx);
        new Hand.WPlugin("w3").apply(handCtx);
        new Hand.RouterPlugin().apply(handCtx);
        handSvc = handCtx.get(Hand.Worker.class);

        // Both must round-robin the same three realms in the same order.
        for (int i = 0; i < 6; i++) {
            String a = emittedSvc.call("r");
            String b = handSvc.call("r");
            if (!a.equals(b)) {
                throw new AssertionError("emitted and hand-written disagree: " + a + " != " + b);
            }
        }
    }

    public static long emitted(int n) {
        long sink = 0L;
        for (int i = 0; i < n; i++) {
            sink += emittedSvc.call("r").length();
        }
        return sink;
    }

    public static long hand(int n) {
        long sink = 0L;
        for (int i = 0; i < n; i++) {
            sink += handSvc.call("r").length();
        }
        return sink;
    }
}
