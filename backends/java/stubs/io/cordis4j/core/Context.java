package io.cordis4j.core;

public class Context {
    // item 173: the registry is realm-aware. A shared store (one per context
    // tree, like the go runtime's `shared`) holds the root realm's providers
    // plus one map per NAMED realm. A context remembers, per service type, the
    // realm an enclosing `isolate` bound it to, so `provide`/`get` resolve into
    // that realm. `serviceInRealm` reads ONE realm strictly (the routing
    // primitive). A routes-less program never touches the realm maps, so its
    // resolution is exactly the old flat behavior.
    private static final class Shared {
        final java.util.Map<Class<?>, Object> root = new java.util.HashMap<>();
        final java.util.Map<String, java.util.Map<Class<?>, Object>> realms =
                new java.util.HashMap<>();

        java.util.Map<Class<?>, Object> realm(String name) {
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

    private java.util.Map<Class<?>, Object> storeFor(Class<?> type) {
        String realm = realmOf.get(type);
        return realm == null ? shared.root : shared.realm(realm);
    }

    /** Committed-view read: this context's realm for the key, else the root
     *  realm (the parent-chain fallback the router must NOT use). */
    public <T> T get(Class<T> type) {
        java.util.Map<Class<?>, Object> store = storeFor(type);
        Object value = store.get(type);
        if (value == null && store != shared.root) {
            value = shared.root.get(type); // fallback to the shared realm
        }
        if (value == null) {
            throw new CordisException("no provider for " + type.getName());
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
        java.util.Map<Class<?>, Object> store = shared.realms.get(realm);
        if (store == null) {
            return java.util.Optional.empty();
        }
        Object value = store.get(type);
        if (value == null) {
            return java.util.Optional.empty();
        }
        return java.util.Optional.of(type.cast(value));
    }

    public <T> Disposable provide(ServiceKey<T> key, T impl) {
        java.util.Map<Class<?>, Object> store = storeFor(key.type());
        store.put(key.type(), impl);
        return () -> store.remove(key.type(), impl);
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
