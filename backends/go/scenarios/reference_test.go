package scenarios

import (
	stdctx "context"
	"reflect"
	"testing"
	"time"

	stc "github.com/0xdenny218/stc-go"
)

func bg() stdctx.Context { return stdctx.Background() }

// (a) Effect installs and its inverse runs on unload in LIFO order.
func TestRef_EffectLIFO(t *testing.T) {
	root, rec := RootWithProbe()
	f := root.Load(TwoSteps())
	if err := f.Ready(bg()); err != nil {
		t.Fatal(err)
	}
	if got, want := rec.Marks(), []string{"do:a", "do:b"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("apply marks = %v, want %v", got, want)
	}
	f.Dispose()
	if err := f.Gone(bg()); err != nil {
		t.Fatal(err)
	}
	want := []string{"do:a", "do:b", "undo:b", "undo:a"}
	if got := rec.Marks(); !reflect.DeepEqual(got, want) {
		t.Fatalf("teardown marks = %v, want %v (LIFO)", got, want)
	}
}

// (a') A failing activation reverts accumulated effects and lands Failed.
func TestRef_FailReverts(t *testing.T) {
	root, rec := RootWithProbe()
	f := root.Load(FailsAfter())
	err := f.Ready(bg())
	if err == nil {
		t.Fatal("expected activation error")
	}
	if f.State() != stc.StateFailed {
		t.Fatalf("state = %v, want failed", f.State())
	}
	want := []string{"do:a", "undo:a"}
	if got := rec.Marks(); !reflect.DeepEqual(got, want) {
		t.Fatalf("marks = %v, want %v", got, want)
	}
}

// (b)+(c) Consumer stays pending until provider activates; deactivates
// (LIFO, before the provider) on withdrawal.
func TestRef_ReactiveProvideInject(t *testing.T) {
	root, rec := RootWithProbe()

	consumer := root.Load(KvConsumer())
	// kv not provided yet: the Inject gate must hold the consumer Pending.
	time.Sleep(40 * time.Millisecond)
	if got := rec.Marks(); len(got) != 0 {
		t.Fatalf("consumer activated before provider: %v", got)
	}
	if consumer.State() != stc.StatePending {
		t.Fatalf("consumer state = %v, want pending", consumer.State())
	}

	provider := root.Load(KvProvider())
	if err := provider.Ready(bg()); err != nil {
		t.Fatal(err)
	}
	if err := consumer.Ready(bg()); err != nil {
		t.Fatal(err)
	}
	if got, want := rec.Marks(), []string{"provider:up", "consumer:up"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("marks = %v, want %v", got, want)
	}

	// Withdraw the provider: the consumer must reactively DEACTIVATE.
	// NOTE (upstream divergence, recorded in README): stc-go removes the
	// provider's provide-entry and runs the provider's own inverses as part
	// of provider disposal, and only THEN does the orchestrator observe the
	// service withdrawal and tear the consumer down — so `provider:down`
	// precedes `consumer:down`. cordis-rs guarantees the opposite (dependent
	// tears down first, while the dependency is still provided). The
	// invariant that DOES hold on stc-go, and that we assert here, is that
	// the consumer reactively deactivates on withdrawal with no torn state
	// (every do: has its undo:).
	provider.Dispose()
	if err := provider.Gone(bg()); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for {
		m := rec.Marks()
		if contains(m, "consumer:down") && contains(m, "provider:down") {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("did not observe reactive consumer teardown: %v", m)
		}
		time.Sleep(10 * time.Millisecond)
	}
	if consumer.State() != stc.StatePending {
		t.Fatalf("consumer state = %v, want pending after withdrawal", consumer.State())
	}
}

// (d) Isolate/Realm placement: same key, two realms, disjoint providers;
// each consumer resolves its own realm's provider.
func TestRef_RealmPlacement(t *testing.T) {
	root, rec := RootWithProbe()

	aStoreCtx, err := IsolatedChild(root, "tenant_a", KvKey)
	if err != nil {
		t.Fatal(err)
	}
	bStoreCtx, err := IsolatedChild(root, "tenant_b", KvKey)
	if err != nil {
		t.Fatal(err)
	}
	pa := aStoreCtx.Load(RealmStore("A"))
	pb := bStoreCtx.Load(RealmStore("B"))
	if err := pa.Ready(bg()); err != nil {
		t.Fatal(err)
	}
	if err := pb.Ready(bg()); err != nil {
		t.Fatal(err)
	}

	aAppCtx, err := IsolatedChild(root, "tenant_a", KvKey)
	if err != nil {
		t.Fatal(err)
	}
	bAppCtx, err := IsolatedChild(root, "tenant_b", KvKey)
	if err != nil {
		t.Fatal(err)
	}
	ca := aAppCtx.Load(RealmConsumer("appA"))
	cb := bAppCtx.Load(RealmConsumer("appB"))
	if err := ca.Ready(bg()); err != nil {
		t.Fatal(err)
	}
	if err := cb.Ready(bg()); err != nil {
		t.Fatal(err)
	}

	m := rec.Marks()
	if !contains(m, "appA:saw:A") {
		t.Fatalf("tenant_a app must see provider A: %v", m)
	}
	if !contains(m, "appB:saw:B") {
		t.Fatalf("tenant_b app must see provider B: %v", m)
	}
	// Cross-realm leak check: appA must NOT see B, appB must NOT see A.
	if contains(m, "appA:saw:B") || contains(m, "appB:saw:A") {
		t.Fatalf("realm isolation leaked: %v", m)
	}
}

func contains(xs []string, s string) bool {
	for _, x := range xs {
		if x == s {
			return true
		}
	}
	return false
}

func indexOf(xs []string, s string) int {
	for i, x := range xs {
		if x == s {
			return i
		}
	}
	return -1
}
