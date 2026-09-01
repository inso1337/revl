package stc

import (
	stdctx "context"
	"testing"
	"time"
)

// M3 验收：隔离 realm——同名服务在不同 realm 互不可见，realm 撤销后回落外层。
func TestIsolateRealm(t *testing.T) {
	root := New()
	defer root.Close()
	k := UntypedKey("svc")

	// 外层提供者（根域）。
	outer, err := root.Provide(k, "outer")
	if err != nil {
		t.Fatal(err)
	}
	_ = outer

	// 隔离区：在 r 内解析 k 走独立栈。
	zone := root.Child()
	r := NewRealm(RootRealm(), "zone")
	if err := zone.Isolate(k, r); err != nil {
		t.Fatal(err)
	}

	inZone, err := zone.Provide(k, "inner")
	if err != nil {
		t.Fatal(err)
	}

	if v, _ := zone.resolve(k); v != "inner" {
		t.Fatalf("zone sees %v, want inner", v)
	}
	if v, _ := root.resolve(k); v != "outer" {
		t.Fatalf("root sees %v, want outer（隔离不外溢）", v)
	}

	// zone 子作用域继承隔离声明。
	if v, _ := zone.Child().resolve(k); v != "inner" {
		t.Fatalf("zone child sees %v, want inner", v)
	}

	// realm 提供者撤退 → 回落外层提供。
	if err := inZone(); err != nil {
		t.Fatal(err)
	}
	if v, _ := zone.resolve(k); v != "outer" {
		t.Fatalf("after zone withdrawal sees %v, want outer（realm 链回落）", v)
	}
}

// 不同 realm 的同名服务由不同 fiber 分别提供，消费者各取所需。
func TestIsolateRealmFibers(t *testing.T) {
	root := New()
	defer root.Close()
	k := NewKey[string]("db")

	mkProvider := func(name, value string) Component {
		return Component{
			Name: name,
			Apply: func(ctx *Context) (Inverse, error) {
				_, err := ctx.Provide(k, value)
				return nil, err
			},
		}
	}

	// 默认域提供者。
	d1 := root.Load(mkProvider("default-db", "postgres"))
	// 隔离域：zone 下装载的提供者进 r，不影响默认域。
	zone := root.Child()
	r := NewRealm(RootRealm(), "test")
	if err := zone.Isolate(k, r); err != nil {
		t.Fatal(err)
	}

	// 注意：fiber 的提供落在装载目标 context 解析出的 realm。
	var err error
	var inZone Inverse
	prov := zone.Load(mkProvider("test-db", "sqlite"))
	g := stdctx.Background()
	if err = prov.Ready(g); err != nil {
		t.Fatal(err)
	}
	if err = d1.Ready(g); err != nil {
		t.Fatal(err)
	}
	_ = inZone

	// zone 内消费者看到 sqlite，根消费者看到 postgres。
	if v, err := Service[string](zone, k); err != nil || v != "sqlite" {
		t.Fatalf("zone service = %v, %v", v, err)
	}
	if v, err := Service[string](root, k); err != nil || v != "postgres" {
		t.Fatalf("root service = %v, %v", v, err)
	}

	// 测试域撤退 → zone 回落到 postgres。
	prov.Dispose()
	if err := prov.Gone(g); err != nil {
		t.Fatal(err)
	}
	if v, err := Service[string](zone, k); err != nil || v != "postgres" {
		t.Fatalf("zone after withdrawal = %v, %v, want postgres", v, err)
	}
}

func TestInterceptors(t *testing.T) {
	root := New()
	defer root.Close()
	k := UntypedKey("op")

	mustOK(t, root.Intercept(k, "root-policy"))
	child := root.Child()
	mustOK(t, child.Intercept(k, "child-policy"))

	got := Interceptors(child, k)
	if len(got) != 2 {
		t.Fatalf("interceptors = %v, want 2", got)
	}
	// 内层优先。
	if got[0] != "child-policy" || got[1] != "root-policy" {
		t.Fatalf("order = %v, want child first", got)
	}
}

// 静默辅助：等待全部 fiber 到达稳定态（Active/Pending/Failed/Gone）且无在飞转移。
func waitQuiet(t *testing.T, root *Context, fibers []*Fiber, within time.Duration) {
	t.Helper()
	deadline := time.Now().Add(within)
	for time.Now().Before(deadline) {
		// inflight>0 说明仍有已提交未处理完毕的命令（如尚未注册的
		// Load、正在进行的 settle），不算静默。inbox 长度不可用：
		// 取出即清空，但处理未完。
		stable := root.sh.pending.Load() == 0 && root.sh.orch.inflight.Load() == 0
		for _, f := range fibers {
			switch f.State() {
			case StateActive, StatePending, StateFailed, StateGone:
			default:
				stable = false
			}
		}
		if stable {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatalf("system not quiescent: %v", fiberStates(fibers))
}

func fiberStates(fs []*Fiber) []string {
	out := make([]string, len(fs))
	for i, f := range fs {
		out[i] = f.State().String()
	}
	return out
}
