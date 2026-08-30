package stc

import (
	"errors"
	"log"
	"sort"
	"sync"
	"sync/atomic"
)

// orchestrator 是 fiber 生命周期转移的唯一决策者（D3）：
// 单 goroutine 事件循环串行处理全部转移；Apply 与逆操作在锁外
// 的独立 goroutine 中运行，其完成以事件回流，循环本身从不阻塞。
// 这正是论文 §4 中 orchestrator 与 fiber 迭代器交错的运行时形态，
// 也使嵌套 Load（apply 内再 Load 子组件并等待）不会死锁。
type orchestrator struct {
	sh    *shared
	inbox chan cmd
	done  chan struct{}

	// sendMu 与退出判定协同闭合发送竞态：退出前在 sendMu 内检查
	// 无在飞命令并置 exited；send 在 sendMu 内检查 exited 并原子入队。
	sendMu sync.Mutex
	exited bool

	cmds     atomic.Int64
	inflight atomic.Int64 // 已提交未处理完毕的命令数（静默判定）
	stopping atomic.Bool
	stopOnce sync.Once
}

type cmd interface{}

type (
	cmdLoad    struct{ f *Fiber }
	cmdDispose struct{ f *Fiber }
	cmdApplied struct {
		f   *Fiber
		inv Inverse
		err error
	}
	cmdUnwound struct{ f *Fiber }
	cmdService struct{}
)

func newOrchestrator(sh *shared) *orchestrator {
	return &orchestrator{
		sh:    sh,
		inbox: make(chan cmd, 4096),
		done:  make(chan struct{}),
	}
}

func (o *orchestrator) start() { go o.run() }

// send 入队一条命令；返回 false 表示循环已退出（调用方必须感知）。
// inflight 在入队前递增、在循环处理完毕后递减：从 inbox 取出命令会立刻
// 使其清空，但处理尚未完成——静默判定必须依赖 inflight 而非队列长度。
// exited 检查与入队同在 sendMu 内：与循环的退出判定互斥，无漏发/漏停窗口。
func (o *orchestrator) send(c cmd) bool {
	o.sendMu.Lock()
	defer o.sendMu.Unlock()
	if o.exited {
		return false
	}
	o.inflight.Add(1)
	o.inbox <- c
	return true
}

func (o *orchestrator) notifyService() { _ = o.send(cmdService{}) }

func (o *orchestrator) run() {
	defer close(o.done)
	for {
		c := <-o.inbox
		o.cmds.Add(1)
		o.handle(c)
		o.settle()
		o.inflight.Add(-1)
		if o.stopping.Load() {
			// 退出判定与 send 互斥：此刻之后任何 send 都会因 exited 被拒，
			// 不会有命令落在死信队列里。
			o.sendMu.Lock()
			if o.inflight.Load() == 0 && len(o.inbox) == 0 && o.quiescent() {
				o.exited = true
				o.sendMu.Unlock()
				return
			}
			o.sendMu.Unlock()
		}
	}
}

// quiescent：注册表清空且无在飞的 apply/unwind。
func (o *orchestrator) quiescent() bool {
	o.sh.mu.RLock()
	n := len(o.sh.registry)
	o.sh.mu.RUnlock()
	return n == 0 && o.sh.pending.Load() == 0
}

// shutdown 停止系统并等待循环退出。幂等。
// 撤退不在此处快照发送——快照会漏掉仍在 inbox 中的 cmdLoad（其 fiber
// 尚未注册），漏网 fiber 永不撤退导致注册表无法清空。撤退决策统一
// 收进 settle（循环内部，天然串行化），此处只置位并唤醒。
func (o *orchestrator) shutdown() {
	o.stopOnce.Do(func() {
		o.stopping.Store(true)
		o.send(cmdService{})
		<-o.done
	})
}

func (o *orchestrator) handle(c cmd) {
	switch c := c.(type) {
	case cmdLoad:
		o.sh.mu.Lock()
		o.sh.registry[c.f.id] = c.f
		o.sh.mu.Unlock()

	case cmdDispose:
		switch c.f.State() {
		case StatePending:
			o.removeFiber(c.f, StateGone)
		case StateLoading, StateUnloading:
			c.f.disposeRequested = true // 完成当前转移后撤退
		case StateActive:
			// Dispose 是终局语义：撤退后不得因依赖仍满足而复活。
			c.f.disposeRequested = true
			o.beginUnload(c.f)
		case StateGone, StateFailed:
			// 幂等：已终结。
		}

	case cmdApplied:
		f := c.f
		o.sh.traceUser(TraceApplied, f.id)
		if c.inv != nil {
			inv := c.inv
			ctx := f.ctxPtr.Load()
			o.sh.mu.Lock()
			if !ctx.unwinding {
				ctx.inverses = append(ctx.inverses, inv)
				inv = nil
			}
			o.sh.mu.Unlock()
			if inv != nil {
				// 防御路径（fresh-context 设计下不可达）：立即自撤销。
				if err := inv(); err != nil {
					log.Printf("stc: stranded inverse error: %v", err)
				}
			}
		}
		switch {
		case c.err != nil:
			f.err = c.err
			f.failed = true
			o.beginUnload(f)
		case f.disposeRequested:
			o.beginUnload(f)
		case o.stopping.Load():
			o.beginUnload(f) // 关停期间完成装载的 fiber 直接撤退
		case f.depsSatisfied():
			// 装载完成即与"当前"依赖代际一致：重新捕获快照。
			// 装载中途的换血不触发重载（fiber 尚未服役，对应 Cordis
			// inertia lock 2 的 applied-once 语义）；服役后的换血
			// 才由 settle 的 depStale 触发重载。
			f.captureDeps()
			f.setState(StateActive)
		default:
			// 惯性：装载期间依赖消失，装载完成后直接卸载。
			o.beginUnload(f)
		}

	case cmdUnwound:
		f := c.f
		o.sh.traceUser(TraceUnwound, f.id)
		switch {
		case f.failed:
			o.removeFiber(f, StateFailed)
		case f.disposeRequested, o.stopping.Load():
			o.removeFiber(f, StateGone)
		case f.depsSatisfied():
			// 惯性：卸载期间依赖恢复，立即重新装载。
			o.beginLoad(f)
		default:
			f.setState(StatePending)
		}

	case cmdService:
		// 无独立处理；settle 重新评估全部 fiber。
	}
}

// settle 推进到不动点：依赖门控的 Pending→Loading 与 Active→Unloading；
// stopping 期间不再发起新装载，并把一切在册 fiber 推向撤退。
func (o *orchestrator) settle() {
	for {
		o.sh.mu.RLock()
		fs := make([]*Fiber, 0, len(o.sh.registry))
		for _, f := range o.sh.registry {
			fs = append(fs, f)
		}
		o.sh.mu.RUnlock()
		sort.Slice(fs, func(i, j int) bool { return fs[i].id < fs[j].id })

		changed := false
		stopping := o.stopping.Load()
		for _, f := range fs {
			switch f.State() {
			case StatePending:
				switch {
				case stopping && !f.disposeRequested:
					f.disposeRequested = true
					o.removeFiber(f, StateGone)
					changed = true
				case !stopping && f.depsSatisfied():
					o.beginLoad(f)
					changed = true
				}
			case StateActive:
				switch {
				case stopping && !f.disposeRequested:
					f.disposeRequested = true
					o.beginUnload(f)
					changed = true
				case !stopping && (!f.depsSatisfied() || f.depStale()):
					// 依赖消失，或被无缝替换（代际改变）：卸载，恢复后重载。
					o.beginUnload(f)
					changed = true
				}
			case StateLoading, StateUnloading:
				if stopping {
					f.disposeRequested = true
				}
			}
		}
		if !changed {
			return
		}
	}
}

func (o *orchestrator) beginLoad(f *Fiber) {
	// 每个装载周期使用全新 context：上一周期的 context 已被卸载永久关闭，
	// 复用会让惯性重载的全部注册失效（Cordis 每次 mount 亦派生新 ctx）。
	// ctxPtr 原子写：Fiber.Context() 在用户 goroutine 上读。
	ctx := f.home.Child()
	ctx.detach() // fiber 的 context 由其生命周期独占管理（D7）
	o.sh.mu.Lock()
	ctx.fiber = f
	f.ctxPtr.Store(ctx)
	o.sh.mu.Unlock()

	f.captureDeps()
	f.setState(StateLoading)
	o.sh.pending.Add(1)
	apply := f.comp.Apply
	if apply == nil {
		apply = func(*Context) (Inverse, error) {
			return nil, errors.New("stc: component has no Apply")
		}
	}
	go func() {
		o.sh.traceUser(TraceApplyStart, f.id)
		inv, err := apply(ctx)
		o.sh.pending.Add(-1)
		_ = o.send(cmdApplied{f: f, inv: inv, err: err})
	}()
}

func (o *orchestrator) beginUnload(f *Fiber) {
	f.setState(StateUnloading)
	ctx := f.ctxPtr.Load()
	o.sh.pending.Add(1)
	go func() {
		_ = ctx.unwind() // 逆错误已逐个记录（吞没语义）
		o.sh.pending.Add(-1)
		_ = o.send(cmdUnwound{f: f})
	}()
}

func (o *orchestrator) removeFiber(f *Fiber, terminal FiberState) {
	o.sh.mu.Lock()
	delete(o.sh.registry, f.id)
	o.sh.mu.Unlock()
	f.setState(terminal)
}

// depsSatisfied 检查 fiber 的全部 inject 键可解析（不计自身提供的条目）。
// 首次装载前 ctx 尚未派生，用 home 解析（realm 链与子 context 等价）。
func (f *Fiber) depsSatisfied() bool {
	for _, k := range f.comp.Inject {
		if _, _, ok := f.resolveBase().resolveExternal(k, f); !ok {
			return false
		}
	}
	return true
}

func (f *Fiber) resolveBase() *Context {
	if c := f.ctxPtr.Load(); c != nil {
		return c
	}
	return f.home
}

// captureDeps 记录装载时刻的依赖代际；depStale 判定其后是否被替换
// （无缝换血：依赖始终可解析，但提供者已换人）。
func (f *Fiber) captureDeps() {
	f.depSnap = make(map[Key]provideIdent, len(f.comp.Inject))
	for _, k := range f.comp.Inject {
		if _, id, ok := f.resolveBase().resolveExternal(k, f); ok {
			f.depSnap[k] = id
		}
	}
}

func (f *Fiber) depStale() bool {
	for _, k := range f.comp.Inject {
		_, id, ok := f.resolveBase().resolveExternal(k, f)
		if !ok || id != f.depSnap[k] {
			return true
		}
	}
	return false
}
