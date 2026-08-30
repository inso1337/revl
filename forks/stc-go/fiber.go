package stc

import (
	stdctx "context"
	"errors"
	"sort"
	"sync/atomic"
)

// FiberState 是 fiber 的生命周期状态（论文 §4.3 的转移系统）：
//
//	Pending → Loading → Active → Unloading → Pending（依赖恢复，重新装载）
//	Loading →（apply 出错）→ Unloading → Failed
//	任意态 →（显式 Dispose）→ Unloading → Gone
type FiberState uint32

const (
	StatePending FiberState = iota
	StateLoading
	StateActive
	StateUnloading
	StateFailed
	StateGone
)

func (s FiberState) String() string {
	switch s {
	case StatePending:
		return "pending"
	case StateLoading:
		return "loading"
	case StateActive:
		return "active"
	case StateUnloading:
		return "unloading"
	case StateFailed:
		return "failed"
	case StateGone:
		return "gone"
	}
	return "unknown"
}

// Component 是组件的静态描述（论文 ⟨d, p, e⟩ 的 Go 形态）：
// Inject 声明协效应依赖；Apply 是装载本体，返回最外层的逆。
// Apply 在 fiber 自己的 context 上执行，其中注册的一切效应
// 都会在 fiber 卸载时回卷。
type Component struct {
	Name    string
	Inject  []Key
	Provide []Key // 声明性元数据，运行时以实际 Provide 调用为准
	Apply   func(ctx *Context) (Inverse, error)
}

// Fiber 是组件的一次实例化（论文 fiber）。Load 创建，Dispose 撤退。
type Fiber struct {
	id   uint64
	comp Component
	home *Context // 装载目标（Load 的接收者）；每次装载在它之下派生新 ctx

	// 当前装载周期的 context：orchestrator 在每个装载周期重写，
	// 用户经 Context() 读取——必须原子，否则与惯性重载构成数据竞争。
	ctxPtr atomic.Pointer[Context]

	state atomic.Uint32
	err   error // apply 的错误（Failed 态）

	sh *shared

	// 以下字段由 orchestrator 串行访问。
	disposeRequested bool
	failed           bool
	depSnap          map[Key]provideIdent // 本次装载时的依赖代际快照
}

// Load 把组件装载进当前 context 的子作用域。
// 立即返回 fiber 句柄；装载异步进行，用 Ready 等待。
// 依赖未就绪时 fiber 停在 Pending，依赖提供后自动开始装载。
func (c *Context) Load(comp Component) *Fiber {
	c.sh.mu.Lock()
	c.sh.seq++
	f := &Fiber{id: c.sh.seq, comp: comp, sh: c.sh}
	closed := c.closed || c.unwinding
	c.sh.mu.Unlock()

	if closed {
		// 装载进已回卷的 context：直接 Failed，不进入注册表。
		f.err = ErrInactive
		f.state.Store(uint32(StateFailed))
		return f
	}
	f.home = c
	if !c.sh.orch.send(cmdLoad{f: f}) {
		// orchestrator 已退出（Close 进行中/之后）：绝不悬挂。
		f.err = ErrDisposed
		f.state.Store(uint32(StateFailed))
	}
	return f
}

// Fibers 返回本树 fiber 注册表的只读快照：经 Load 装载且尚未出册的
// 全部 fiber 句柄，按 ID 升序。注册表随 New() 每棵树一份——根与任意
// 子作用域 context 观察的是同一棵树的视图，多棵树互不可见。
// 快照只固定成员集合，不冻结生命周期：句柄的 ID/Name/State 等访问器
// 并发安全，状态以现读为准。Dispose→Gone 与装载失败（Failed）的
// fiber 均已出册，不会出现在之后的快照中。
func (c *Context) Fibers() []*Fiber {
	c.sh.mu.RLock()
	defer c.sh.mu.RUnlock()
	out := make([]*Fiber, 0, len(c.sh.registry))
	for _, f := range c.sh.registry {
		out = append(out, f)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].id < out[j].id })
	return out
}

// Dispose 请求撤退 fiber：回卷其全部效应并从注册表移除。
// 幂等；装载中的 fiber 会在装载完成后撤退。
func (f *Fiber) Dispose() {
	f.sh.orch.send(cmdDispose{f: f})
}

func (f *Fiber) State() FiberState { return FiberState(f.state.Load()) }

func (f *Fiber) ID() uint64   { return f.id }
func (f *Fiber) Name() string { return f.comp.Name }
func (f *Fiber) Err() error {
	if f.State() == StateFailed { // atomic 读建立 HB，保证 err 可见
		return f.err
	}
	return nil
}

// Context 返回当前装载周期的 context；惯性重载会更换它，
// 调用方可能读到上一周期的（已回卷）context。
func (f *Fiber) Context() *Context { return f.ctxPtr.Load() }

// Ready 等待 fiber 进入稳定态：Active（装载完成）、Failed（装载出错）
// 或 Gone（已撤退）。依赖缺失时 fiber 停在 Pending——Ready 会一直等待，
// 需要超时控制请传入可取消的 ctx。
func (f *Fiber) Ready(ctx stdctx.Context) error {
	for {
		ch := f.sh.waitCh() // 先订阅，后查状态（防丢失唤醒）
		switch f.State() {
		case StateActive:
			return nil
		case StateFailed:
			return f.err
		case StateGone:
			return ErrDisposed
		}
		select {
		case <-ch:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// Gone 等待 fiber 撤出注册表：到达 Gone（显式撤退完成）或 Failed
// （装载失败，已回卷出册）即返回——两者都是出册终态。
func (f *Fiber) Gone(ctx stdctx.Context) error {
	for {
		ch := f.sh.waitCh() // 先订阅，后查状态
		switch f.State() {
		case StateGone, StateFailed:
			return nil
		}
		select {
		case <-ch:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// ErrDisposed 由 Ready 在 fiber 已撤退时返回。
var ErrDisposed = errors.New("stc: fiber disposed")

// setState 由 orchestrator 调用（串行），更新状态并广播等待者。
func (f *Fiber) setState(s FiberState) {
	f.state.Store(uint32(s))
	f.sh.traceFiber(s, f.id)
	f.sh.broadcast()
}

// ------------------------------------------------------------------

// waitCh/broadcast：close-and-replace 广播，供 Ready/Gone 等待状态变化。
func (sh *shared) waitCh() chan struct{} {
	sh.mu.RLock()
	defer sh.mu.RUnlock()
	return sh.fiberCh
}

func (sh *shared) broadcast() {
	sh.mu.Lock()
	close(sh.fiberCh)
	sh.fiberCh = make(chan struct{})
	sh.mu.Unlock()
}

func (sh *shared) traceFiber(s FiberState, id uint64) {
	sh.mu.Lock()
	defer sh.mu.Unlock()
	var kind TraceKind
	switch s {
	case StateLoading:
		kind = TraceLoading
	case StateActive:
		kind = TraceActive
	case StateUnloading:
		kind = TraceUnloading
	case StatePending:
		kind = TracePending
	case StateFailed:
		kind = TraceFailed
	case StateGone:
		kind = TraceGone
	}
	sh.traceLocked(kind, id, "")
}
