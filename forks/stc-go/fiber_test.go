package stc

import (
	stdctx "context"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// scenario bank: cordis@8cc9e33 fiber.spec「inertia lock 1/2/3」
// 原测试用假时钟控制异步进度；此处用门控信道等效地冻结 apply。

func waitState(t *testing.T, f *Fiber, want FiberState, within time.Duration) FiberState {
	t.Helper()
	deadline := time.Now().Add(within)
	for time.Now().Before(deadline) {
		if f.State() == want {
			return want
		}
		time.Sleep(2 * time.Millisecond)
	}
	return f.State()
}

// 惯性锁 1：装载期间依赖消失又恢复——装载完成后先卸载再重载，最终 Active。
func TestInertiaLock1(t *testing.T) {
	root := New()
	defer root.Close()
	k := UntypedKey("foo")

	inv, err := root.Provide(k, 1)
	if err != nil {
		t.Fatal(err)
	}

	loaded := make(chan struct{}) // apply 到达中点
	release := make(chan struct{})
	unloadingGate := make(chan struct{})
	var once sync.Once // fiber 会重载，apply 必须可重入
	f := root.Load(Component{
		Name:   "consumer",
		Inject: []Key{k},
		Apply: func(ctx *Context) (Inverse, error) {
			once.Do(func() { close(loaded) })
			<-release // 冻结装载（等效 sleep(1000)）；重载时立即通过
			// 逆操作同样受控，让 Unloading 态可观察（等效 sleep(1000) 的卸载）。
			return func() error {
				<-unloadingGate
				return nil
			}, nil
		},
	})

	<-loaded
	if s := f.State(); s != StateLoading {
		t.Fatalf("state = %v, want loading", s)
	}
	// 依赖消失。
	if err := inv(); err != nil {
		t.Fatal(err)
	}
	time.Sleep(20 * time.Millisecond)
	if s := f.State(); s != StateLoading {
		t.Fatalf("after dep gone, state = %v, want loading (inertia: 不中断在飞装载)", s)
	}
	close(release) // 装载完成 → 依赖缺失 → 卸载（在逆的门控处驻留）
	if s := waitState(t, f, StateUnloading, time.Second); s != StateUnloading {
		t.Fatalf("after load completes without dep, state = %v, want unloading", s)
	}
	// 卸载期间依赖恢复 → 放行卸载后立即重载。
	if _, err := root.Provide(k, 1); err != nil {
		t.Fatal(err)
	}
	close(unloadingGate)
	if s := waitState(t, f, StateActive, 2*time.Second); s != StateActive {
		t.Fatalf("final state = %v, want active", s)
	}
}

// 惯性锁 2：装载期间依赖消失、装载完成前恢复——不打断，直接 Active。
func TestInertiaLock2(t *testing.T) {
	root := New()
	defer root.Close()
	k := UntypedKey("foo")

	inv, _ := root.Provide(k, 1)
	loaded := make(chan struct{})
	release := make(chan struct{})
	var once sync.Once
	f := root.Load(Component{
		Name:   "consumer",
		Inject: []Key{k},
		Apply: func(ctx *Context) (Inverse, error) {
			once.Do(func() { close(loaded) })
			<-release
			return nil, nil
		},
	})

	<-loaded
	if err := inv(); err != nil { // 依赖消失
		t.Fatal(err)
	}
	time.Sleep(10 * time.Millisecond)
	if _, err := root.Provide(k, 2); err != nil { // 依赖恢复（装载完成前）
		t.Fatal(err)
	}
	close(release)
	if s := waitState(t, f, StateActive, time.Second); s != StateActive {
		t.Fatalf("state = %v, want active（依赖在装载完成前恢复，无需重载）", s)
	}
}

// 惯性锁 3：依赖永久消失——卸载后 fiber 停在 Pending（蛰伏等待）。
func TestInertiaLock3(t *testing.T) {
	root := New()
	defer root.Close()
	k := UntypedKey("foo")

	inv, _ := root.Provide(k, 1)
	f := root.Load(Component{
		Name:   "consumer",
		Inject: []Key{k},
		Apply: func(ctx *Context) (Inverse, error) {
			return nil, nil
		},
	})
	if err := f.Ready(stdctx.Background()); err != nil {
		t.Fatal(err)
	}
	if err := inv(); err != nil { // 依赖永久消失
		t.Fatal(err)
	}
	if s := waitState(t, f, StatePending, time.Second); s != StatePending {
		t.Fatalf("state = %v, want pending（蛰伏）", s)
	}
	// 蛰伏的 fiber 不再持有副作用。
	if v, ok := root.resolve(k); ok {
		t.Fatalf("unexpected provider visible: %v", v)
	}
}

// scenario bank: cordis@8cc9e33 fiber.spec「plugin error」
// apply 出错 → Failed；装载期间已注册的效应被回卷。
func TestApplyErrorCleansUp(t *testing.T) {
	root := New()
	defer root.Close()
	k := UntypedKey("side")

	calls := atomic.Int64{}
	f := root.Load(Component{
		Name: "bad",
		Apply: func(ctx *Context) (Inverse, error) {
			if err := ctx.Effect(func() Inverse {
				calls.Add(1)
				return func() error { calls.Add(-1); return nil }
			}); err != nil {
				return nil, err
			}
			if _, err := ctx.Provide(k, "partial"); err != nil {
				return nil, err
			}
			return nil, fmt.Errorf("apply failed")
		},
	})
	err := f.Ready(stdctx.Background())
	if err == nil || err.Error() != "apply failed" {
		t.Fatalf("Ready err = %v, want apply failed", err)
	}
	if s := f.State(); s != StateFailed {
		t.Fatalf("state = %v, want failed", s)
	}
	if got := calls.Load(); got != 0 {
		t.Fatalf("effects not unwound: %d", got)
	}
	if _, ok := root.resolve(k); ok {
		t.Fatal("partial provide should be withdrawn")
	}
}

// 显式 Dispose 打在装载中的 fiber 上：装载完成后撤退。
func TestDisposeDuringLoading(t *testing.T) {
	root := New()
	defer root.Close()
	k := UntypedKey("x")

	release := make(chan struct{})
	applied := atomic.Bool{}
	f := root.Load(Component{
		Name: "slow",
		Apply: func(ctx *Context) (Inverse, error) {
			<-release
			applied.Store(true)
			_, err := ctx.Provide(k, "v")
			return nil, err
		},
	})
	f.Dispose() // 装载仍在进行
	close(release)

	g := stdctx.Background()
	if err := f.Gone(g); err != nil {
		t.Fatal(err)
	}
	if !applied.Load() {
		t.Fatal("apply should have completed before disposal")
	}
	if _, ok := root.resolve(k); ok {
		t.Fatal("provide must be withdrawn by disposal")
	}
}

// scenario bank: cordis@8cc9e33 fiber.spec「update config while injected
// service reloads」——provider 重载与 consumer 自身变更并发，最终一致。
func TestProviderConsumerUpdate(t *testing.T) {
	root := New()
	defer root.Close()
	k := NewKey[int]("provider")

	var mu sync.Mutex
	var applied []int // consumer 每次装载观察到的 provider 值

	provider := func(v int) Component {
		return Component{
			Name:    fmt.Sprintf("provider-%d", v),
			Provide: []Key{k},
			Apply: func(ctx *Context) (Inverse, error) {
				if _, err := ctx.Provide(k, v); err != nil {
					return nil, err
				}
				return nil, nil
			},
		}
	}
	consumer := Component{
		Name:   "consumer",
		Inject: []Key{k},
		Apply: func(ctx *Context) (Inverse, error) {
			v, err := Service[int](ctx, k)
			if err != nil {
				return nil, err
			}
			mu.Lock()
			applied = append(applied, v)
			mu.Unlock()
			return nil, nil
		},
	}

	g := stdctx.Background()
	p1 := root.Load(provider(1))
	if err := p1.Ready(g); err != nil {
		t.Fatal(err)
	}
	c := root.Load(consumer)
	if err := c.Ready(g); err != nil {
		t.Fatal(err)
	}

	// provider 更新（撤退旧值、装载新值）与 consumer 的既有生命周期并发。
	// 同键换血必须等旧提供者完全撤退（Gone），否则新提供者的 apply 会被
	// ErrDuplicateProvide 拒绝（Def.58 的 fail-fast 语义）。
	p1.Dispose()
	if err := p1.Gone(g); err != nil {
		t.Fatal(err)
	}
	p2 := root.Load(provider(2))
	if err := p2.Ready(g); err != nil {
		t.Fatal(err)
	}
	// consumer 因依赖闪断经历卸载-重载，最终观察到新值。
	if err := c.Ready(g); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		last := applied[len(applied)-1]
		mu.Unlock()
		if last == 2 {
			break
		}
		time.Sleep(2 * time.Millisecond)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(applied) == 0 || applied[len(applied)-1] != 2 {
		t.Fatalf("consumer observations = %v, want last = 2", applied)
	}
	// 观察序列单调无错乱：只允许 1（可能多次，因依赖闪断重载）最终 2。
	for _, v := range applied {
		if v != 1 && v != 2 {
			t.Fatalf("unexpected observation %d in %v", v, applied)
		}
	}
}

// 依赖门控：consumer 先于 provider 装载，必须等待依赖就绪。
func TestInjectGating(t *testing.T) {
	root := New()
	defer root.Close()
	k := NewKey[string]("dep")

	consumerStart := atomic.Int64{}
	c := root.Load(Component{
		Name:   "consumer",
		Inject: []Key{k},
		Apply: func(ctx *Context) (Inverse, error) {
			consumerStart.Add(1)
			v, err := Service[string](ctx, k)
			if err != nil {
				return nil, err
			}
			if v != "ready" {
				return nil, fmt.Errorf("consumer ran before dep ready: %q", v)
			}
			return nil, nil
		},
	})
	time.Sleep(30 * time.Millisecond)
	if got := consumerStart.Load(); got != 0 {
		t.Fatalf("consumer apply ran %d times without dependency", got)
	}
	if s := c.State(); s != StatePending {
		t.Fatalf("state = %v, want pending", s)
	}

	p := root.Load(Component{
		Name:    "provider",
		Provide: []Key{k},
		Apply: func(ctx *Context) (Inverse, error) {
			_, err := ctx.Provide(k, "ready")
			return nil, err
		},
	})
	g := stdctx.Background()
	if err := p.Ready(g); err != nil {
		t.Fatal(err)
	}
	if err := c.Ready(g); err != nil {
		t.Fatal(err)
	}
	if got := consumerStart.Load(); got != 1 {
		t.Fatalf("consumer apply count = %d, want 1", got)
	}
}

// 嵌套 Load：apply 内装载子组件并等待其就绪（orchestrator 不得死锁）。
func TestNestedLoad(t *testing.T) {
	root := New()
	defer root.Close()
	k := UntypedKey("nested")
	kOuter := UntypedKey("nested-outer")

	var childEffect atomic.Int64
	f := root.Load(Component{
		Name: "outer",
		Apply: func(ctx *Context) (Inverse, error) {
			child := ctx.Load(Component{
				Name: "inner",
				Apply: func(c2 *Context) (Inverse, error) {
					if err := c2.Effect(func() Inverse {
						childEffect.Add(1)
						return func() error { childEffect.Add(-1); return nil }
					}); err != nil {
						return nil, err
					}
					if _, err := c2.Provide(k, "inner"); err != nil {
						return nil, err
					}
					return nil, nil
				},
			})
			if err := child.Ready(stdctx.Background()); err != nil {
				return nil, fmt.Errorf("inner: %w", err)
			}
			if _, err := ctx.Provide(kOuter, "outer"); err != nil {
				return nil, err
			}
			return nil, nil
		},
	})
	g := stdctx.Background()
	if err := f.Ready(g); err != nil {
		t.Fatal(err)
	}
	// 各自的键各归其位（同键双 fiber 提供被良构性强制拒绝）。
	if v, _ := root.resolve(k); v != "inner" {
		t.Fatalf("inner visible = %v, want inner", v)
	}
	if v, _ := root.resolve(kOuter); v != "outer" {
		t.Fatalf("outer visible = %v, want outer", v)
	}
	if got := childEffect.Load(); got != 1 {
		t.Fatalf("child effect = %d, want 1", got)
	}
	f.Dispose()
	if err := f.Gone(g); err != nil {
		t.Fatal(err)
	}
	// D7 收窄：子 fiber 独立于父，父撤退后子依然存活。
	if got := childEffect.Load(); got != 1 {
		t.Fatalf("child effect after outer dispose = %d, want 1 (D7: 子 fiber 独立存活)", got)
	}
	// 根 Close 终结一切（orchestrator 显式撤退全部注册 fiber）。
	if err := root.Close(); err != nil {
		t.Fatal(err)
	}
	if got := childEffect.Load(); got != 0 {
		t.Fatalf("child effect after root.Close = %d, want 0", got)
	}
}

// ------------------------------------------------------------------
// 枚举 API（stc-go#4）：Fibers 是注册表的只读快照，取代消费者侧
// 与注册表并行维护的手工目录（副本与真相漂移的根治）。
// ------------------------------------------------------------------

// 装载后枚举含全部在册 fiber：Active 与依赖蛰伏（Pending）都在，
// ID 升序；同树的子作用域 context 看到同一视图；异树互不可见。
func TestFibersSnapshot(t *testing.T) {
	root := New()
	defer root.Close()
	k := UntypedKey("dep")

	root.Load(Component{ // 依赖缺失 → 蛰伏在册
		Name:   "gated",
		Inject: []Key{k},
		Apply:  func(ctx *Context) (Inverse, error) { return nil, nil },
	})
	a := root.Load(Component{Name: "a", Apply: func(ctx *Context) (Inverse, error) { return nil, nil }})
	child := root.Child()
	b := child.Load(Component{Name: "b", Apply: func(ctx *Context) (Inverse, error) { return nil, nil }})

	g := stdctx.Background()
	if err := a.Ready(g); err != nil {
		t.Fatal(err)
	}
	if err := b.Ready(g); err != nil {
		t.Fatal(err)
	}

	fs := root.Fibers()
	if len(fs) != 3 {
		t.Fatalf("Fibers() = %d 项, want 3", len(fs))
	}
	if !(fs[0].ID() < fs[1].ID() && fs[1].ID() < fs[2].ID()) {
		t.Fatalf("Fibers() 未按 ID 升序: %v", []uint64{fs[0].ID(), fs[1].ID(), fs[2].ID()})
	}
	for _, f := range fs {
		var want FiberState
		switch f.Name() {
		case "gated":
			want = StatePending
		case "a", "b":
			want = StateActive
		default:
			t.Fatalf("unexpected fiber %q in snapshot", f.Name())
		}
		if f.State() != want {
			t.Fatalf("fiber %q state = %v, want %v", f.Name(), f.State(), want)
		}
	}
	// 同树的子作用域观察同一注册表。
	if got := len(child.Fibers()); got != 3 {
		t.Fatalf("child.Fibers() = %d 项, want 3（同树共享注册表）", got)
	}

	// 另一棵树互不可见。
	other := New()
	defer other.Close()
	if got := len(other.Fibers()); got != 0 {
		t.Fatalf("other.Fibers() = %d 项, want 0（异树隔离）", got)
	}
}

// 卸载与装载失败都出册：Dispose→Gone 后从快照消失；apply 出错
// （Failed）的 fiber 同样不在册。
func TestFibersAfterDispose(t *testing.T) {
	root := New()
	defer root.Close()

	f := root.Load(Component{Name: "tmp", Apply: func(ctx *Context) (Inverse, error) { return nil, nil }})
	if err := f.Ready(stdctx.Background()); err != nil {
		t.Fatal(err)
	}
	if got := len(root.Fibers()); got != 1 {
		t.Fatalf("Fibers() = %d 项, want 1", got)
	}
	f.Dispose()
	if err := f.Gone(stdctx.Background()); err != nil {
		t.Fatal(err)
	}
	if got := len(root.Fibers()); got != 0 {
		t.Fatalf("dispose 后 Fibers() = %d 项, want 0", got)
	}

	bad := root.Load(Component{Name: "bad", Apply: func(ctx *Context) (Inverse, error) {
		return nil, fmt.Errorf("boom")
	}})
	if err := bad.Ready(stdctx.Background()); err == nil {
		t.Fatal("want load error")
	}
	if got := len(root.Fibers()); got != 0 {
		t.Fatalf("装载失败后 Fibers() = %d 项, want 0", got)
	}
}

// 并发装载/撤退/枚举：快照经 RLock 读取、句柄状态原子现读，
// -race 背书无竞态；全部撤退后注册表回到空。
func TestFibersConcurrent(t *testing.T) {
	root := New()
	defer root.Close()

	const workers = 4
	const rounds = 50
	var wg sync.WaitGroup
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for i := 0; i < rounds; i++ {
				f := root.Load(Component{
					Name:  fmt.Sprintf("w%d-%d", w, i),
					Apply: func(ctx *Context) (Inverse, error) { return nil, nil },
				})
				f.Dispose()
				// 现读快照句柄（快照后状态可变，不对其值断言；
				// 此处为 -race 覆盖并发读路径）。
				for _, x := range root.Fibers() {
					_ = x.ID()
					_ = x.State()
				}
			}
		}(w)
	}
	wg.Wait()

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if len(root.Fibers()) == 0 {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatalf("全部撤退后 Fibers() 未归零: %d 项", len(root.Fibers()))
}
