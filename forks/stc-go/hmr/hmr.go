// Package hmr 把文件系统变化粘合成 wasm.Handle 的原子热重载：
// 监听目标 .wasm 文件，防抖后读取新字节并调用 Handle.Update。
// 更新失败时旧版本由 stc-go/wasm 的机制保留或回滚（探针失败旧版本
// 原样不动，start trap 自动回滚），结果经 OnReload 回调上报。
package hmr

import (
	stdctx "context"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/fsnotify/fsnotify"

	"github.com/0xdenny218/stc-go/wasm"
)

// Options 配置 Watcher。
type Options struct {
	// Debounce 是合并连续文件事件的等待窗口；零值取 200ms。
	Debounce time.Duration
	// OnReload 在每次 Update 落定后回调（err 即 Update 的返回值，
	// 含读取文件失败与 watcher 运行期错误）。
	OnReload func(err error)
}

// Watcher 监听单个 wasm 文件并驱动热重载。ctx 取消或 Close 时停止。
type Watcher struct {
	fw   *fsnotify.Watcher
	done chan struct{}
	once sync.Once
}

// Watch 开始监听 path（相对/绝对路径均可）并立即返回。
// 文件每次内容变化（含原子保存的 rename 形态）都会触发一次
// 防抖合并后的原子 Update。
func Watch(ctx stdctx.Context, h *wasm.Handle, path string, opts *Options) (*Watcher, error) {
	if opts == nil {
		opts = &Options{}
	}
	debounce := opts.Debounce
	if debounce <= 0 {
		debounce = 200 * time.Millisecond
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	fw, err := fsnotify.NewWatcher()
	if err != nil {
		return nil, err
	}
	// 监听目录而非文件本身：多数工具链的原子保存是「临时文件 + rename
	// 覆盖」，直接盯文件会在第一次保存后丢句柄。
	if err := fw.Add(filepath.Dir(abs)); err != nil {
		_ = fw.Close()
		return nil, err
	}
	wt := &Watcher{fw: fw, done: make(chan struct{})}
	base := filepath.Base(abs)
	go wt.loop(ctx, h, abs, base, debounce, opts.OnReload)
	return wt, nil
}

func (wt *Watcher) loop(ctx stdctx.Context, h *wasm.Handle, abs, base string, debounce time.Duration, onReload func(error)) {
	report := func(err error) {
		if onReload != nil {
			onReload(err)
		}
	}
	var timer *time.Timer
	var timerC <-chan time.Time
	stopTimer := func() {
		if timer != nil {
			timer.Stop()
			timer = nil
		}
		timerC = nil
	}
	defer func() {
		stopTimer()
		_ = wt.fw.Close()
	}()
	for {
		select {
		case <-ctx.Done():
			return
		case <-wt.done:
			return
		case ev, ok := <-wt.fw.Events:
			if !ok {
				return
			}
			if filepath.Base(ev.Name) != base {
				continue
			}
			if !ev.Op.Has(fsnotify.Write) && !ev.Op.Has(fsnotify.Create) && !ev.Op.Has(fsnotify.Rename) {
				continue
			}
			stopTimer()
			timer = time.NewTimer(debounce)
			timerC = timer.C
		case err, ok := <-wt.fw.Errors:
			if !ok {
				return
			}
			report(err)
		case <-timerC:
			stopTimer()
			src, err := os.ReadFile(abs)
			if err == nil {
				err = h.Update(ctx, src)
			}
			report(err)
		}
	}
}

// Close 停止监听并释放 watcher（幂等；ctx 取消有同等效果）。
func (wt *Watcher) Close() error {
	wt.once.Do(func() { close(wt.done) })
	return nil
}
