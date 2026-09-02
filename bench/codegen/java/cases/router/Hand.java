package bench;

import io.cordis4j.core.Context;
import io.cordis4j.core.CordisException;
import io.cordis4j.core.Disposable;
import io.cordis4j.core.Plugin;
import io.cordis4j.core.ServiceKey;

/**
 * What a competent Java developer writes for a routed require.
 *
 * The class shape is the emitter's, down to the two-hop forward through a
 * provider struct, so the A/B measures the SELECTION and nothing else. Four
 * things change inside `select`:
 *
 * 1. The strategy is a compile-time constant. The emitter knows which one the
 *    author wrote, so the unchosen branch and the `String strategy` field it
 *    tests are dead weight in every call. Here only round_robin exists. (An
 *    emitter would render whichever strategy the route declares.)
 * 2. The liveness probe keeps the handle it just resolved instead of throwing
 *    it away and calling `serviceInRealm` a second time for the winner. This
 *    is exactly what backends/go/emit.py already does: its `_revlLive`
 *    returns `map[string]Worker` alongside the labels.
 * 3. No `ArrayList` of live labels, and therefore no `live.contains(cand)`
 *    linear scan nested inside the candidate loop.
 * 4. `served` counters are a `long[]` indexed by realm position, not a
 *    `HashMap<String, Long>` that boxes a fresh Long past 127 on every call.
 *
 * In the common case where the first candidate is live, that is one
 * `serviceInRealm` call and zero allocation, against the emitted form's
 * realms+1 calls, one ArrayList, up to realms Optionals and a boxed counter.
 */
public final class Hand {

    public interface Worker {
        String call(String request);
    }

    public static final class W implements Worker {
        private final String label;

        W(String label) {
            this.label = label;
        }

        @Override
        public String call(String request) {
            return label + ":" + request;
        }
    }

    public static final class WPlugin implements Plugin {
        private final String realm;

        public WPlugin(String realm) {
            this.realm = realm;
        }

        @Override
        public Disposable apply(Context ctx) {
            Context scoped = ctx.isolate(Worker.class, realm);
            Context.EffectScope fx = scoped.effect();
            fx.track(scoped.provide(ServiceKey.of(Worker.class), new W(realm)));
            return fx;
        }
    }

    /** The router itself: the part under audit. */
    public static final class Router implements Worker {
        private final Context ctx;
        private final String[] realms;
        private final long[] served;
        private int cursor = 0;

        Router(Context ctx, String... realms) {
            this.ctx = ctx;
            this.realms = realms;
            this.served = new long[realms.length];
        }

        private Worker select() {
            int n = realms.length;
            for (int off = 0; off < n; off++) {
                int idx = (cursor + off) % n;
                java.util.Optional<Worker> hit = ctx.serviceInRealm(Worker.class, realms[idx]);
                if (hit.isPresent()) {
                    cursor = (idx + 1) % n;
                    served[idx]++;
                    return hit.get();
                }
            }
            throw new CordisException(
                    "revl: router for worker has no live worker (all realms withdrawn)");
        }

        @Override
        public String call(String request) {
            return select().call(request);
        }
    }

    /** The provider struct the emitter puts between the key and the router. */
    public static final class RouterWorker implements Worker {
        private final Worker worker;

        RouterWorker(Worker worker) {
            this.worker = worker;
        }

        @Override
        public String call(String request) {
            return this.worker.call(request);
        }
    }

    public static final class RouterPlugin implements Plugin {
        public RouterPlugin() {
        }

        @Override
        public Disposable apply(Context ctx) {
            Context.EffectScope fx = ctx.effect();
            Worker worker = new Router(ctx, "w1", "w2", "w3");
            fx.track(ctx.provide(ServiceKey.of(Worker.class), new RouterWorker(worker)));
            return fx;
        }
    }

    private Hand() {}
}
