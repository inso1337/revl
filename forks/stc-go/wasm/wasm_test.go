package wasm

// M4 验收：Cordis HMR 契约（重载、依赖链、失败回滚）按 wazero 机制改写，
// 外加规格要求的 Test/WasmRollback 与 T61 跨边界卸载精确性。

import (
	stdctx "context"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/0xdenny218/stc-go"
)

// recorder 是注入 key 并记录每次装载观察值的 Go 消费者组件。
type recorder struct {
	mu   sync.Mutex
	seen []string
}

func (r *recorder) component(key stc.Key) stc.Component {
	return stc.Component{
		Name:   "recorder",
		Inject: []stc.Key{key},
		Apply: func(c *stc.Context) (stc.Inverse, error) {
			v, err := stc.Service[string](c, key)
			if err != nil {
				return nil, err
			}
			r.mu.Lock()
			r.seen = append(r.seen, v)
			r.mu.Unlock()
			return nil, nil
		},
	}
}

func (r *recorder) snapshot() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.seen...)
}

// waitSeen 等待 recorder 的最后一个观察值变为 want。
func waitSeen(t *testing.T, r *recorder, want string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		seen := r.snapshot()
		if len(seen) > 0 && seen[len(seen)-1] == want {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("recorder never saw %q; seen=%v", want, r.snapshot())
}

func setup(t *testing.T) (*stc.Context, *Runtime) {
	t.Helper()
	rt, err := NewRuntime()
	if err != nil {
		t.Fatal(err)
	}
	root := stc.New()
	t.Cleanup(func() {
		_ = root.Close()
		_ = rt.Close()
	})
	return root, rt
}

func bg() stdctx.Context { return stdctx.Background() }

// HMR 契约一：重载——Update 后消费者经依赖代际机制重载并观察到新值，
// 旧模块的 stop 被调用、实例被关闭。
func TestWasmReload(t *testing.T) {
	root, rt := setup(t)
	greeting := rt.Key("greeting")

	rec := &recorder{}
	cf := root.Load(rec.component(greeting))

	h, err := Load(bg(), root, rt, helloGuest("hello wasm"), Options{
		Name: "hello", Provide: []string{"greeting"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := cf.Ready(bg()); err != nil {
		t.Fatal(err)
	}
	waitSeen(t, rec, "hello wasm")

	if err := h.Update(bg(), helloGuest("hi wasm")); err != nil {
		t.Fatal(err)
	}
	waitSeen(t, rec, "hi wasm")

	if got := rec.snapshot(); strings.Join(got, ",") != "hello wasm,hi wasm" {
		t.Fatalf("consumer reload trace = %v, want exactly [hello wasm, hi wasm]", got)
	}
	var stopLogged bool
	for _, l := range rt.Logs() {
		if l == "hello stopped" {
			stopLogged = true
		}
	}
	if !stopLogged {
		t.Fatalf("old module stop not called; logs=%v", rt.Logs())
	}
}

// HMR 契约二：依赖链跨边界——Go 提供 greeting → WASM reader 转供 echo
// → Go 消费者注入 echo；源头换血后链式重载按序发生。
func TestWasmDependencyChain(t *testing.T) {
	root, rt := setup(t)
	greeting := rt.Key("greeting")
	echo := rt.Key("echo")

	inv1, err := root.Provide(greeting, "hello")
	if err != nil {
		t.Fatal(err)
	}
	reader, err := Load(bg(), root, rt, readerGuest(), Options{
		Name: "reader", Inject: []string{"greeting"}, Provide: []string{"echo"},
	})
	if err != nil {
		t.Fatal(err)
	}
	rec := &recorder{}
	cf := root.Load(rec.component(echo))
	if err := cf.Ready(bg()); err != nil {
		t.Fatal(err)
	}
	waitSeen(t, rec, "hello")

	// 源头换血：reader 因代际变化重载，echo 换血，消费者随之重载。
	if err := inv1(); err != nil {
		t.Fatal(err)
	}
	if _, err := root.Provide(greeting, "bye"); err != nil {
		t.Fatal(err)
	}
	waitSeen(t, rec, "bye")
	if got := rec.snapshot(); strings.Join(got, ",") != "hello,bye" {
		t.Fatalf("chain reload trace = %v, want exactly [hello, bye]", got)
	}
	_ = reader
}

// HMR 契约三 + 规格 Test/WasmRollback：新版本实例化失败，
// 旧版本及其全部副作用保持有效（fiber 未动、消费者未重载）。
func TestWasmRollbackProbeFailure(t *testing.T) {
	root, rt := setup(t)
	greeting := rt.Key("greeting")

	h, err := Load(bg(), root, rt, helloGuest("hello wasm"), Options{
		Name: "hello", Provide: []string{"greeting"},
	})
	if err != nil {
		t.Fatal(err)
	}
	rec := &recorder{}
	cf := root.Load(rec.component(greeting))
	if err := cf.Ready(bg()); err != nil {
		t.Fatal(err)
	}
	waitSeen(t, rec, "hello wasm")
	oldFiber := h.Fiber()

	err = h.Update(bg(), badGuest())
	if err == nil || !strings.Contains(err.Error(), "old version intact") {
		t.Fatalf("Update = %v, want probe failure keeping old version", err)
	}
	if h.Fiber() != oldFiber {
		t.Fatal("fiber replaced despite failed probe")
	}
	if st := oldFiber.State(); st != stc.StateActive {
		t.Fatalf("old fiber state = %v, want Active", st)
	}
	if v, err := stc.Service[string](root, greeting); err != nil || v != "hello wasm" {
		t.Fatalf("old service lost: v=%q err=%v", v, err)
	}
	if got := rec.snapshot(); len(got) != 1 {
		t.Fatalf("consumer reloaded on failed update: %v", got)
	}
}

// start 期失败（trap）：探针通过但 start 失败 → 用旧字节回滚，
// 服务恢复原值，消费者观察到一次完整的卸载-重载。
func TestWasmRollbackTrap(t *testing.T) {
	root, rt := setup(t)
	greeting := rt.Key("greeting")

	h, err := Load(bg(), root, rt, helloGuest("hello wasm"), Options{
		Name: "hello", Provide: []string{"greeting"},
	})
	if err != nil {
		t.Fatal(err)
	}
	rec := &recorder{}
	cf := root.Load(rec.component(greeting))
	if err := cf.Ready(bg()); err != nil {
		t.Fatal(err)
	}
	waitSeen(t, rec, "hello wasm")

	err = h.Update(bg(), trapGuest())
	if err == nil || !strings.Contains(err.Error(), "rolled back") {
		t.Fatalf("Update = %v, want trap with rollback", err)
	}
	// 回滚纤维服役，服务恢复原值。
	if st := h.Fiber().State(); st != stc.StateActive {
		t.Fatalf("rollback fiber state = %v, want Active", st)
	}
	waitSeen(t, rec, "hello wasm")
	if v, err := stc.Service[string](root, greeting); err != nil || v != "hello wasm" {
		t.Fatalf("rollback did not restore service: v=%q err=%v", v, err)
	}
}

// T61 跨边界：Dispose 后 provide 被移除、stop 被调用、模块实例关闭。
func TestWasmUnloadExactness(t *testing.T) {
	root, rt := setup(t)
	greeting := rt.Key("greeting")

	h, err := Load(bg(), root, rt, helloGuest("hello wasm"), Options{
		Name: "hello", Provide: []string{"greeting"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := stc.Service[string](root, greeting); err != nil {
		t.Fatal(err)
	}

	h.Dispose()
	if err := h.Fiber().Gone(bg()); err != nil {
		t.Fatal(err)
	}
	if _, err := stc.Service[string](root, greeting); err == nil {
		t.Fatal("greeting still resolvable after dispose")
	}
	var stopLogged bool
	for _, l := range rt.Logs() {
		if l == "hello stopped" {
			stopLogged = true
		}
	}
	if !stopLogged {
		t.Fatalf("stop not called on dispose; logs=%v", rt.Logs())
	}
}

// 依赖未满足时 WASM fiber 停在 Pending（与 Go 组件同一门控语义）。
func TestWasmInjectGating(t *testing.T) {
	root, rt := setup(t)
	greeting := rt.Key("greeting")

	f := root.Load(rt.Component(readerGuest(), Options{
		Name: "reader", Inject: []string{"greeting"}, Provide: []string{"echo"},
	}))
	ctx, cancel := stdctx.WithTimeout(stdctx.Background(), 200*time.Millisecond)
	defer cancel()
	if err := f.Ready(ctx); err == nil {
		t.Fatal("reader became ready without greeting")
	}
	if st := f.State(); st != stc.StatePending {
		t.Fatalf("state = %v, want Pending", st)
	}
	if _, err := root.Provide(greeting, "late"); err != nil {
		t.Fatal(err)
	}
	if err := f.Ready(bg()); err != nil {
		t.Fatal(err)
	}
	if v, err := stc.Service[string](root, rt.Key("echo")); err != nil || v != "late" {
		t.Fatalf("echo = %q, %v; want late", v, err)
	}
}

// 回归（e2e 发现）：TinyGo 等工具链产物的模块名段固定（恒为 "main"），
// Update 的探针/新实例与现役旧实例撞名，被 wazero 拒绝。
// 唯一实例名派生后，同名模块的原子换血必须成功。
func TestWasmUpdateSameModuleName(t *testing.T) {
	root, rt := setup(t)
	greeting := rt.Key("greeting")

	rec := &recorder{}
	cf := root.Load(rec.component(greeting))

	h, err := Load(bg(), root, rt, withModuleName(helloGuest("v1"), "main"), Options{
		Name: "hello", Provide: []string{"greeting"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := cf.Ready(bg()); err != nil {
		t.Fatal(err)
	}
	waitSeen(t, rec, "v1")

	if err := h.Update(bg(), withModuleName(helloGuest("v2"), "main")); err != nil {
		t.Fatalf("update same-named module: %v", err)
	}
	waitSeen(t, rec, "v2")
}
