package watch_test

// 验收：防抖监听原语的契约（手法对齐 hmr_test.go）：单文件写入触发、
// 连发合并、原子保存 rename 形态、删除回调、目录监听、Close 与 ctx
// 取消的幂等停止、构造期参数校验。

import (
	stdctx "context"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/0xdenny218/stc-go/watch"
)

// recorder 收集 fire 事件（OnFire 在监听 goroutine 上回调）。
type recorder struct {
	mu  sync.Mutex
	evs []watch.Event
}

func (r *recorder) cb() func(watch.Event) {
	return func(ev watch.Event) {
		r.mu.Lock()
		r.evs = append(r.evs, ev)
		r.mu.Unlock()
	}
}

func (r *recorder) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.evs)
}

func (r *recorder) last() watch.Event {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.evs) == 0 {
		return watch.Event{}
	}
	return r.evs[len(r.evs)-1]
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("timeout waiting for %s", what)
}

func write(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

// tempDir 取真实路径（macOS 的 /var 是 /private/var 的符号链接，
// fsnotify 的事件路径按真实路径拼，先对齐避免断言路径不稳）。
func tempDir(t *testing.T) string {
	t.Helper()
	dir, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	return dir
}

// newFile 建一个被监听文件，返回其绝对路径。
func newFile(t *testing.T) string {
	t.Helper()
	path := filepath.Join(tempDir(t), "target.txt")
	write(t, path, "v0")
	return path
}

func TestFileFireOnWrite(t *testing.T) {
	path := newFile(t)
	rec := &recorder{}
	w, err := watch.Watch(stdctx.Background(), path, watch.Options{
		Debounce: 20 * time.Millisecond, OnFire: rec.cb(),
	})
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()

	write(t, path, "v1")
	waitFor(t, "fire after write", func() bool { return rec.count() >= 1 })

	ev := rec.last()
	if ev.Path != path {
		t.Fatalf("event path = %q, want %q", ev.Path, path)
	}
	if ev.Op != watch.Create && ev.Op != watch.Write {
		t.Fatalf("event op = %v, want create or write", ev.Op)
	}
}

func TestFileDebounceMergesBurst(t *testing.T) {
	path := newFile(t)
	rec := &recorder{}
	w, err := watch.Watch(stdctx.Background(), path, watch.Options{
		Debounce: 100 * time.Millisecond, OnFire: rec.cb(),
	})
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()

	for i := 0; i < 5; i++ {
		write(t, path, "v")
		time.Sleep(10 * time.Millisecond)
	}
	// 一串写入应合并为一次 fire；给足防抖落定时间。
	time.Sleep(500 * time.Millisecond)
	if got := rec.count(); got != 1 {
		t.Fatalf("fire count = %d, want 1 (debounced)", got)
	}
}

// 原子保存（临时文件 + rename 覆盖）：同目录的临时文件事件必须被
// 过滤，fire 落定后读到的应是新内容。
func TestFileAtomicSave(t *testing.T) {
	path := newFile(t)
	var mu sync.Mutex
	var content string
	rec := &recorder{}
	onFire := func(ev watch.Event) {
		rec.cb()(ev)
		b, err := os.ReadFile(path)
		if err != nil {
			return
		}
		mu.Lock()
		content = string(b)
		mu.Unlock()
	}
	w, err := watch.Watch(stdctx.Background(), path, watch.Options{
		Debounce: 100 * time.Millisecond, OnFire: onFire,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()

	tmp := path + ".tmp"
	write(t, tmp, "atomic")
	if err := os.Rename(tmp, path); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "fire after atomic save", func() bool { return rec.count() >= 1 })
	time.Sleep(400 * time.Millisecond)

	if got := rec.count(); got != 1 {
		t.Fatalf("fire count = %d, want 1 (temp-file events filtered)", got)
	}
	mu.Lock()
	defer mu.Unlock()
	if content != "atomic" {
		t.Fatalf("content at fire = %q, want %q", content, "atomic")
	}
}

// 删除被监听文件 → fire（Op=remove），fire 时刻 stat 终态为消失。
func TestFileRemovedFires(t *testing.T) {
	path := newFile(t)
	var mu sync.Mutex
	var gone bool
	rec := &recorder{}
	onFire := func(ev watch.Event) {
		rec.cb()(ev)
		_, err := os.Stat(path)
		mu.Lock()
		gone = err != nil
		mu.Unlock()
	}
	w, err := watch.Watch(stdctx.Background(), path, watch.Options{
		Debounce: 20 * time.Millisecond, OnFire: onFire,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()

	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "fire after remove", func() bool { return rec.count() >= 1 })

	if rec.last().Op != watch.Remove {
		t.Fatalf("event op = %v, want remove", rec.last().Op)
	}
	mu.Lock()
	defer mu.Unlock()
	if !gone {
		t.Fatal("file still present at fire time")
	}
}

// 目录监听：目录内条目增删都触发，Path 是条目自身。
func TestDirWatch(t *testing.T) {
	dir := tempDir(t)
	rec := &recorder{}
	w, err := watch.Watch(stdctx.Background(), dir, watch.Options{
		Debounce: 20 * time.Millisecond, OnFire: rec.cb(),
	})
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()

	entry := filepath.Join(dir, "entry.txt")
	write(t, entry, "x")
	waitFor(t, "fire after create", func() bool { return rec.count() >= 1 })
	if rec.last().Path != entry {
		t.Fatalf("event path = %q, want %q", rec.last().Path, entry)
	}
	// 新建即写入的平台差异：Linux inotify 的最后事件是 write，macOS
	// fsevents 合并为 create——两者都算"条目出现"。
	if rec.last().Op != watch.Create && rec.last().Op != watch.Write {
		t.Fatalf("event op = %v, want create or write", rec.last().Op)
	}

	if err := os.Remove(entry); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "fire after remove", func() bool { return rec.count() >= 2 })
	if rec.last().Op != watch.Remove {
		t.Fatalf("event op = %v, want remove", rec.last().Op)
	}
}

// Close 幂等且停跳：关闭后事件不再触发 fire。
func TestCloseStops(t *testing.T) {
	path := newFile(t)
	rec := &recorder{}
	w, err := watch.Watch(stdctx.Background(), path, watch.Options{
		Debounce: 20 * time.Millisecond, OnFire: rec.cb(),
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); err != nil {
		t.Fatalf("second Close: %v", err)
	}

	write(t, path, "after close")
	time.Sleep(200 * time.Millisecond)
	if got := rec.count(); got != 0 {
		t.Fatalf("fire count after Close = %d, want 0", got)
	}
}

// ctx 取消与 Close 有同等效果；先确认取消前正常触发。
func TestCtxCancelStops(t *testing.T) {
	path := newFile(t)
	rec := &recorder{}
	ctx, cancel := stdctx.WithCancel(stdctx.Background())
	w, err := watch.Watch(ctx, path, watch.Options{
		Debounce: 20 * time.Millisecond, OnFire: rec.cb(),
	})
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()

	write(t, path, "v1")
	waitFor(t, "fire before cancel", func() bool { return rec.count() == 1 })

	cancel()
	write(t, path, "after cancel")
	time.Sleep(200 * time.Millisecond)
	if got := rec.count(); got != 1 {
		t.Fatalf("fire count after cancel = %d, want 1", got)
	}
}

func TestWatchInputErrors(t *testing.T) {
	onFire := func(watch.Event) {}
	if _, err := watch.Watch(stdctx.Background(), "no-such-file", watch.Options{OnFire: onFire}); err == nil {
		t.Fatal("expected error for missing path")
	}
	if _, err := watch.Watch(stdctx.Background(), ".", watch.Options{}); err == nil {
		t.Fatal("expected error for missing OnFire")
	}
}
