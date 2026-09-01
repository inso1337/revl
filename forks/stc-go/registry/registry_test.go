package registry_test

import (
	stdctx "context"
	"fmt"
	"slices"
	"sync"
	"testing"
	"time"

	stc "github.com/0xdenny218/stc-go"
	"github.com/0xdenny218/stc-go/registry"
)

func TestRegisterLookupListNames(t *testing.T) {
	r := registry.New[int]()
	if got := r.List(); len(got) != 0 {
		t.Fatalf("empty registry: List = %v", got)
	}
	if _, ok := r.Lookup("a"); ok {
		t.Fatal("Lookup on empty registry reports hit")
	}

	r.Register("b", 2)
	r.Register("a", 1)

	// List 按名排序，不是按值。
	if got := r.List(); !slices.Equal(got, []int{1, 2}) {
		t.Fatalf("List = %v, want [1 2]", got)
	}
	if got := r.Names(); !slices.Equal(got, []string{"a", "b"}) {
		t.Fatalf("Names = %v, want [a b]", got)
	}
	if v, ok := r.Lookup("b"); !ok || v != 2 {
		t.Fatalf("Lookup(b) = %v, %v; want 2, true", v, ok)
	}
}

func TestUnregisterInverseIdempotent(t *testing.T) {
	r := registry.New[string]()
	inv := r.Register("x", "ex")
	if err := inv(); err != nil {
		t.Fatalf("inverse: %v", err)
	}
	if _, ok := r.Lookup("x"); ok {
		t.Fatal("entry survives its inverse")
	}
	if err := inv(); err != nil {
		t.Fatalf("inverse not idempotent: %v", err)
	}
}

// 同名覆盖登记后，被覆盖的旧逆不得误删新值（ABA 防护）。
func TestReregisterSupersededInverse(t *testing.T) {
	r := registry.New[string]()
	inv1 := r.Register("k", "v1")
	inv2 := r.Register("k", "v2")

	if err := inv1(); err != nil {
		t.Fatalf("superseded inverse: %v", err)
	}
	if v, ok := r.Lookup("k"); !ok || v != "v2" {
		t.Fatalf("superseded inverse clobbered new value: %v, %v", v, ok)
	}
	if err := inv2(); err != nil {
		t.Fatalf("inverse: %v", err)
	}
	if _, ok := r.Lookup("k"); ok {
		t.Fatal("entry survives its own inverse")
	}
}

// 范式性质：注册表键身份稳定，成员增删（含成员 fiber 卸载引发的注销）
// 不重载 inject 注册表的消费方 fiber——消费方的装载周期 context 指针
// 全程不变。
func TestComponentStableAcrossMemberChurn(t *testing.T) {
	root := stc.New()
	defer root.Close()
	ctx, cancel := stdctx.WithTimeout(stdctx.Background(), 5*time.Second)
	defer cancel()

	key := stc.NewKey[*registry.Registry[string]]("regs")
	if err := root.Load(registry.Component[string]("regs", key)).Ready(ctx); err != nil {
		t.Fatalf("registry component: %v", err)
	}

	var (
		mu      sync.Mutex
		applies int
	)
	consumer := root.Load(stc.Component{
		Name:   "consumer",
		Inject: []stc.Key{key},
		Apply: func(c *stc.Context) (stc.Inverse, error) {
			if _, err := stc.Service[*registry.Registry[string]](c, key); err != nil {
				return nil, err
			}
			mu.Lock()
			applies++
			mu.Unlock()
			return nil, nil
		},
	})
	if err := consumer.Ready(ctx); err != nil {
		t.Fatalf("consumer: %v", err)
	}
	cycle := consumer.Context()

	// 成员 fiber 以可逆效应登记自己（stc-agent 工具 fiber 的同款形态）。
	member := root.Load(stc.Component{
		Name:   "member",
		Inject: []stc.Key{key},
		Apply: func(c *stc.Context) (stc.Inverse, error) {
			r, err := stc.Service[*registry.Registry[string]](c, key)
			if err != nil {
				return nil, err
			}
			return r.Register("x", "ex"), nil
		},
	})
	if err := member.Ready(ctx); err != nil {
		t.Fatalf("member: %v", err)
	}

	regs, err := stc.Service[*registry.Registry[string]](root, key)
	if err != nil {
		t.Fatalf("lookup registry: %v", err)
	}
	if got := regs.Names(); !slices.Equal(got, []string{"x"}) {
		t.Fatalf("Names after member load = %v, want [x]", got)
	}

	member.Dispose()
	if err := member.Gone(ctx); err != nil {
		t.Fatalf("member gone: %v", err)
	}
	if got := regs.Names(); len(got) != 0 {
		t.Fatalf("Names after member unload = %v, want empty", got)
	}

	mu.Lock()
	defer mu.Unlock()
	if applies != 1 {
		t.Fatalf("consumer re-applied %d times across member churn, want 1", applies)
	}
	if consumer.Context() != cycle {
		t.Fatal("consumer load cycle replaced across member churn")
	}
}

// 并发登记/注销/读取：-race 下验证锁纪律。
func TestConcurrentChurn(t *testing.T) {
	r := registry.New[int]()
	var wg sync.WaitGroup
	for w := 0; w < 8; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			name := fmt.Sprintf("k%d", w)
			for i := 0; i < 50; i++ {
				inv := r.Register(name, i)
				_ = r.List()
				_ = r.Names()
				_, _ = r.Lookup(name)
				_ = inv()
			}
		}(w)
	}
	wg.Wait()
	if got := r.Names(); len(got) != 0 {
		t.Fatalf("final Names = %v, want empty", got)
	}
}
