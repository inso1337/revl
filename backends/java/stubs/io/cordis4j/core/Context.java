package io.cordis4j.core;

public class Context {
    // item 173: the registry is realm-aware. A shared store (one per context
    // tree, like the go runtime's `shared`) holds the root realm's providers
    // plus one map per NAMED realm. A context remembers, per service type, the
    // realm an enclosing `isolate` bound it to, so `provide`/`get` resolve into
    // that realm. `serviceInRealm` reads ONE realm strictly (the routing
    // primitive). A routes-less program never touches the realm maps, so its
    // resolution is exactly the old flat behavior.
    //
    // Within a realm, providers are keyed by SERVICE TYPE and PROVISION KEY, not
    // by type alone: `provide(ServiceKey.of(Db.class, "readonly"), ..)` and
    // `provide(ServiceKey.of(Db.class, "admin"), ..)` coexist, and a consumer
    // that requires `readonly` binds the readonly provider (the keyed `get`
    // below). Keying only by the class overwrote the first provider with the
    // second and handed every consumer whichever registered last — the go, rust,
    // wasm and python tiers already key by the provision name.
    private static final class Shared {
        final java.util.Map<Class<?>, java.util.Map<String, Object>> root =
                new java.util.HashMap<>();
        final java.util.Map<String, java.util.Map<Class<?>, java.util.Map<String, Object>>> realms =
                new java.util.HashMap<>();

        java.util.Map<Class<?>, java.util.Map<String, Object>> realm(String name) {
            return realms.computeIfAbsent(name, k -> new java.util.HashMap<>());
        }
    }

    private final Shared shared;
    private final java.util.Map<Class<?>, String> realmOf; // service type -> realm

    public Context() {
        this(new Shared(), new java.util.HashMap<>());
    }

    private Context(Shared shared, java.util.Map<Class<?>, String> realmOf) {
        this.shared = shared;
        this.realmOf = realmOf;
    }

    private java.util.Map<Class<?>, java.util.Map<String, Object>> storeFor(Class<?> type) {
        String realm = realmOf.get(type);
        return realm == null ? shared.root : shared.realm(realm);
    }

    /** The provider a plain type-only read resolves to: the default (unnamed)
     *  provision when one exists, else any provision of that type. Callers that
     *  must distinguish two providers of one type use the keyed `get` below;
     *  this overload answers "is there a provider of this type" (liveness /
     *  residue reads and a spawn instance reading its own single provision). */
    private static Object pick(java.util.Map<String, Object> byName) {
        if (byName == null || byName.isEmpty()) {
            return null;
        }
        Object def = byName.get("");
        if (def != null) {
            return def;
        }
        return byName.values().iterator().next();
    }

    /** Committed-view read: this context's realm for the key, else the root
     *  realm (the parent-chain fallback the router must NOT use). */
    public <T> T get(Class<T> type) {
        java.util.Map<Class<?>, java.util.Map<String, Object>> store = storeFor(type);
        Object value = pick(store.get(type));
        if (value == null && store != shared.root) {
            value = pick(shared.root.get(type)); // fallback to the shared realm
        }
        if (value == null) {
            throw new CordisException("no provider for " + type.getName());
        }
        return type.cast(value);
    }

    /** Committed-view read of the provision registered under `name`. This is the
     *  read an emitted `requires <name>: <Svc>` resolves through, so two
     *  providers of one service type route by key instead of colliding. */
    public <T> T get(Class<T> type, String name) {
        String key = name == null ? "" : name;
        java.util.Map<Class<?>, java.util.Map<String, Object>> store = storeFor(type);
        Object value = null;
        java.util.Map<String, Object> byName = store.get(type);
        if (byName != null) {
            value = byName.get(key);
        }
        if (value == null && store != shared.root) {
            java.util.Map<String, Object> rootByName = shared.root.get(type);
            if (rootByName != null) {
                value = rootByName.get(key); // fallback to the shared realm
            }
        }
        if (value == null) {
            throw new CordisException(
                    "no provider for " + type.getName() + " under key \"" + key + "\"");
        }
        return type.cast(value);
    }

    /**
     * item 173: STRICT single-realm liveness-checked read. Resolves `type`
     * ONLY in realm `realm` — an empty Optional when that realm has no active
     * provider, with NO fallback to a parent/root realm. Map membership is
     * liveness: provide() inserts, its Disposable removes. This is the read a
     * router's emitted body needs so a withdrawn worker realm drops out of the
     * live set instead of resolving to the router's own root provision.
     */
    public <T> java.util.Optional<T> serviceInRealm(Class<T> type, String realm) {
        java.util.Map<Class<?>, java.util.Map<String, Object>> store = shared.realms.get(realm);
        if (store == null) {
            return java.util.Optional.empty();
        }
        Object value = pick(store.get(type));
        if (value == null) {
            return java.util.Optional.empty();
        }
        return java.util.Optional.of(type.cast(value));
    }

    public <T> Disposable provide(ServiceKey<T> key, T impl) {
        java.util.Map<Class<?>, java.util.Map<String, Object>> store = storeFor(key.type());
        java.util.Map<String, Object> byName =
                store.computeIfAbsent(key.type(), k -> new java.util.HashMap<>());
        byName.put(key.name(), impl);
        return () -> {
            byName.remove(key.name(), impl);
            if (byName.isEmpty()) {
                store.remove(key.type(), byName);
            }
        };
    }

    /**
     * Load a plugin into this context and return the Disposable that unloads
     * it — the real runtime's `plugin(..)`, which the emitted lifecycle-test
     * driver drives as `load` / `unload` (docs/syntax-2.0.md §7.1). The stub
     * activates it directly: a plugin's `apply` IS its activation and the
     * Disposable it returns IS its teardown (G7 LIFO), so the stub keeps no
     * registry of its own — residue is read back through `get`, exactly as
     * backends/java/placement/RunOnce.java reads it.
     */
    public Disposable plugin(Plugin plugin) {
        return plugin.apply(this);
    }

    /** Effect scope: tracked disposables run in reverse order (LIFO, G7). */
    public EffectScope effect() {
        return new EffectScope();
    }

    /** Declare that `service` resolves in realm `realm` inside the returned
     *  view. A provider loaded through the returned context publishes into that
     *  realm; `serviceInRealm(service, realm)` then reads it strictly. */
    public Context isolate(Class<?> service, String realm) {
        java.util.Map<Class<?>, String> next = new java.util.HashMap<>(realmOf);
        next.put(service, realm);
        return new Context(shared, next);
    }

    public void intercept(ServiceKey<?> key, Object metadata) {
        // Stub: interception metadata is a runtime concern; shape only.
    }

    public static final class EffectScope implements Disposable {
        private final java.util.ArrayDeque<Disposable> tracked = new java.util.ArrayDeque<>();

        public void track(Disposable disposable) {
            tracked.push(disposable);
        }

        @Override
        public void dispose() {
            while (!tracked.isEmpty()) {
                tracked.pop().dispose();
            }
        }
    }
}
