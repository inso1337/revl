// Package watch 把文件系统事件粘合成防抖后的终态回调：监听一个文件
// 或目录，事件停歇一个防抖窗口后以窗口内最后的事件回调一次 OnFire。
// 刻意极简——不做 diff、不带领域语义，fire 意味着什么由消费方决定
// （stat 定 reload/gone，或全量扫目录做装/卸差分）。监听形态按启动时
// stat 自动区分：文件 → 盯所在目录并按 base 名过滤（原子保存的
// 「临时文件 + rename 覆盖」形态安全）；目录 → 盯目录自身。
//
// 该原语经回流评审从 stc-agent M8 skills 手写的两套 fsnotify 防抖
// 循环提取（stc-go#6）；hmr 内部还有第三套同构循环。
package watch

import (
	stdctx "context"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/fsnotify/fsnotify"
)

// Op 是触发一次 fire 的事件类别，取防抖窗口内最后一个事件的归类。
type Op uint8

const (
	Create Op = iota + 1 // 条目出现（含原子保存 rename 覆盖后的落地）
	Write                // 内容写入
	Remove               // 条目消失
	Rename               // 改名（原子保存的前半形态）
)

func (op Op) String() string {
	switch op {
	case Create:
		return "create"
	case Write:
		return "write"
	case Remove:
		return "remove"
	case Rename:
		return "rename"
	default:
		return "unknown"
	}
}

// Event 是一次 fire 的载荷：Path 为事件路径（绝对路径），Op 为事件
// 类别。终态以消费方在回调里 stat/扫描的结果为准——Op 只是提示。
type Event struct {
	Path string
	Op   Op
}

// Options 配置 Watcher。
type Options struct {
	// Debounce 是合并连续事件的等待窗口；零值取 200ms。
	Debounce time.Duration
	// OnFire 在事件停歇一个防抖窗口后回调（必填）。
	OnFire func(ev Event)
}

// Watcher 监听一个文件或目录，防抖后驱动 OnFire。ctx 取消或 Close
// 时停止。
type Watcher struct {
	fw   *fsnotify.Watcher
	done chan struct{}
	once sync.Once
	base string // 文件形态只认这个 base 名；目录形态为空
}

// Watch 开始监听 path（文件或目录，相对/绝对路径均可）并立即返回。
// path 按启动时 stat 区分形态：文件 → 监听所在目录并过滤到该文件
// （多数工具链的原子保存是「临时文件 + rename 覆盖」，直接盯文件
// 会在第一次保存后丢句柄）；目录 → 监听目录自身，内部任意条目的
// create/write/rename/remove 事件都会触发（防抖合并为一次）。
// path 不存在或 OnFire 缺省是错误。
func Watch(ctx stdctx.Context, path string, opts Options) (*Watcher, error) {
	debounce := opts.Debounce
	if debounce <= 0 {
		debounce = 200 * time.Millisecond
	}
	if opts.OnFire == nil {
		return nil, errors.New("watch: OnFire is required")
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	st, err := os.Stat(abs)
	if err != nil {
		return nil, err
	}
	watchDir, base := abs, ""
	if !st.IsDir() {
		watchDir, base = filepath.Dir(abs), filepath.Base(abs)
	}
	fw, err := fsnotify.NewWatcher()
	if err != nil {
		return nil, err
	}
	if err := fw.Add(watchDir); err != nil {
		_ = fw.Close()
		return nil, err
	}
	wt := &Watcher{fw: fw, done: make(chan struct{}), base: base}
	go wt.loop(ctx, base, debounce, opts.OnFire)
	return wt, nil
}

// loop 事件主循环：匹配事件（重）置防抖定时器，停歇一个窗口后以
// 窗口内最后的事件回调 OnFire。Chmod 不携带内容变化，忽略；运行期
// 错误事件同样忽略（监听不中断，回调语义里没有错误通道）。
func (wt *Watcher) loop(ctx stdctx.Context, base string, debounce time.Duration, onFire func(Event)) {
	var timer *time.Timer
	var timerC <-chan time.Time
	var last Event
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
			op, ok := classify(ev.Op)
			if !ok {
				continue
			}
			if base != "" && filepath.Base(ev.Name) != base {
				continue
			}
			stopTimer()
			last = Event{Path: ev.Name, Op: op}
			timer = time.NewTimer(debounce)
			timerC = timer.C
		case _, ok := <-wt.fw.Errors:
			if !ok {
				return
			}
		case <-timerC:
			stopTimer()
			onFire(last)
		}
	}
}

// classify 把 fsnotify 的操作位映射到 Op；Chmod 与空操作位不触发
// fire，返回 false。组合位按「更接近消失」的优先级归类。
func classify(op fsnotify.Op) (Op, bool) {
	switch {
	case op.Has(fsnotify.Remove):
		return Remove, true
	case op.Has(fsnotify.Rename):
		return Rename, true
	case op.Has(fsnotify.Create):
		return Create, true
	case op.Has(fsnotify.Write):
		return Write, true
	}
	return 0, false
}

// Close 停止监听并释放 watcher（幂等；ctx 取消有同等效果）。
func (wt *Watcher) Close() error {
	wt.once.Do(func() { close(wt.done) })
	return nil
}
