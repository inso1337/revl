// Package registry 提供"稳定注册表 + 可逆注册效应"模式：一个 fiber
// 提供键身份永不变的注册表（Inject 为空 → 不随任何级联重载），成员
// fiber inject 注册表并以可逆效应登记自己（逆 = 注销）。消费方按用
// 读取当前视图，成员增删从不引发消费方重载——stc-agent 的对话中途
// 热替换工具而 agent 循环不感知，靠的就是这个模式。
//
// 模式在 stc-agent 中两处独立出现（工具注册表与斜杠命令注册表），
// 经回流评审提取为卫星包（stc-go#2）。
package registry

import (
	"sort"
	"sync"

	stc "github.com/0xdenny218/stc-go"
)

// Registry 是并发安全的 名字→值 注册表。
type Registry[T any] struct {
	mu  sync.RWMutex
	m   map[string]entry[T]
	seq uint64
}

// entry 带注册代际：逆只注销自己那一代，同名覆盖后旧逆不误删新值。
type entry[T any] struct {
	v   T
	seq uint64
}

// New 创建空注册表。
func New[T any]() *Registry[T] {
	return &Registry[T]{m: make(map[string]entry[T])}
}

// Register 以 name 登记 v，返回注销该次登记的逆（幂等）。
// 同名再登记覆盖旧值；被覆盖的旧逆失效——调用它不会删除新值。
func (r *Registry[T]) Register(name string, v T) stc.Inverse {
	r.mu.Lock()
	r.seq++
	r.m[name] = entry[T]{v: v, seq: r.seq}
	seq := r.seq
	r.mu.Unlock()
	return func() error {
		r.mu.Lock()
		if e, ok := r.m[name]; ok && e.seq == seq {
			delete(r.m, name)
		}
		r.mu.Unlock()
		return nil
	}
}

// Lookup 按名取值；未登记返回 (零值, false)。
func (r *Registry[T]) Lookup(name string) (T, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	e, ok := r.m[name]
	return e.v, ok
}

// List 返回按名排序的当前值视图。
func (r *Registry[T]) List() []T {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make([]T, 0, len(r.m))
	for _, name := range r.sortedNamesLocked() {
		out = append(out, r.m[name].v)
	}
	return out
}

// Names 返回排序后的当前名字视图。
func (r *Registry[T]) Names() []string {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.sortedNamesLocked()
}

func (r *Registry[T]) sortedNamesLocked() []string {
	names := make([]string, 0, len(r.m))
	for name := range r.m {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

// Component 返回以 key 提供稳定注册表的组件。Inject 为空：键身份
// 不变，inject 注册表的消费方 fiber 不因成员增删而重载。
func Component[T any](name string, key stc.Key) stc.Component {
	return stc.Component{
		Name:    name,
		Provide: []stc.Key{key},
		Apply: func(c *stc.Context) (stc.Inverse, error) {
			_, err := c.Provide(key, New[T]())
			return nil, err
		},
	}
}
