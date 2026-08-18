package scenarios

import (
	"fmt"
	"sync"

	stc "github.com/0xdenny218/stc-go"
)

// This file is the HAND-WRITTEN reference: the exact Go that revl's simplest
// components SHOULD lower to against real stc-go. It is the emitter's target
// and its executable oracle (reference_test.go drives it).

// ---------------------------------------------------------------------------
// (a) Effect + inverse in LIFO order. revl:
//
//	component TwoSteps {
//	  effect probe.mark("do:a") undo probe.mark("undo:a")
//	  effect probe.mark("do:b") undo probe.mark("undo:b")
//	}
func TwoSteps() stc.Component {
	return stc.Component{
		Name: "TwoSteps",
		Apply: func(ctx *stc.Context) (stc.Inverse, error) {
			p := probe(ctx)
			if err := ctx.Effect(func() stc.Inverse {
				p.Mark("do:a")
				return func() error { p.Mark("undo:a"); return nil }
			}); err != nil {
				return nil, err
			}
			if err := ctx.Effect(func() stc.Inverse {
				p.Mark("do:b")
				return func() error { p.Mark("undo:b"); return nil }
			}); err != nil {
				return nil, err
			}
			return nil, nil
		},
	}
}

// ---------------------------------------------------------------------------
// (a') A failing activation reverts accumulated effects then lands Failed.
func FailsAfter() stc.Component {
	return stc.Component{
		Name: "FailsAfter",
		Apply: func(ctx *stc.Context) (stc.Inverse, error) {
			p := probe(ctx)
			if err := ctx.Effect(func() stc.Inverse {
				p.Mark("do:a")
				return func() error { p.Mark("undo:a"); return nil }
			}); err != nil {
				return nil, err
			}
			return nil, fmt.Errorf("activation failed after do:a")
		},
	}
}

// ---------------------------------------------------------------------------
// (b)+(c) Provide/inject resolves a keyed service, and a reactive consumer
// stays pending until the provider activates and deactivates on withdrawal.
//
//	service Kv { fn get(k: Str) -> Opt[Str]; fn set(k, v) }
//	component KvProvider provides kv: Kv { ... }
//	component KvConsumer requires kv: Kv { ... }

// Kv is the emitted service interface.
type Kv interface {
	Get(k string) (string, bool)
	Set(k, v string)
}

// KvKey is the service key.
var KvKey = stc.NewKey[Kv]("kv")

// kvStore is the provide-block impl backed by a host Map effect.
type kvStore struct{ m map[string]string }

func (s *kvStore) Get(k string) (string, bool) { v, ok := s.m[k]; return v, ok }
func (s *kvStore) Set(k, v string)             { s.m[k] = v }

func KvProvider() stc.Component {
	return stc.Component{
		Name:    "KvProvider",
		Provide: []stc.Key{KvKey},
		Apply: func(ctx *stc.Context) (stc.Inverse, error) {
			p := probe(ctx)
			// let store = effect Map.new() undo store.drop()
			store := &kvStore{}
			if err := ctx.Effect(func() stc.Inverse {
				store.m = map[string]string{}
				p.Mark("provider:up")
				return func() error { store.m = nil; p.Mark("provider:down"); return nil }
			}); err != nil {
				return nil, err
			}
			// provide kv { ... }
			if _, err := ctx.Provide(KvKey, Kv(store)); err != nil {
				return nil, err
			}
			return nil, nil
		},
	}
}

func KvConsumer() stc.Component {
	return stc.Component{
		Name:   "KvConsumer",
		Inject: []stc.Key{KvKey},
		Apply: func(ctx *stc.Context) (stc.Inverse, error) {
			p := probe(ctx)
			// requires kv: Kv  -> resolve the coeffect
			kv, err := stc.Service[Kv](ctx, KvKey)
			if err != nil {
				return nil, err
			}
			if err := ctx.Effect(func() stc.Inverse {
				kv.Set("who", "alice")
				p.Mark("consumer:up")
				return func() error { p.Mark("consumer:down"); return nil }
			}); err != nil {
				return nil, err
			}
			return nil, nil
		},
	}
}

// ---------------------------------------------------------------------------
// (d) Isolate/Realm placement: two providers of the SAME key in disjoint
// realms; a consumer isolated into one realm resolves that realm's provider.
//
//	component TenantStore provides kv: Kv { isolate kv in realm("tenant_a") ... }
//	component TenantApp   requires kv: Kv { isolate kv in realm("tenant_a") ... }
//
// Realm placement is done by the LOADER, not by the component body: the
// `isolate kv in realm("R")` declaration lowers to isolating the load-target
// context, so the fiber's provide/inject resolve in realm R. Provider and
// consumer of the same key+realm MUST share the same *stc.Realm pointer
// (provKey is keyed by pointer, not by name), so realms are interned by name.

var (
	realmMu    sync.Mutex
	realmByTag = map[string]*stc.Realm{}
)

// RevlRealm interns a realm by name under the root realm. Every load site that
// names the same realm gets the same *Realm, so their (key, realm) provisions
// coincide. This is the Go analog of the rust backend's `_revl_realm` helper.
func RevlRealm(name string) *stc.Realm {
	realmMu.Lock()
	defer realmMu.Unlock()
	if r, ok := realmByTag[name]; ok {
		return r
	}
	r := stc.NewRealm(stc.RootRealm(), name)
	realmByTag[name] = r
	return r
}

// IsolatedChild derives a child context that isolates each key into the named
// realm, then returns it as the load target. This is the emitter's placement
// seam: `_revl_isolate_ctx(parent, realm, keys...)`.
func IsolatedChild(parent *stc.Context, realmName string, keys ...stc.Key) (*stc.Context, error) {
	child := parent.Child()
	r := RevlRealm(realmName)
	for _, k := range keys {
		if err := child.Isolate(k, r); err != nil {
			return nil, err
		}
	}
	return child, nil
}

// RealmStore provides kv, marking with a tag. It is loaded into an isolated
// child by the harness; the body itself is realm-agnostic.
func RealmStore(tag string) stc.Component {
	return stc.Component{
		Name:    "RealmStore_" + tag,
		Provide: []stc.Key{KvKey},
		Apply: func(ctx *stc.Context) (stc.Inverse, error) {
			p := probe(ctx)
			store := &kvStore{m: map[string]string{}}
			store.Set("tag", tag)
			if _, err := ctx.Provide(KvKey, Kv(store)); err != nil {
				return nil, err
			}
			p.Mark("store:" + tag)
			return nil, nil
		},
	}
}

// RealmConsumer records which provider's tag it saw. Loaded into an isolated
// child by the harness.
func RealmConsumer(label string) stc.Component {
	return stc.Component{
		Name:   "RealmConsumer_" + label,
		Inject: []stc.Key{KvKey},
		Apply: func(ctx *stc.Context) (stc.Inverse, error) {
			p := probe(ctx)
			kv, err := stc.Service[Kv](ctx, KvKey)
			if err != nil {
				return nil, err
			}
			tag, _ := kv.Get("tag")
			p.Mark(label + ":saw:" + tag)
			return nil, nil
		},
	}
}
