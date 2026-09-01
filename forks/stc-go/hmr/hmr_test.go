package hmr

// 验收：文件变化 → 防抖 → 原子 Update 的三条路径
// （成功换血、连续写入合并、坏字节保留旧版）。
// guest 为内联手写最小模块，与 stc-go/wasm 的测试编码器同法。

import (
	stdctx "context"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	stc "github.com/0xdenny218/stc-go"
	"github.com/0xdenny218/stc-go/wasm"
)

// noOpGuest 是手写最小合法模块：导出 memory 与空 start，无任何导入。
// Update 成功与否不靠行为差异观察，而靠 Fiber() 身份变化（dispose+load）。
var noOpGuest = []byte{
	0x00, 0x61, 0x73, 0x6d, 0x01, 0x00, 0x00, 0x00, // magic + version
	0x01, 0x04, 0x01, 0x60, 0x00, 0x00, // type section: () -> ()
	0x03, 0x02, 0x01, 0x00, // func section: func 0 has type 0
	0x05, 0x03, 0x01, 0x00, 0x01, // memory section: 1 page
	0x07, 0x12, 0x02, // export section: 2 entries
	0x06, 'm', 'e', 'm', 'o', 'r', 'y', 0x02, 0x00, // "memory" -> memory 0
	0x05, 's', 't', 'a', 'r', 't', 0x00, 0x00, // "start" -> func 0
	0x0a, 0x04, 0x01, 0x02, 0x00, 0x0b, // code section: empty body
}

// recorder 收集 OnReload 结果。
type recorder struct {
	mu   sync.Mutex
	errs []error
}

func (r *recorder) cb() func(error) {
	return func(err error) {
		r.mu.Lock()
		r.errs = append(r.errs, err)
		r.mu.Unlock()
	}
}

func (r *recorder) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.errs)
}

func (r *recorder) last() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.errs) == 0 {
		return nil
	}
	return r.errs[len(r.errs)-1]
}

func setup(t *testing.T) (*stc.Context, *wasm.Runtime, *wasm.Handle, string) {
	t.Helper()
	rt, err := wasm.NewRuntime()
	if err != nil {
		t.Fatal(err)
	}
	root := stc.New()
	t.Cleanup(func() {
		_ = root.Close()
		_ = rt.Close()
	})
	path := filepath.Join(t.TempDir(), "guest.wasm")
	if err := os.WriteFile(path, noOpGuest, 0o644); err != nil {
		t.Fatal(err)
	}
	h, err := wasm.Load(stdctx.Background(), root, rt, noOpGuest, wasm.Options{Name: "guest"})
	if err != nil {
		t.Fatal(err)
	}
	return root, rt, h, path
}

func write(t *testing.T, path string, src []byte) {
	t.Helper()
	if err := os.WriteFile(path, src, 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestWatchReload(t *testing.T) {
	_, _, h, path := setup(t)
	rec := &recorder{}
	w, err := Watch(stdctx.Background(), h, path, &Options{
		Debounce: 20 * time.Millisecond, OnReload: rec.cb(),
	})
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()

	old := h.Fiber()
	write(t, path, noOpGuest)

	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if h.Fiber() != old {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	if h.Fiber() == old {
		t.Fatal("fiber not swapped after file change")
	}
	if rec.count() != 1 || rec.last() != nil {
		t.Fatalf("OnReload: count=%d last=%v, want 1 nil", rec.count(), rec.last())
	}
}

func TestWatchDebounce(t *testing.T) {
	_, _, h, path := setup(t)
	rec := &recorder{}
	w, err := Watch(stdctx.Background(), h, path, &Options{
		Debounce: 100 * time.Millisecond, OnReload: rec.cb(),
	})
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()

	for i := 0; i < 5; i++ {
		write(t, path, noOpGuest)
		time.Sleep(10 * time.Millisecond)
	}
	// 一串写入应合并为一次重载；给足防抖 + Update 的落定时间。
	time.Sleep(500 * time.Millisecond)
	if got := rec.count(); got != 1 {
		t.Fatalf("reload count = %d, want 1 (debounced)", got)
	}
	if rec.last() != nil {
		t.Fatalf("last reload err = %v", rec.last())
	}
}

func TestWatchBadBytesKeepsOld(t *testing.T) {
	_, _, h, path := setup(t)
	rec := &recorder{}
	w, err := Watch(stdctx.Background(), h, path, &Options{
		Debounce: 20 * time.Millisecond, OnReload: rec.cb(),
	})
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()

	old := h.Fiber()
	write(t, path, []byte("not a wasm module"))

	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) && rec.count() == 0 {
		time.Sleep(5 * time.Millisecond)
	}
	if rec.count() != 1 {
		t.Fatalf("OnReload count = %d, want 1", rec.count())
	}
	if rec.last() == nil {
		t.Fatal("expected probe failure for garbage bytes")
	}
	if h.Fiber() != old {
		t.Fatal("fiber swapped despite failed probe")
	}
	if st := old.State(); st != stc.StateActive {
		t.Fatalf("old fiber state = %v, want Active", st)
	}
}
