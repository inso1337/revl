package stc

import (
	stdctx "context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"
)

func TestEffectDisposeOrdering(t *testing.T) {
	root := New()
	var mu sync.Mutex
	var order []string

	// 嵌套注册三个效应；逆内含不同时长的阻塞（异步逆），
	// 只有严格串行 LIFO 才能得到确定的顺序。
	mustOK(t, root.Effect(func() Inverse {
		return func() error {
			time.Sleep(30 * time.Millisecond)
			mu.Lock()
			order = append(order, "undo-1")
			mu.Unlock()
			return nil
		}
	}))
	mustOK(t, root.Effect(func() Inverse {
		return func() error {
			mu.Lock()
			order = append(order, "undo-2")
			mu.Unlock()
			return nil
		}
	}))
	mustOK(t, root.Child().Effect(func() Inverse {
		return func() error {
			time.Sleep(10 * time.Millisecond)
			mu.Lock()
			order = append(order, "undo-3")
			mu.Unlock()
			return nil
		}
	}))

	if err := root.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	want := []string{"undo-3", "undo-2", "undo-1"}
	if fmt.Sprint(order) != fmt.Sprint(want) {
		t.Fatalf("dispose order = %v, want %v", order, want)
	}
}

func TestInverseErrorSwallowed(t *testing.T) {
	root := New()
	var mu sync.Mutex
	var ran []int

	mustOK(t, root.Effect(func() Inverse {
		return func() error { mu.Lock(); ran = append(ran, 1); mu.Unlock(); return nil }
	}))
	mustOK(t, root.Effect(func() Inverse {
		return func() error { mu.Lock(); ran = append(ran, 2); mu.Unlock(); return errors.New("boom") }
	}))
	mustOK(t, root.Effect(func() Inverse {
		return func() error { mu.Lock(); ran = append(ran, 3); mu.Unlock(); return errors.New("second boom") }
	}))

	err := root.Close()
	// LIFO 执行序：inverse-3 最先执行，其错误即首个被报告的错误。
	if err == nil || err.Error() != "second boom" {
		t.Fatalf("Close err = %v, want first-executed error %q", err, "second boom")
	}
	// 抛错的逆不阻断其余逆：3 → 2 → 1 全部执行。
	if fmt.Sprint(ran) != "[3 2 1]" {
		t.Fatalf("ran = %v, want [3 2 1]", ran)
	}
}

func TestEffectAfterUnwindSelfUndoes(t *testing.T) {
	root := New()
	mustOK(t, root.Close())

	var sideEffect int
	err := root.Effect(func() Inverse {
		sideEffect = 1
		return func() error { sideEffect = 0; return nil }
	})
	if !errors.Is(err, ErrInactive) {
		t.Fatalf("err = %v, want ErrInactive", err)
	}
	if sideEffect != 0 {
		t.Fatalf("side effect not self-undone: %d", sideEffect)
	}
}

func TestProvideServiceTyped(t *testing.T) {
	type DB struct{ DSN string }
	root := New()
	k := NewKey[DB]("db")

	inv, err := root.Provide(k, DB{DSN: "a"})
	if err != nil {
		t.Fatal(err)
	}
	got, err := Service[DB](root, k)
	if err != nil || got.DSN != "a" {
		t.Fatalf("Service = %v, %v", got, err)
	}

	// 类型不匹配的提供被拒绝。
	if _, err := root.Provide(k, "not a db"); err == nil {
		t.Fatal("want type mismatch error")
	}
	// 类型不匹配的读取报错。
	other := NewKey[string]("other")
	if _, err2 := root.Provide(other, "s"); err2 != nil {
		t.Fatal(err2)
	}
	if _, err := Service[int](root, other); err == nil {
		t.Fatal("want type mismatch on read")
	}

	if err := inv(); err != nil {
		t.Fatal(err)
	}
	if _, err := Service[DB](root, k); err == nil {
		t.Fatal("service should be gone after inverse")
	}
	// 幂等：再次调用不 panic、不影响其他条目。
	if err := inv(); err != nil {
		t.Fatal(err)
	}
}

func TestProvideLastWinsAndMiddleRemoval(t *testing.T) {
	root := New()
	k := UntypedKey("k")

	invA, _ := root.Provide(k, "a")
	invB, _ := root.Provide(k, "b")

	if v, _ := root.resolve(k); v != "b" {
		t.Fatalf("visible = %v, want b (last provided wins)", v)
	}
	// 移除非顶部的 A：可观察值不变（撤销精确性的体现）。
	if err := invA(); err != nil {
		t.Fatal(err)
	}
	if v, _ := root.resolve(k); v != "b" {
		t.Fatalf("visible = %v, want b after removing non-top entry", v)
	}
	// 移除顶部 B：回落到无。
	if err := invB(); err != nil {
		t.Fatal(err)
	}
	if _, ok := root.resolve(k); ok {
		t.Fatal("want no provider left")
	}
}

func TestSetGetShadowing(t *testing.T) {
	root := New()
	k := UntypedKey("v")
	mustOK(t, root.Set(k, "root"))
	child := root.Child()
	mustOK(t, child.Set(k, "child"))

	if v, _ := root.Get(k); v != "root" {
		t.Fatalf("root sees %v", v)
	}
	if v, _ := child.Get(k); v != "child" {
		t.Fatalf("child sees %v", v)
	}
	if _, ok := root.Get(UntypedKey("missing")); ok {
		t.Fatal("missing key should be absent")
	}
}

func TestOnEmitDisposal(t *testing.T) {
	root := New()
	calls := 0
	mustOK(t, root.On("evt", func(args ...any) { calls++ }))

	root.Emit("evt")
	if calls != 1 {
		t.Fatalf("calls = %d", calls)
	}
	mustOK(t, root.Close())
	root.Emit("evt") // 监听器已随回卷撤销
	if calls != 1 {
		t.Fatalf("calls after close = %d, want 1", calls)
	}
}

func TestWaitService(t *testing.T) {
	root := New()
	k := UntypedKey("late")
	go func() {
		time.Sleep(20 * time.Millisecond)
		if _, err := root.Provide(k, 42); err != nil {
			t.Error(err)
		}
	}()
	ctx, cancel := stdctx.WithTimeout(stdctx.Background(), time.Second)
	defer cancel()
	v, err := root.WaitService(ctx, k)
	if err != nil || v != 42 {
		t.Fatalf("WaitService = %v, %v", v, err)
	}

	ctx2, cancel2 := stdctx.WithTimeout(stdctx.Background(), 30*time.Millisecond)
	defer cancel2()
	if _, err := root.WaitService(ctx2, UntypedKey("never")); err == nil {
		t.Fatal("want timeout error")
	}
}

func TestConcurrentEffectsAndProvides(t *testing.T) {
	root := New()
	const goroutines = 8
	const perG = 40

	var wg sync.WaitGroup
	counters := make([]*counter, goroutines)
	for g := range goroutines {
		counters[g] = &counter{}
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			child := root.Child()
			for i := range perG {
				cn := counters[g]
				mustOK(t, child.Effect(func() Inverse {
					cn.add(1)
					return func() error { cn.add(-1); return nil }
				}))
				if _, err := child.Provide(UntypedKey(fmt.Sprintf("g%d-i%d", g, i)), i); err != nil {
					t.Error(err)
				}
			}
		}(g)
	}
	wg.Wait()

	for g, cn := range counters {
		if got := cn.get(); got != perG {
			t.Fatalf("counter[%d] = %d, want %d", g, got, perG)
		}
	}
	mustT(t, root.Close())
	for g, cn := range counters {
		if got := cn.get(); got != 0 {
			t.Fatalf("counter[%d] after close = %d, want 0", g, got)
		}
	}
}

// ------------------------------------------------------------------

type counter struct {
	mu sync.Mutex
	n  int
}

func (c *counter) add(d int) { c.mu.Lock(); c.n += d; c.mu.Unlock() }
func (c *counter) get() int  { c.mu.Lock(); defer c.mu.Unlock(); return c.n }

func mustOK(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func mustT(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}
