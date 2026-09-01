package stc

import (
	stdctx "context"
	"fmt"
	"math/rand"
	"sync"
	"testing"
	"time"
)

// ==================================================================
// 验收 = 论文 §4.4 五条元理论定理的 property-based 测试。
// 生成器保证独立性前提（T61/T73 的条件）：组件提供键互不相交、
// 依赖图为 DAG。
// ==================================================================

// genComp 生成的组件：独占提供 own 键、注册净计数效应、可选注入低层键。
type genComp struct {
	name   string
	own    Key
	inject []Key
}

func genSystem(n int, rng *rand.Rand) (root *Context, comps []genComp, j *journal, keys []Key) {
	root = New()
	j = newJournal()
	names := make([]string, n)
	for i := range names {
		names[i] = fmt.Sprintf("c%02d", i)
	}
	// 层级 DAG：组件 i 只注入 j < i 的键（无环）。
	for i, name := range names {
		var inject []Key
		for _, jdx := range rng.Perm(i) {
			if rng.Intn(3) == 0 {
				inject = append(inject, UntypedKey(names[jdx]))
			}
			if len(inject) >= 2 {
				break
			}
		}
		comps = append(comps, genComp{name: name, own: UntypedKey(name), inject: inject})
		keys = append(keys, comps[i].own)
	}
	return root, comps, j, keys
}

func compOf(root *Context, gc genComp, j *journal) Component {
	return Component{
		Name:    gc.name,
		Inject:  gc.inject,
		Provide: []Key{gc.own},
		Apply: func(ctx *Context) (Inverse, error) {
			if _, err := ctx.Provide(gc.own, gc.name); err != nil {
				return nil, err
			}
			if err := ctx.Effect(func() Inverse {
				j.add(gc.name, 1)
				return func() error { j.add(gc.name, -1); return nil }
			}); err != nil {
				return nil, err
			}
			return nil, nil
		},
	}
}

type journal struct {
	mu sync.Mutex
	n  map[string]int
}

func newJournal() *journal { return &journal{n: map[string]int{}} }

func (j *journal) add(name string, d int) {
	j.mu.Lock()
	j.n[name] += d
	j.mu.Unlock()
}

func (j *journal) snapshot() map[string]int {
	j.mu.Lock()
	defer j.mu.Unlock()
	out := make(map[string]int, len(j.n))
	for k, v := range j.n {
		out[k] = v
	}
	return out
}

// observable 是静默态的全局可观察状态：各键的可见值 + 各组件的净效应。
func observable(root *Context, keys []Key, j *journal) map[string]any {
	m := make(map[string]any)
	for _, k := range keys {
		if v, ok := root.resolve(k); ok {
			m[k.name] = v
		}
	}
	for name, n := range j.snapshot() {
		if n != 0 {
			m["fx:"+name] = n
		}
	}
	return m
}

func mustEqual(t *testing.T, what string, got, want map[string]any) {
	t.Helper()
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("%s mismatch:\n got  %v\n want %v", what, got, want)
	}
}

// P1（T59 Preservation）：随机操作序列中，每次操作后的静默点上
// 注册表良构不变量保持。
func TestPropertyPreservation(t *testing.T) {
	for seed := range 100 {
		rng := rand.New(rand.NewSource(int64(seed)))
		root, comps, j, keys := genSystem(8, rng)
		var fibers []*Fiber
		var fiberComp []int // fiber 下标 → 组件下标（live 去重用）
		live := map[int]bool{}
		check := func() {
			waitQuiet(t, root, fibers, 5*time.Second)
			checkInvariants(t, root)
		}
		for op := range 24 {
			switch rng.Intn(3) {
			case 0, 1:
				// 只装载未在架的组件：同键重复装载被 ErrDuplicateProvide
				// 拒绝（Def.58 的 fail-fast 语义），在本性质的前提范围之外。
				var avail []int
				for i := range comps {
					if !live[i] {
						avail = append(avail, i)
					}
				}
				if len(avail) == 0 {
					continue
				}
				i := avail[rng.Intn(len(avail))]
				live[i] = true
				fiberComp = append(fiberComp, i)
				fibers = append(fibers, root.Load(compOf(root, comps[i], j)))
			case 2:
				if len(fibers) > 1 {
					i := rng.Intn(len(fibers))
					fibers[i].Dispose()
					delete(live, fiberComp[i])
					fiberComp = append(fiberComp[:i], fiberComp[i+1:]...)
					fibers = append(fibers[:i], fibers[i+1:]...)
				}
			}
			// 每次操作后推进到静默：上一步 Dispose 的撤退在此完成，
			// 下一步的重新装载不会撞上未移除的同键条目。
			check()
			_ = op
		}
		root.Close()
		_ = keys
	}
}

// checkInvariants 校验注册表良构性（T59 的不变量集）。
// 仅在静默点调用（waitQuiet 之后），此时所有 fiber 处于终态。
func checkInvariants(t *testing.T, root *Context) {
	t.Helper()
	sh := root.sh
	sh.mu.RLock()
	defer sh.mu.RUnlock()
	for pk, st := range sh.provides {
		fiberOwned := 0
		for _, e := range st {
			f := e.owner.fiber
			if f == nil {
				continue // 手动 context（根/Child）的提供，由其自身生命周期管理
			}
			fiberOwned++
			// Def.58 良构性：每 (key, realm) 至多一个 fiber 提供者。
			if fiberOwned > 1 {
				t.Fatalf("invariant (Def.58): %q has %d fiber providers", pk.key.name, fiberOwned)
			}
			reg, ok := sh.registry[f.id]
			if !ok {
				t.Fatalf("invariant: %q has entry owned by unregistered fiber %d", pk.key.name, f.id)
			}
			if reg != f {
				t.Fatalf("invariant: registry id collision on fiber %d", f.id)
			}
			// 静默点上拥有提供条目的 fiber 必须是 Active：
			// Pending/Failed/Gone 的条目已被卸载移除，Loading/Unloading
			// 不是终态，不会出现在静默点。
			if f.State() != StateActive {
				t.Fatalf("invariant: fiber %d state %v still owns provide of %q", f.id, f.State(), pk.key.name)
			}
		}
	}
	for id, f := range sh.registry {
		if f.ID() != id {
			t.Fatalf("invariant: fiber id mismatch %d != %d", f.ID(), id)
		}
		if f.home == nil {
			t.Fatalf("invariant: fiber %d has no home", id)
		}
		// fiber 的当前 context 必须挂在其 home 之下（parent 链可达 home）。
		if fc := f.ctxPtr.Load(); fc != nil {
			for c := fc; c != nil; c = c.parent {
				if c == f.home {
					goto okCtx
				}
			}
			t.Fatalf("invariant: fiber %d context detached from home", id)
		}
	okCtx:
	}
}

// P2（T61 Recovery exactness）：撤销子集 S 后的状态 ≡ S 从未装载。
func TestPropertyRecoveryExactness(t *testing.T) {
	for seed := range 60 {
		rng := rand.New(rand.NewSource(int64(seed)))

		// 运行 A：全部装载 → 撤退随机子集 S（随机顺序）。
		rootA, compsA, jA, keys := genSystem(6, rng)
		fibers := make([]*Fiber, len(compsA))
		for i, gc := range compsA {
			fibers[i] = rootA.Load(compOf(rootA, gc, jA))
		}
		waitQuiet(t, rootA, fibers, 5*time.Second)
		for _, f := range fibers {
			if err := f.Ready(stdctx.Background()); err != nil {
				t.Fatalf("seed %d: %v", seed, err)
			}
		}
		var subset []*Fiber
		for _, f := range fibers {
			if rng.Intn(2) == 0 {
				subset = append(subset, f)
			}
		}
		for _, i := range rng.Perm(len(subset)) {
			subset[i].Dispose()
		}
		for _, f := range subset {
			if err := f.Gone(stdctx.Background()); err != nil {
				t.Fatalf("seed %d: gone: %v", seed, err)
			}
		}
		waitQuiet(t, rootA, fibers, 5*time.Second)
		finalA := observable(rootA, keys, jA)

		// 运行 B（基线）：同种子重放生成器，保证组件图与运行 A 一致。
		rngB := rand.New(rand.NewSource(int64(seed)))
		rootB, compsB, jB, keysB := genSystem(6, rngB)
		_ = keysB
		inS := map[string]bool{}
		for _, f := range subset {
			inS[f.Name()] = true
		}
		var fibersB []*Fiber
		for _, gc := range compsB {
			if inS[gc.name] {
				continue
			}
			fibersB = append(fibersB, rootB.Load(compOf(rootB, gc, jB)))
		}
		waitQuiet(t, rootB, fibersB, 5*time.Second)
		finalB := observable(rootB, keys, jB)

		mustEqual(t, fmt.Sprintf("seed %d recovery exactness", seed), finalA, finalB)
		rootA.Close()
		rootB.Close()
	}
}

// P3（T63 Ordering）：fiber 进入 Loading 的时刻不早于其全部依赖的提供。
func TestPropertyOrdering(t *testing.T) {
	for seed := range 50 {
		rng := rand.New(rand.NewSource(int64(seed)))
		root, comps, j, _ := genSystem(10, rng)
		injectOf := map[string][]string{}
		for _, gc := range comps {
			var names []string
			for _, k := range gc.inject {
				names = append(names, k.name)
			}
			injectOf[gc.name] = names
		}
		var mu sync.Mutex
		idName := map[uint64]string{}
		var wg sync.WaitGroup
		for _, i := range rng.Perm(len(comps)) {
			gc := comps[i]
			wg.Add(1)
			go func() {
				defer wg.Done()
				f := root.Load(compOf(root, gc, j))
				mu.Lock()
				idName[f.ID()] = f.Name()
				mu.Unlock()
			}()
		}
		wg.Wait()

		deadline := time.Now().Add(5 * time.Second)
		for time.Now().Before(deadline) && len(idName) < len(comps) {
			time.Sleep(2 * time.Millisecond)
		}
		trace := root.Trace()
		provideSeq := map[string]uint64{}
		for _, ev := range trace {
			switch ev.Kind {
			case TraceProvide:
				if _, seen := provideSeq[ev.Key]; !seen {
					provideSeq[ev.Key] = ev.Seq // 首次提供
				}
			case TraceLoading:
				for _, dep := range injectOf[idName[ev.Fiber]] {
					ps, ok := provideSeq[dep]
					if !ok || ps > ev.Seq {
						t.Fatalf("seed %d: fiber %d loading(%d) before dep %q provided(%v)",
							seed, ev.Fiber, ev.Seq, dep, ps)
					}
				}
			case TraceApplyStart:
				// apply 一律晚于其 Loading 事件。
			}
		}
		root.Close()
	}
}

// P4（T66 Progress）：依赖无环的系统在有界命令数内达到静默。
func TestPropertyProgress(t *testing.T) {
	for seed := range 40 {
		rng := rand.New(rand.NewSource(int64(seed)))
		root, comps, j, _ := genSystem(12, rng)
		var fibers []*Fiber
		for _, gc := range comps {
			fibers = append(fibers, root.Load(compOf(root, gc, j)))
		}
		waitQuiet(t, root, fibers, 5*time.Second)
		n := len(comps)
		if cmds := root.sh.orch.cmds.Load(); cmds > int64(10*n+60) {
			t.Fatalf("seed %d: %d commands for %d components, want ≤ %d", seed, cmds, n, 10*n+60)
		}
		for _, f := range fibers {
			if f.State() != StateActive {
				t.Fatalf("seed %d: fiber %s state %v, want active", seed, f.Name(), f.State())
			}
		}

		// 抖动：反复撤供/复供，系统仍应有界静默。
		if len(comps) > 2 {
			cur := fibers[0]
			for range 3 {
				cur.Dispose()
				if err := cur.Gone(stdctx.Background()); err != nil {
					t.Fatal(err)
				}
				cur = root.Load(compOf(root, comps[0], j))
				if err := cur.Ready(stdctx.Background()); err != nil {
					t.Fatal(err)
				}
			}
			waitQuiet(t, root, fibers, 5*time.Second)
		}
		root.Close()
	}
}

// P5（T73 Confluence）：同一操作多重集的任意调度顺序，静默终态互相等价。
func TestPropertyConfluence(t *testing.T) {
	const runs = 200
	var want map[string]any
	for run := range runs {
		final, states, root := runConfluence(t, int64(run), false)
		if run == 0 {
			want = final
			wantStates = states
			root.Close()
			continue
		}
		mustEqual(t, fmt.Sprintf("run %d confluence", run), final, want)
		if fmt.Sprint(states) != fmt.Sprint(wantStates) {
			t.Fatalf("run %d fiber states %v, want %v", run, states, wantStates)
		}
		root.Close()
	}
	// 并发提交变体（-race 下随机交错）。
	for run := range 40 {
		final, states, root := runConfluence(t, 10_000+int64(run), true)
		mustEqual(t, fmt.Sprintf("concurrent run %d confluence", run), final, want)
		if fmt.Sprint(states) != fmt.Sprint(wantStates) {
			t.Fatalf("concurrent run %d states %v, want %v", run, states, wantStates)
		}
		root.Close()
	}
}

var wantStates map[string]string

// runConfluence 装载固定组件集后撤退固定子集，调度顺序由 seed 决定。
// confComps/confKeys 是汇合试验的固定组件图（生成一次），
// 种子只决定调度顺序——否则每次运行比较的是不同系统。
var (
	confOnce  sync.Once
	confComps []genComp
	confKeys  []Key
)

func confGraph() ([]genComp, []Key) {
	confOnce.Do(func() {
		rng := rand.New(rand.NewSource(20260814))
		_, comps, _, keys := genSystem(6, rng)
		confComps = comps
		confKeys = keys
	})
	return confComps, confKeys
}

func runConfluence(t *testing.T, seed int64, concurrent bool) (final map[string]any, states map[string]string, root *Context) {
	rng := rand.New(rand.NewSource(seed))
	nComp := 6
	comps, keys := confGraph()
	root = New()
	j := newJournal()

	// 固定子集：偶数号组件最终被撤退（每 seed 相同的多重集）。
	disposeSet := map[string]bool{}
	for i := 0; i < nComp; i += 2 {
		disposeSet[comps[i].name] = true
	}

	fibers := make([]*Fiber, nComp)
	byName := map[string]*Fiber{}
	var mu sync.Mutex

	loadOp := func(i int) {
		f := root.Load(compOf(root, comps[i], j))
		mu.Lock()
		fibers[i] = f
		byName[comps[i].name] = f
		mu.Unlock()
	}
	disposeOp := func(name string) {
		mu.Lock()
		f := byName[name]
		mu.Unlock()
		if f != nil {
			f.Dispose()
		}
	}

	// 操作多重集：6 个 Load + 3 个 Dispose（偶数号）。
	// 操作多重集固定：全部 Load（随机序）+ 偶数号 Dispose（随机序）。
	// Dispose 因果上晚于对应 Load（先行提交全部 Load），否则 Dispose
	// 语义上不存在，不构成同一操作多重集。
	var wg sync.WaitGroup
	for _, i := range rng.Perm(nComp) {
		i := i
		wg.Add(1)
		go func() { defer wg.Done(); loadOp(i) }()
	}
	wg.Wait()
	var names []string
	for i := 0; i < nComp; i += 2 {
		names = append(names, comps[i].name)
	}
	if concurrent {
		for _, x := range rng.Perm(len(names)) {
			name := names[x]
			wg.Add(1)
			go func() { defer wg.Done(); disposeOp(name) }()
		}
		wg.Wait()
	} else {
		for _, x := range rng.Perm(len(names)) {
			disposeOp(names[x])
		}
	}

	waitQuiet(t, root, nonNil(fibers), 5*time.Second)
	states = map[string]string{}
	mu.Lock()
	for name, f := range byName {
		states[name] = f.State().String()
	}
	mu.Unlock()
	final = observable(root, keys, j)
	return final, states, root
}

func nonNil(fs []*Fiber) []*Fiber {
	var out []*Fiber
	for _, f := range fs {
		if f != nil {
			out = append(out, f)
		}
	}
	return out
}

// FuzzInterleaving：把并发汇合试验交给模糊测试引擎扩展种子。
// 单次运行验证不变量保持与无死锁；跨运行等价由 P5 覆盖。
func FuzzInterleaving(f *testing.F) {
	for _, s := range []int64{1, 7, 42, 2026} {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, seed int64) {
		_, _, root := runConfluence(t, seed, true)
		checkInvariants(t, root)
		root.Close()
	})
}
