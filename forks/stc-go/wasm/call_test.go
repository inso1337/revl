package wasm

// Handle.Call 契约（host→guest 带参调用，全字符串 ABI）：
// 入参/结果跨边界往返、缓冲释放、Update 后走新版本、坏构建旧版本服役、
// 换血与调用互斥（-race）。

import (
	stdctx "context"
	"strings"
	"testing"
	"time"
)

// waitLog 等到 Runtime 日志出现含 sub 的条目。
func waitLog(t *testing.T, rt *Runtime, sub string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		for _, l := range rt.Logs() {
			if strings.Contains(l, sub) {
				return
			}
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("log %q never appeared; logs=%v", sub, rt.Logs())
}

func TestHandleCall(t *testing.T) {
	root, rt := setup(t)
	h, err := Load(bg(), root, rt, callGuest("hi:"), Options{Name: "caller"})
	if err != nil {
		t.Fatal(err)
	}

	out, err := h.Call(bg(), "invoke", "bob")
	if err != nil {
		t.Fatalf("Call: %v", err)
	}
	if out != "hi:bob" {
		t.Fatalf("result: %q", out)
	}

	// stc_free 应对入参与结果各调用一次（Call 返回前两笔都已落定）。
	var freed int
	for _, l := range rt.Logs() {
		if l == "freed" {
			freed++
		}
	}
	if freed != 2 {
		t.Fatalf("stc_free calls: %d, want 2 (input + result); logs=%v", freed, rt.Logs())
	}
}

func TestHandleCallErrors(t *testing.T) {
	root, rt := setup(t)

	t.Run("missing export", func(t *testing.T) {
		h, err := Load(bg(), root, rt, helloGuest("x"), Options{Name: "plain"})
		if err != nil {
			t.Fatal(err)
		}
		if _, err := h.Call(bg(), "invoke", "x"); err == nil ||
			!strings.Contains(err.Error(), `no export "invoke"`) {
			t.Fatalf("want missing-export error, got %v", err)
		}
	})

	t.Run("missing stc_alloc", func(t *testing.T) {
		h, err := Load(bg(), root, rt, spinGuest("x"), Options{Name: "noalloc"})
		if err != nil {
			t.Fatal(err)
		}
		// 空入参跳过分配，自旋语义不变——这里用非空入参触发 alloc 缺失。
		if _, err := h.Call(bg(), "invoke", "x"); err == nil ||
			!strings.Contains(err.Error(), "no stc_alloc export") {
			t.Fatalf("want missing-alloc error, got %v", err)
		}
	})

	t.Run("no active module", func(t *testing.T) {
		h, err := Load(bg(), root, rt, callGuest("z:"), Options{Name: "gone"})
		if err != nil {
			t.Fatal(err)
		}
		h.Dispose()
		ctx, cancel := stdctx.WithTimeout(bg(), 5*time.Second)
		defer cancel()
		if err := h.Fiber().Gone(ctx); err != nil {
			t.Fatal(err)
		}
		if _, err := h.Call(bg(), "invoke", "x"); err == nil ||
			!strings.Contains(err.Error(), "no active module") {
			t.Fatalf("want no-active-module error, got %v", err)
		}
	})
}

func TestHandleCallAfterUpdate(t *testing.T) {
	root, rt := setup(t)
	h, err := Load(bg(), root, rt, callGuest("v1:"), Options{Name: "up"})
	if err != nil {
		t.Fatal(err)
	}
	if out, err := h.Call(bg(), "invoke", "x"); err != nil || out != "v1:x" {
		t.Fatalf("v1 call: %q, %v", out, err)
	}

	ctx, cancel := stdctx.WithTimeout(bg(), 5*time.Second)
	defer cancel()
	if err := h.Update(ctx, callGuest("v2:")); err != nil {
		t.Fatalf("Update: %v", err)
	}
	if out, err := h.Call(bg(), "invoke", "x"); err != nil || out != "v2:x" {
		t.Fatalf("v2 call: %q, %v", out, err)
	}
}

// 坏构建（探针失败）与 start trap（回滚）都不得影响在役版本的调用。
func TestHandleCallBadUpdateKeepsServing(t *testing.T) {
	root, rt := setup(t)
	h, err := Load(bg(), root, rt, callGuest("v1:"), Options{Name: "bad"})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := stdctx.WithTimeout(bg(), 5*time.Second)
	defer cancel()

	if err := h.Update(ctx, badGuest()); err == nil ||
		!strings.Contains(err.Error(), "old version intact") {
		t.Fatalf("want probe failure, got %v", err)
	}
	if out, err := h.Call(bg(), "invoke", "a"); err != nil || out != "v1:a" {
		t.Fatalf("after bad build: %q, %v", out, err)
	}

	if err := h.Update(ctx, trapGuest()); err == nil ||
		!strings.Contains(err.Error(), "rolled back") {
		t.Fatalf("want rollback, got %v", err)
	}
	if out, err := h.Call(bg(), "invoke", "b"); err != nil || out != "v1:b" {
		t.Fatalf("after rollback: %q, %v", out, err)
	}
}

// Contract/UpdateWaitsInflight（-race）：进行中的调用在旧版本上完整
// 跑完，Update 等待；Update 落定后的调用走新版本。
func TestHandleCallUpdateMutualExclusion(t *testing.T) {
	root, rt := setup(t)
	h, err := Load(bg(), root, rt, spinGuest("v1-done"), Options{Name: "spin"})
	if err != nil {
		t.Fatal(err)
	}

	type callResult struct {
		out string
		err error
	}
	callDone := make(chan callResult, 1)
	go func() {
		out, err := h.Call(bg(), "invoke", "")
		callDone <- callResult{out, err}
	}()
	waitLog(t, rt, "spinning") // 调用确已在飞

	updateDone := make(chan error, 1)
	go func() { updateDone <- h.Update(bg(), callGuest("v2:")) }()

	select {
	case err := <-updateDone:
		t.Fatalf("Update returned while a call was in flight: %v", err)
	case <-time.After(100 * time.Millisecond):
	}

	// 放行在途调用：guest 的自旋条件是根上出现字符串服务 "release"。
	if _, err := root.Provide(rt.Key("release"), "1"); err != nil {
		t.Fatal(err)
	}

	select {
	case res := <-callDone:
		if res.err != nil || res.out != "v1-done" {
			t.Fatalf("in-flight call: %q, %v (torn?)", res.out, res.err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("in-flight call never returned after release")
	}
	select {
	case err := <-updateDone:
		if err != nil {
			t.Fatalf("Update: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Update never returned after the in-flight call finished")
	}

	if out, err := h.Call(bg(), "invoke", "q"); err != nil || out != "v2:q" {
		t.Fatalf("post-update call: %q, %v", out, err)
	}
}
