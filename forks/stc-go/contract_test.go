package stc

// 对抗性评审（2026-08-14，84 agent 工作流）确认缺陷的回归测试。
// 每条测试对应一项经实证复现的发现或一个零覆盖的错误路径。

import (
	stdctx "context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// [critical] Ready/Gone 丢失唤醒：状态检查与订阅之间存在竞态窗口，
// 最后一次转移会使等待者永久挂起。修复为"先订阅后查状态"；
// 此测试以高频转移 + 带超时的等待者做压力验证。
func TestReadyNoLostWakeup(t *testing.T) {
	root := New()
	defer root.Close()

	key := NewKey[int]("dep")
	var undone atomic.Int64
	provider := Component{
		Name:    "provider",
		Provide: []Key{key},
		Apply: func(c *Context) (Inverse, error) {
			inv, err := c.Provide(key, 1)
			if err != nil {
				return nil, err
			}
			return func() error { undone.Add(1); return inv() }, nil
		},
	}
	consumer := Component{
		Name:   "consumer",
		Inject: []Key{key},
		Apply:  func(c *Context) (Inverse, error) { return nil, nil },
	}

	for i := range 400 {
		pf := root.Load(provider)
		cf := root.Load(consumer)
		done := make(chan error, 1)
		go func() {
			ctx, cancel := stdctx.WithTimeout(stdctx.Background(), 5*time.Second)
			defer cancel()
			done <- cf.Ready(ctx)
		}()
		pf.Dispose() // 撤销依赖 → consumer 转 Unloading
		if err := pf.Gone(stdctx.Background()); err != nil {
			t.Fatalf("iter %d: provider Gone: %v", i, err)
		}
		// 旧提供者完全撤退后再换血（同键提供者在旧条目移除前装载会被
		// ErrDuplicateProvide 拒绝——这是 fail-fast 语义，不是悬挂）。
		pf2 := root.Load(provider)
		if err := <-done; err != nil {
			t.Fatalf("iter %d: Ready hung or errored: %v (consumer state %v)", i, err, cf.State())
		}
		cf.Dispose()
		pf2.Dispose()
		if err := cf.Gone(stdctx.Background()); err != nil {
			t.Fatalf("iter %d: Gone: %v", i, err)
		}
		if err := pf2.Gone(stdctx.Background()); err != nil {
			t.Fatalf("iter %d: provider Gone: %v", i, err)
		}
	}
}

// [critical] WaitService 同样的丢失唤醒窗口。
func TestWaitServiceNoLostWakeup(t *testing.T) {
	root := New()
	defer root.Close()

	key := NewKey[int]("ws")
	for i := range 2000 {
		done := make(chan error, 1)
		go func() {
			ctx, cancel := stdctx.WithTimeout(stdctx.Background(), 5*time.Second)
			defer cancel()
			v, err := root.WaitService(ctx, key)
			if err == nil && v.(int) != i {
				err = fmt.Errorf("got %v want %d", v, i)
			}
			done <- err
		}()
		inv, err := root.Provide(key, i)
		if err != nil {
			t.Fatalf("iter %d: provide: %v", i, err)
		}
		if err := <-done; err != nil {
			t.Fatalf("iter %d: WaitService hung or errored: %v", i, err)
		}
		if err := inv(); err != nil {
			t.Fatalf("iter %d: unprovide: %v", i, err)
		}
	}
}

// [major] Fiber.Context() 与惯性重载的数据竞争：consumer 在 provider
// 换血期间被重载，orchestrator 重写 ctx；并发读取在 -race 下必须干净。
func TestFiberContextRaceFree(t *testing.T) {
	root := New()
	defer root.Close()

	key := NewKey[int]("cfg")
	provider := Component{
		Name:    "provider",
		Provide: []Key{key},
		Apply:   func(c *Context) (Inverse, error) { return c.Provide(key, 1) },
	}
	consumer := Component{
		Name:   "consumer",
		Inject: []Key{key},
		Apply:  func(c *Context) (Inverse, error) { return nil, nil },
	}
	pf := root.Load(provider)
	cf := root.Load(consumer)
	if err := cf.Ready(stdctx.Background()); err != nil {
		t.Fatal(err)
	}

	var wg sync.WaitGroup
	stop := make(chan struct{})
	for range 4 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
					_ = cf.Context() // 与重载的 ctx 重写并发
				}
			}
		}()
	}
	for range 50 {
		pf.Dispose()
		if err := pf.Gone(stdctx.Background()); err != nil {
			t.Fatal(err)
		}
		pf = root.Load(provider)
		if err := pf.Ready(stdctx.Background()); err != nil {
			t.Fatal(err)
		}
	}
	close(stop)
	wg.Wait()
	cf.Dispose()
	pf.Dispose()
}

// [major] Close 期间并发 Load 不得产生永久 Pending 的孤儿：
// 每个 fiber 必须到达终态（Gone 或 Failed），Ready 不得悬挂。
func TestLoadCloseRaceNoOrphans(t *testing.T) {
	for round := range 30 {
		root := New()
		key := NewKey[int](fmt.Sprintf("k%d", round))
		comp := Component{
			Name:    "c",
			Provide: []Key{key},
			Apply:   func(c *Context) (Inverse, error) { return c.Provide(key, 1) },
		}

		const n = 40
		fibers := make([]*Fiber, n)
		var wg sync.WaitGroup
		for i := range n {
			wg.Add(1)
			go func(i int) {
				defer wg.Done()
				fibers[i] = root.Load(comp)
			}(i)
		}
		wg.Add(1)
		go func() { defer wg.Done(); _ = root.Close() }()
		wg.Wait()

		for i, f := range fibers {
			ctx, cancel := stdctx.WithTimeout(stdctx.Background(), 5*time.Second)
			err := f.Ready(ctx)
			cancel()
			switch f.State() {
			case StateGone:
				if !errors.Is(err, ErrDisposed) {
					t.Fatalf("round %d fiber %d: Gone but Ready=%v", round, i, err)
				}
			case StateFailed:
				if err == nil {
					t.Fatalf("round %d fiber %d: Failed but Ready=nil", round, i)
				}
			default:
				t.Fatalf("round %d fiber %d: orphan in state %v", round, i, f.State())
			}
		}
	}
}

// [major] 子 context 的 Close 不得关停全局 orchestrator。
func TestChildCloseRejected(t *testing.T) {
	root := New()
	defer root.Close()

	child := root.Child()
	if err := child.Close(); !errors.Is(err, ErrNotRoot) {
		t.Fatalf("child Close = %v, want ErrNotRoot", err)
	}

	// 系统仍然存活：fiber 照常装载。
	key := NewKey[int]("alive")
	f := root.Load(Component{
		Name:    "c",
		Provide: []Key{key},
		Apply:   func(c *Context) (Inverse, error) { return c.Provide(key, 7) },
	})
	if err := f.Ready(stdctx.Background()); err != nil {
		t.Fatalf("system dead after child Close: %v", err)
	}
}

// 非根作用域的子树清理用 Release，不影响其余系统。
func TestReleaseSubtreeOnly(t *testing.T) {
	root := New()
	defer root.Close()

	key := NewKey[int]("scoped")
	child := root.Child()
	var undone atomic.Int64
	if err := child.Effect(func() Inverse {
		return func() error { undone.Add(1); return nil }
	}); err != nil {
		t.Fatal(err)
	}
	f := child.Load(Component{
		Name:    "c",
		Provide: []Key{key},
		Apply:   func(c *Context) (Inverse, error) { return c.Provide(key, 1) },
	})
	if err := f.Ready(stdctx.Background()); err != nil {
		t.Fatal(err)
	}

	if err := child.Release(); err != nil {
		t.Fatal(err)
	}
	if undone.Load() != 1 {
		t.Fatalf("child effect not unwound: %d", undone.Load())
	}
	// fiber 的 context 在装载时已 detach（D7：fiber 生命周期独占管理），
	// 不随子树回卷——fiber 仍 Active，系统照常工作。
	if st := f.State(); st != StateActive {
		t.Fatalf("fiber state = %v after Release, want Active (detached per D7)", st)
	}
	f.Dispose()
	if err := f.Gone(stdctx.Background()); err != nil {
		t.Fatal(err)
	}
}

// Load 到已回卷的 context：立即 Failed，不进入注册表。
func TestLoadAfterCloseFailed(t *testing.T) {
	root := New()
	if err := root.Close(); err != nil {
		t.Fatal(err)
	}
	f := root.Load(Component{Name: "late", Apply: func(c *Context) (Inverse, error) { return nil, nil }})
	if f.State() != StateFailed {
		t.Fatalf("state = %v, want Failed", f.State())
	}
	if !errors.Is(f.Err(), ErrInactive) && !errors.Is(f.Err(), ErrDisposed) {
		t.Fatalf("err = %v, want ErrInactive or ErrDisposed", f.Err())
	}
	// Ready 对已终结 fiber 立即返回，不悬挂。
	if err := f.Ready(stdctx.Background()); err == nil {
		t.Fatal("Ready on Failed fiber returned nil")
	}
}

// Ready 在 fiber 已撤退后返回 ErrDisposed。
func TestReadyAfterGone(t *testing.T) {
	root := New()
	defer root.Close()
	f := root.Load(Component{Name: "c", Apply: func(c *Context) (Inverse, error) { return nil, nil }})
	if err := f.Ready(stdctx.Background()); err != nil {
		t.Fatal(err)
	}
	f.Dispose()
	if err := f.Gone(stdctx.Background()); err != nil {
		t.Fatal(err)
	}
	if err := f.Ready(stdctx.Background()); !errors.Is(err, ErrDisposed) {
		t.Fatalf("Ready after Gone = %v, want ErrDisposed", err)
	}
}

// Ready/Gone 尊重 ctx 取消。
func TestReadyContextCancel(t *testing.T) {
	root := New()
	defer root.Close()
	key := NewKey[int]("missing")
	// 依赖永不满足 → fiber 永远 Pending。
	f := root.Load(Component{Name: "c", Inject: []Key{key}, Apply: func(c *Context) (Inverse, error) { return nil, nil }})

	ctx, cancel := stdctx.WithCancel(stdctx.Background())
	cancel()
	if err := f.Ready(ctx); !errors.Is(err, stdctx.Canceled) {
		t.Fatalf("Ready = %v, want context.Canceled", err)
	}
	if err := f.Gone(ctx); !errors.Is(err, stdctx.Canceled) {
		t.Fatalf("Gone = %v, want context.Canceled", err)
	}
}

// 错误路径零覆盖项：Effect(nil)、Set 类型不匹配、空 Apply。
func TestErrorPaths(t *testing.T) {
	root := New()
	defer root.Close()

	if err := root.Effect(nil); !errors.Is(err, ErrNilInstall) {
		t.Fatalf("Effect(nil) = %v, want ErrNilInstall", err)
	}

	key := NewKey[int]("typed")
	if err := root.Set(key, "not an int"); err == nil {
		t.Fatal("Set with wrong type succeeded")
	}
	if err := root.Set(key, 42); err != nil {
		t.Fatalf("Set with right type: %v", err)
	}

	f := root.Load(Component{Name: "noapply"})
	if err := f.Ready(stdctx.Background()); err == nil {
		t.Fatal("component without Apply loaded successfully")
	}
	if f.State() != StateFailed {
		t.Fatalf("state = %v, want Failed", f.State())
	}
}

// TraceApplied/TraceUnwound 必须真实发射（原为死枚举）。
func TestTraceAppliedUnwoundEmitted(t *testing.T) {
	root := New()
	defer root.Close()

	f := root.Load(Component{Name: "c", Apply: func(c *Context) (Inverse, error) { return nil, nil }})
	if err := f.Ready(stdctx.Background()); err != nil {
		t.Fatal(err)
	}
	f.Dispose()
	if err := f.Gone(stdctx.Background()); err != nil {
		t.Fatal(err)
	}

	var sawApplied, sawUnwound bool
	for _, ev := range root.Trace() {
		if ev.Fiber != f.ID() {
			continue
		}
		switch ev.Kind {
		case TraceApplied:
			sawApplied = true
		case TraceUnwound:
			sawUnwound = true
		}
	}
	if !sawApplied || !sawUnwound {
		t.Fatalf("trace missing events: applied=%v unwound=%v", sawApplied, sawUnwound)
	}
}

// On/Emit 并发安全，且回卷后监听器不再触发。
func TestOnEmitConcurrent(t *testing.T) {
	root := New()
	defer root.Close()

	var calls atomic.Int64
	const listeners = 20
	for range listeners {
		if err := root.On("ev", func(args ...any) { calls.Add(1) }); err != nil {
			t.Fatal(err)
		}
	}
	var wg sync.WaitGroup
	for range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for range 50 {
				root.Emit("ev", 1)
			}
		}()
	}
	wg.Wait()
	if calls.Load() != 8*50*listeners {
		t.Fatalf("calls = %d, want %d", calls.Load(), 8*50*listeners)
	}

	if err := root.Release(); err != nil {
		t.Fatal(err)
	}
	before := calls.Load()
	root.Emit("ev", 1)
	if calls.Load() != before {
		t.Fatal("listener fired after Release")
	}
}

// WaitService 与 fiber 提供者的交互：fiber 装载唤醒等待者；
// 撤退后新的等待者不再被旧值满足。
func TestWaitServiceFiberProvider(t *testing.T) {
	root := New()
	defer root.Close()

	key := NewKey[int]("svc")
	comp := Component{
		Name:    "svc",
		Provide: []Key{key},
		Apply:   func(c *Context) (Inverse, error) { return c.Provide(key, 99) },
	}

	done := make(chan any, 1)
	go func() {
		v, err := root.WaitService(stdctx.Background(), key)
		if err != nil {
			t.Errorf("WaitService: %v", err)
			return
		}
		done <- v
	}()
	f := root.Load(comp)
	select {
	case v := <-done:
		if v.(int) != 99 {
			t.Fatalf("got %v", v)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("WaitService not woken by fiber load")
	}

	f.Dispose()
	if err := f.Gone(stdctx.Background()); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := stdctx.WithTimeout(stdctx.Background(), 100*time.Millisecond)
	defer cancel()
	if _, err := root.WaitService(ctx, key); !errors.Is(err, stdctx.DeadlineExceeded) {
		t.Fatalf("WaitService after dispose = %v, want timeout", err)
	}
}

// 子树逆错误向根 Close 冒泡（首个错误契约跨越层级）。
func TestSubtreeErrorBubbles(t *testing.T) {
	root := New()
	child := root.Child()
	sentinel := errors.New("boom")
	if err := child.Effect(func() Inverse { return func() error { return sentinel } }); err != nil {
		t.Fatal(err)
	}
	if err := root.Close(); !errors.Is(err, sentinel) {
		t.Fatalf("Close = %v, want sentinel", err)
	}
}

// Def.58 的直接断言：第二个 fiber 提供者被 ErrDuplicateProvide 拒绝，
// 首个提供者不受影响。
func TestDuplicateProvideFailsFast(t *testing.T) {
	root := New()
	defer root.Close()

	key := NewKey[int]("dup")
	comp := Component{
		Name:    "p",
		Provide: []Key{key},
		Apply:   func(c *Context) (Inverse, error) { return c.Provide(key, 1) },
	}
	p1 := root.Load(comp)
	if err := p1.Ready(stdctx.Background()); err != nil {
		t.Fatal(err)
	}
	p2 := root.Load(comp)
	err := p2.Ready(stdctx.Background())
	if !errors.Is(err, ErrDuplicateProvide) {
		t.Fatalf("second provider Ready = %v, want ErrDuplicateProvide", err)
	}
	if p2.State() != StateFailed {
		t.Fatalf("second provider state = %v, want Failed", p2.State())
	}
	// Gone 对 Failed fiber 同样返回（出册终态），不得悬挂。
	if err := p2.Gone(stdctx.Background()); err != nil {
		t.Fatal(err)
	}
	// 首个提供者仍在服役。
	v, err := Service[int](root, key)
	if err != nil || v != 1 {
		t.Fatalf("first provider disturbed: v=%v err=%v", v, err)
	}
	if p1.State() != StateActive {
		t.Fatalf("first provider state = %v, want Active", p1.State())
	}
}

// 已回卷 context 的全部拒绝路径。
func TestClosedContextRejected(t *testing.T) {
	root := New()
	defer root.Close()

	child := root.Child()
	if err := child.Release(); err != nil {
		t.Fatal(err)
	}
	key := NewKey[int]("closed")
	if err := child.Set(key, 1); !errors.Is(err, ErrInactive) {
		t.Errorf("Set = %v, want ErrInactive", err)
	}
	if _, err := child.Provide(key, 1); !errors.Is(err, ErrInactive) {
		t.Errorf("Provide = %v, want ErrInactive", err)
	}
	if err := child.Effect(func() Inverse { return nil }); !errors.Is(err, ErrInactive) {
		t.Errorf("Effect = %v, want ErrInactive", err)
	}
	if err := child.Isolate(key, NewRealm(RootRealm(), "r")); !errors.Is(err, ErrInactive) {
		t.Errorf("Isolate = %v, want ErrInactive", err)
	}
	if err := child.Intercept(key, "m"); !errors.Is(err, ErrInactive) {
		t.Errorf("Intercept = %v, want ErrInactive", err)
	}
	if err := child.On("ev", func(...any) {}); !errors.Is(err, ErrInactive) {
		t.Errorf("On = %v, want ErrInactive", err)
	}
	// Child 返回预关闭的 context（注册立即失败，而非 panic 或悬挂）。
	grand := child.Child()
	if err := grand.Set(key, 1); !errors.Is(err, ErrInactive) {
		t.Errorf("grandchild Set = %v, want ErrInactive", err)
	}
}

// 拦截器随 context 回卷撤销。
func TestInterceptorsClearedAfterRelease(t *testing.T) {
	root := New()
	defer root.Close()

	key := NewKey[int]("ic")
	child := root.Child()
	if err := child.Intercept(key, "m1"); err != nil {
		t.Fatal(err)
	}
	if got := Interceptors(child, key); len(got) != 1 {
		t.Fatalf("interceptors = %v, want 1 entry", got)
	}
	if err := child.Release(); err != nil {
		t.Fatal(err)
	}
	if got := Interceptors(child, key); len(got) != 0 {
		t.Fatalf("interceptors after Release = %v, want empty", got)
	}
}

// resolveExternal 自跳过：同时提供并注入同键的 fiber 不得自我满足，
// 必须等待外部提供者；外部提供者撤销后它随之卸载。
func TestSelfProvideDoesNotSatisfyInject(t *testing.T) {
	root := New()
	defer root.Close()

	key := NewKey[int]("self")
	selfish := Component{
		Name:    "selfish",
		Inject:  []Key{key},
		Provide: []Key{key},
		Apply:   func(c *Context) (Inverse, error) { return c.Provide(key, 2) },
	}
	f := root.Load(selfish)

	// 自我提供不算数：fiber 必须停在 Pending。
	ctx, cancel := stdctx.WithTimeout(stdctx.Background(), 200*time.Millisecond)
	if err := f.Ready(ctx); !errors.Is(err, stdctx.DeadlineExceeded) {
		cancel()
		t.Fatalf("self-satisfying fiber left Pending: Ready=%v state=%v", err, f.State())
	}
	cancel()

	// 外部提供者出现后装载。
	inv, err := root.Provide(key, 1)
	if err != nil {
		t.Fatal(err)
	}
	if err := f.Ready(stdctx.Background()); err != nil {
		t.Fatal(err)
	}

	// 外部撤销 → fiber 卸载回 Pending（它自己的提供救不了它）。
	if err := inv(); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if f.State() == StatePending {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("fiber did not return to Pending after external loss, state=%v", f.State())
}
