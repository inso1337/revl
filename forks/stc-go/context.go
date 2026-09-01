package stc

import (
	stdctx "context"
	"fmt"
	"log"
	"reflect"
	"sync"
	"sync/atomic"
)

// Inverse 是与正向动作配对的逆操作。执行可能阻塞（等价于异步逆）；
// 返回的错误被记录，不阻断其余逆的回卷。
type Inverse func() error

// Context 是范式的统一上下文（论文 Γ∞ 的运行时形态）：
// 服务容器、作用域树节点、副作用累加器三合一。
// 通过 New 创建根 context，Child 派生作用域。
type Context struct {
	sh     *shared
	parent *Context
	fiber  *Fiber // 拥有此 context 的 fiber；非 fiber context 为 nil

	values   map[Key]any
	realmFor map[Key]*Realm
	interc   map[Key][]interceptEntry
	inverses []Inverse
	children []*Context

	unwinding bool
	closed    bool
}

// shared 是整棵 context 树共享的可变状态，由单一 RWMutex 保护（D3 决策）。
type shared struct {
	mu        sync.RWMutex
	seq       uint64
	provides  map[provKey][]provideEntry
	listeners map[string][]listenerEntry
	trace     []TraceEvent
	registry  map[uint64]*Fiber

	// 服务变更广播：close-and-replace，供 WaitService 等待。
	svcCh chan struct{}
	// fiber 状态变更广播：close-and-replace，供 Ready/Gone 等待。
	fiberCh chan struct{}

	orch *orchestrator

	pending atomic.Int64 // 运行中的 apply/unwind goroutine 数，静默判定用
}

type provKey struct {
	realm *Realm
	key   Key
}

// provideEntry.owner 记录提供方 context；同一 context 对同一键的
// 不同次提供以 seq 区分。可见值 = 切片最后一个元素（后提供者胜）。
type provideEntry struct {
	owner *Context
	seq   uint64
	value any
}

func (a provideEntry) same(b provideEntry) bool {
	return a.owner == b.owner && a.seq == b.seq
}

// provideIdent 是服务条目的代际标识：条目被替换即代际改变。
type provideIdent struct {
	owner *Context
	seq   uint64
}

func (e provideEntry) ident() provideIdent {
	return provideIdent{owner: e.owner, seq: e.seq}
}

// resolveExternal 解析 key，跳过 self 自己提供的条目——
// fiber 不得以自身的提供满足自己的 inject（自给自足会破坏响应式）。
func (c *Context) resolveExternal(key Key, self *Fiber) (any, provideIdent, bool) {
	c.sh.mu.RLock()
	defer c.sh.mu.RUnlock()
	for r := c.realmForLocked(key); r != nil; r = r.parent {
		st := c.sh.provides[provKey{realm: r, key: key}]
		for i := len(st) - 1; i >= 0; i-- {
			e := st[i]
			if self != nil && e.owner.fiber == self {
				continue
			}
			return e.value, e.ident(), true
		}
	}
	return nil, provideIdent{}, false
}

type listenerEntry struct {
	id    uint64 // sh.seq 分配，可比较的稳定标识（func 值本身不可比较）
	owner *Context
	fn    func(args ...any)
}

type interceptEntry struct {
	id   uint64
	meta any
}

// New 创建根 context 并启动 orchestrator。
func New() *Context {
	sh := &shared{
		provides:  make(map[provKey][]provideEntry),
		listeners: make(map[string][]listenerEntry),
		registry:  make(map[uint64]*Fiber),
		svcCh:     make(chan struct{}),
		fiberCh:   make(chan struct{}),
	}
	sh.orch = newOrchestrator(sh)
	sh.orch.start()
	return &Context{sh: sh}
}

// Child 派生子作用域。realm 与 intercept 的解析沿树向上进行，
// 因此子作用域天然继承外层的隔离与拦截配置。
// 关闭父 context 会先于自身逆序回卷全部子树。
// 已关闭 context 的 Child 返回标记为关闭的 context（注册将得到 ErrInactive）。
func (c *Context) Child() *Context {
	c.sh.mu.Lock()
	defer c.sh.mu.Unlock()
	ch := &Context{sh: c.sh, parent: c}
	if c.closed || c.unwinding {
		ch.closed = true
		return ch
	}
	c.children = append(c.children, ch)
	return ch
}

// detach 把 c 从父 context 的级联回卷列表中摘除（父指针保留，
// realm/intercept 解析仍沿树向上）。fiber 的 context 由其生命周期独占管理。
func (c *Context) detach() {
	c.sh.mu.Lock()
	defer c.sh.mu.Unlock()
	if c.parent == nil {
		return
	}
	kids := c.parent.children
	for i, x := range kids {
		if x == c {
			kids = append(kids[:i], kids[i+1:]...)
			break
		}
	}
	c.parent.children = kids
}

// ------------------------------------------------------------------
// get / set（论文 get(k) / set(k,v)）：context 局部值，树状遮蔽。
// 注意与 Service 的 provide/resolve 是两套命名空间：
// Set/Get 是普通上下文值；服务协效应一律走 Provide / Service。
// ------------------------------------------------------------------

func (c *Context) Set(key Key, value any) error {
	if key.typ != nil {
		rt := reflect.TypeOf(value)
		if rt == nil || !rt.AssignableTo(key.typ) {
			return fmt.Errorf("stc: set %q: %T not assignable to %s", key.name, value, key.typ)
		}
	}
	c.sh.mu.Lock()
	defer c.sh.mu.Unlock()
	if c.closed || c.unwinding {
		return ErrInactive
	}
	if c.values == nil {
		c.values = make(map[Key]any)
	}
	c.values[key] = value
	return nil
}

func (c *Context) Get(key Key) (any, bool) {
	c.sh.mu.RLock()
	defer c.sh.mu.RUnlock()
	for x := c; x != nil; x = x.parent {
		if v, ok := x.values[key]; ok {
			return v, true
		}
	}
	return nil, false
}

// ------------------------------------------------------------------
// 可逆效应（论文 revertible effects）
// ------------------------------------------------------------------

// Effect 注册一个可逆效应：install 执行正向动作并返回其逆（可为 nil）。
// 逆在 context 回卷（Close / fiber 卸载）时按 LIFO 逆序执行。
//
// 约定：install 内部不要再调用同一 context 的 Effect——嵌套注册的
// 逆序语义以顶层调用为单位，install 的返回值被视为最外层的逆。
// 若注册时 context 已在回卷，install 的效果会被立即自撤销并返回 ErrInactive。
func (c *Context) Effect(install func() Inverse) error {
	if install == nil {
		return ErrNilInstall
	}
	inv := install() // 锁外执行：正向动作可能任意耗时
	c.sh.mu.Lock()
	if c.closed || c.unwinding {
		c.sh.mu.Unlock()
		if inv != nil {
			if err := inv(); err != nil {
				log.Printf("stc: self-undo error: %v", err)
			}
		}
		return ErrInactive
	}
	if inv != nil {
		c.inverses = append(c.inverses, inv)
	}
	c.sh.mu.Unlock()
	return nil
}

// unwind 回卷 context：先逆创建序回卷全部子树，再 LIFO 逆序执行自身
// 已注册的逆。单个逆出错被记录且不阻断回卷；返回首个（按执行序）错误。幂等。
func (c *Context) unwind() error {
	c.sh.mu.Lock()
	if c.unwinding {
		c.sh.mu.Unlock()
		return nil
	}
	c.unwinding = true
	c.closed = true
	if c.parent != nil {
		kids := c.parent.children
		for i, x := range kids {
			if x == c {
				kids = append(kids[:i], kids[i+1:]...)
				break
			}
		}
		c.parent.children = kids
	}
	subtree := c.children
	c.children = nil
	invs := c.inverses
	c.inverses = nil
	c.sh.mu.Unlock()

	var firstErr error
	for i := len(subtree) - 1; i >= 0; i-- {
		if err := subtree[i].unwind(); err != nil && firstErr == nil {
			firstErr = err
		}
	}
	for i := len(invs) - 1; i >= 0; i-- {
		if invs[i] == nil {
			continue
		}
		if err := invs[i](); err != nil {
			log.Printf("stc: inverse error (swallowed): %v", err)
			if firstErr == nil {
				firstErr = err
			}
		}
	}
	return firstErr
}

// ------------------------------------------------------------------
// 服务提供与协效应解析（论文 provide / inject / Service）
// ------------------------------------------------------------------

// Provide 把 value 提供到当前 context 解析到的 realm 中，
// 并自动注册撤销效应：context 回卷时条目被移除。
// 返回的 Inverse 可提前手动撤销（与自动撤销幂等互斥）。
// 后提供者可见（last-provided-wins）；条目移除即回落到其余提供者。
func (c *Context) Provide(key Key, value any) (Inverse, error) {
	if key.typ != nil {
		rt := reflect.TypeOf(value)
		if rt == nil || !rt.AssignableTo(key.typ) {
			return nil, fmt.Errorf("stc: provide %q: %T not assignable to %s", key.name, value, key.typ)
		}
	}
	c.sh.mu.Lock()
	if c.closed || c.unwinding {
		c.sh.mu.Unlock()
		return nil, ErrInactive
	}
	r := c.realmForLocked(key)
	pk := provKey{realm: r, key: key}
	// 良构性强制：同一键已有别的 fiber 提供者时拒绝（防止双提供者
	// 互相触发依赖代际变化而无限抖动）。根/手动 context 的提供不在此列。
	for _, e := range c.sh.provides[pk] {
		if e.owner.fiber != nil && e.owner.fiber != c.fiber {
			c.sh.mu.Unlock()
			return nil, fmt.Errorf("%w: %q already provided by fiber %d",
				ErrDuplicateProvide, key.name, e.owner.fiber.id)
		}
	}
	c.sh.seq++
	entry := provideEntry{owner: c, seq: c.sh.seq, value: value}
	c.sh.provides[pk] = append(c.sh.provides[pk], entry)
	c.sh.traceLocked(TraceProvide, entry.owner.fiberID(), key.name)
	inv := func() error {
		c.removeProvide(pk, entry)
		return nil
	}
	c.inverses = append(c.inverses, inv)
	c.sh.mu.Unlock()

	c.sh.wakeServiceWatchers()
	c.sh.notifyOrch()
	return inv, nil
}

func (c *Context) removeProvide(pk provKey, entry provideEntry) {
	c.sh.mu.Lock()
	st := c.sh.provides[pk]
	removed := false
	for i, x := range st {
		if x.same(entry) {
			st = append(st[:i], st[i+1:]...)
			removed = true
			break
		}
	}
	if !removed {
		// 重复撤销（手动 Inverse 与自动回卷各调一次）：完全幂等，
		// 不动状态、不发通知、不记轨迹。
		c.sh.mu.Unlock()
		return
	}
	if len(st) == 0 {
		delete(c.sh.provides, pk)
	} else {
		c.sh.provides[pk] = st
	}
	c.sh.traceLocked(TraceUnprovide, entry.owner.fiberID(), pk.key.name)
	c.sh.mu.Unlock()

	c.sh.wakeServiceWatchers()
	c.sh.notifyOrch()
}

// resolve 在 context 的 realm 链上解析服务的当前可见值。
func (c *Context) resolve(key Key) (any, bool) {
	c.sh.mu.RLock()
	defer c.sh.mu.RUnlock()
	for r := c.realmForLocked(key); r != nil; r = r.parent {
		if st := c.sh.provides[provKey{realm: r, key: key}]; len(st) > 0 {
			return st[len(st)-1].value, true
		}
	}
	return nil, false
}

func (c *Context) realmForLocked(key Key) *Realm {
	for x := c; x != nil; x = x.parent {
		if r, ok := x.realmFor[key]; ok {
			return r
		}
	}
	return rootRealm
}

// Service 做类型化协效应读取（Go 方法不允许类型参数，故为包级泛型函数）。
func Service[T any](c *Context, key Key) (T, error) {
	v, ok := c.resolve(key)
	if !ok {
		var zero T
		return zero, fmt.Errorf("stc: service %q not provided", key.name)
	}
	t, ok := v.(T)
	if !ok {
		var zero T
		return zero, fmt.Errorf("stc: service %q: %T is not %T", key.name, v, zero)
	}
	return t, nil
}

// WaitService 阻塞直到 key 在此 context 可解析或 ctx 结束。
// 协效应侧的"响应式等待"原语。
func (c *Context) WaitService(ctx stdctx.Context, key Key) (any, error) {
	for {
		c.sh.mu.RLock()
		ch := c.sh.svcCh // 先订阅，后解析（防丢失唤醒）
		c.sh.mu.RUnlock()
		if v, ok := c.resolve(key); ok {
			return v, nil
		}
		select {
		case <-ch:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
}

func (sh *shared) wakeServiceWatchers() {
	sh.mu.Lock()
	close(sh.svcCh)
	sh.svcCh = make(chan struct{})
	sh.mu.Unlock()
}

// notifyOrch 通知 orchestrator 服务格局变化、需要重新评估 fiber。
// M2 之前为空操作。
func (sh *shared) notifyOrch() {
	if sh.orch != nil {
		sh.orch.notifyService()
	}
}

// ------------------------------------------------------------------
// 隔离与拦截（论文 isolate(k, r) / intercept(k, ν)）
// ------------------------------------------------------------------

// Isolate 声明：在此 context 子树内，key 解析到 realm r 而非外层 realm。
// 撤销效应会在回卷时移除该声明。
func (c *Context) Isolate(key Key, r *Realm) error {
	if r == nil {
		r = rootRealm
	}
	c.sh.mu.Lock()
	if c.closed || c.unwinding {
		c.sh.mu.Unlock()
		return ErrInactive
	}
	if c.realmFor == nil {
		c.realmFor = map[Key]*Realm{}
	}
	c.realmFor[key] = r
	c.inverses = append(c.inverses, func() error {
		c.sh.mu.Lock()
		delete(c.realmFor, key)
		c.sh.mu.Unlock()
		return nil
	})
	c.sh.mu.Unlock()
	c.sh.wakeServiceWatchers()
	c.sh.notifyOrch()
	return nil
}

// Intercept 在此 context 上为 key 附加拦截元数据（策略、适配器等）。
func (c *Context) Intercept(key Key, meta any) error {
	c.sh.mu.Lock()
	defer c.sh.mu.Unlock()
	if c.closed || c.unwinding {
		return ErrInactive
	}
	if c.interc == nil {
		c.interc = make(map[Key][]interceptEntry)
	}
	c.sh.seq++
	ie := interceptEntry{id: c.sh.seq, meta: meta}
	c.interc[key] = append(c.interc[key], ie)
	c.inverses = append(c.inverses, func() error {
		c.sh.mu.Lock()
		lst := c.interc[key]
		for i, x := range lst {
			if x.id == ie.id {
				lst = append(lst[:i], lst[i+1:]...)
				break
			}
		}
		c.interc[key] = lst
		c.sh.mu.Unlock()
		return nil
	})
	return nil
}

// Interceptors 沿树自内向外收集 key 的拦截元数据（内层优先）。
func Interceptors(c *Context, key Key) []any {
	c.sh.mu.RLock()
	defer c.sh.mu.RUnlock()
	var out []any
	for x := c; x != nil; x = x.parent {
		for _, ie := range x.interc[key] {
			out = append(out, ie.meta)
		}
	}
	return out
}

// ------------------------------------------------------------------
// 最小事件机制（D4）：监听器是可逆效应，回卷自动撤销。
// ------------------------------------------------------------------

func (c *Context) On(name string, fn func(args ...any)) error {
	c.sh.mu.Lock()
	defer c.sh.mu.Unlock()
	if c.closed || c.unwinding {
		return ErrInactive
	}
	c.sh.seq++
	le := listenerEntry{id: c.sh.seq, owner: c, fn: fn}
	c.sh.listeners[name] = append(c.sh.listeners[name], le)
	c.inverses = append(c.inverses, func() error {
		c.sh.mu.Lock()
		lst := c.sh.listeners[name]
		for i, x := range lst {
			if x.id == le.id {
				lst = append(lst[:i], lst[i+1:]...)
				break
			}
		}
		c.sh.listeners[name] = lst
		c.sh.mu.Unlock()
		return nil
	})
	return nil
}

func (c *Context) Emit(name string, args ...any) {
	c.sh.mu.RLock()
	lst := make([]listenerEntry, len(c.sh.listeners[name]))
	copy(lst, c.sh.listeners[name])
	c.sh.mu.RUnlock()
	for _, l := range lst {
		l.fn(args...)
	}
}

// ------------------------------------------------------------------
// 生命周期
// ------------------------------------------------------------------

// Close 仅限根 context：终结全部 fiber、停止 orchestrator，并回卷整棵树。
// 对非根 context 调用返回 ErrNotRoot——orchestrator 是整树共享的单例，
// 若允许任意作用域关停全局，会静默撤退无关 fiber 并使整树僵尸化。
// 非根作用域的清理用 Release（仅回卷子树，不动系统）。
func (c *Context) Close() error {
	if c.parent != nil {
		return ErrNotRoot
	}
	c.sh.orch.shutdown()
	return c.unwind()
}

// Release 回卷该 context 的子树（不停止 orchestrator）。
func (c *Context) Release() error { return c.unwind() }

// ------------------------------------------------------------------
// 追踪（验收测试断言用）
// ------------------------------------------------------------------

// fiberID 返回拥有此 context 的 fiber 编号（根/手动 context 为 0）。
func (c *Context) fiberID() uint64 {
	if c.fiber != nil {
		return c.fiber.id
	}
	return 0
}

// traceUser 从任意 goroutine 记录事件。
func (sh *shared) traceUser(kind TraceKind, fiber uint64) {
	sh.mu.Lock()
	sh.traceLocked(kind, fiber, "")
	sh.mu.Unlock()
}

// traceCap 是轨迹的最大保留条数：轨迹服务于验收测试与调试，
// 不允许在生产负载下无界增长；超出后丢弃最旧事件（Seq 仍单调）。
const traceCap = 8192

func (sh *shared) traceLocked(kind TraceKind, fiber uint64, key string) {
	sh.seq++
	if len(sh.trace) >= traceCap {
		copy(sh.trace, sh.trace[1:])
		sh.trace = sh.trace[:traceCap-1]
	}
	sh.trace = append(sh.trace, TraceEvent{Seq: sh.seq, Kind: kind, Fiber: fiber, Key: key})
}

// Trace 返回事件轨迹的拷贝。轨迹有界（最近 traceCap 条），
// 供验收测试与调试使用；Seq 全局单调，可用于判定缺口。
func (c *Context) Trace() []TraceEvent {
	c.sh.mu.RLock()
	defer c.sh.mu.RUnlock()
	out := make([]TraceEvent, len(c.sh.trace))
	copy(out, c.sh.trace)
	return out
}
