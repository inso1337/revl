// 插件式 HTTP 服务器：时空可组合性在真实服务里的样子。
//
//   - 路由注册是可逆效应：fiber 卸载时路由被精确摘除（时间维度）；
//   - banner 重提供 → 注入它的 hello 插件级联重载（空间维度）；
//   - WASM guest 提供字符串服务，Go 桥插件把它暴露为 /wasm——
//     跨 WASM 边界的依赖链；guest.wasm 文件变化经 hmr 原子换血，
//     桥插件随之自动重载。
package main

import (
	"context"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"syscall"

	stc "github.com/0xdenny218/stc-go"
	"github.com/0xdenny218/stc-go/hmr"
	"github.com/0xdenny218/stc-go/wasm"
)

var bannerKey = stc.NewKey[string]("banner")

func main() {
	root := stc.New()
	rt := newRouter()

	// 基础设施 fiber：提供 router 服务。
	routerFiber := root.Load(rt.component())
	if err := routerFiber.Ready(context.Background()); err != nil {
		log.Fatal(err)
	}

	// banner 配置挂在根上；/admin/banner 演示「重提供 → 依赖方级联重载」。
	bannerInv, err := root.Provide(bannerKey, "v1")
	if err != nil {
		log.Fatal(err)
	}
	var bannerMu sync.Mutex

	// hello 插件：注入 router 与 banner，缺任一则停在 Pending。
	root.Load(stc.Component{
		Name:   "hello",
		Inject: []stc.Key{routerKey, bannerKey},
		Apply: func(c *stc.Context) (stc.Inverse, error) {
			rt, err := stc.Service[*router](c, routerKey)
			if err != nil {
				return nil, err
			}
			banner, err := stc.Service[string](c, bannerKey)
			if err != nil {
				return nil, err
			}
			log.Printf("[hello] loaded, banner=%q", banner)
			return rt.handle("/hello", http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				fmt.Fprintf(w, "hello from banner %s\n", banner)
			})), nil
		},
	})

	// WASM 桥插件：注入 guest 提供的字符串服务，暴露为 /wasm。
	// guest 换血 → wasm-message 代际变化 → 本插件自动重载。
	wasmRt, err := wasm.NewRuntime()
	if err != nil {
		log.Fatal(err)
	}
	msgKey := wasmRt.Key("wasm-message")
	root.Load(stc.Component{
		Name:   "wasm-bridge",
		Inject: []stc.Key{routerKey, msgKey},
		Apply: func(c *stc.Context) (stc.Inverse, error) {
			rt, err := stc.Service[*router](c, routerKey)
			if err != nil {
				return nil, err
			}
			msg, err := stc.Service[string](c, msgKey)
			if err != nil {
				return nil, err
			}
			log.Printf("[wasm-bridge] loaded, message=%q", msg)
			return rt.handle("/wasm", http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				fmt.Fprintln(w, msg)
			})), nil
		},
	})

	// 装载 guest 并挂热重载。文件缺失则降级运行（make wasm 可补齐）。
	if src, err := os.ReadFile("guest.wasm"); err == nil {
		h, err := wasm.Load(context.Background(), root, wasmRt, src, wasm.Options{
			Name: "guest", Provide: []string{"wasm-message"},
		})
		if err != nil {
			log.Fatal(err)
		}
		if _, err := hmr.Watch(context.Background(), h, "guest.wasm", &hmr.Options{
			OnReload: func(err error) {
				if err != nil {
					log.Printf("[hmr] reload failed, old version kept: %v", err)
				} else {
					log.Printf("[hmr] guest reloaded")
				}
			},
		}); err != nil {
			log.Fatal(err)
		}
	} else {
		log.Print("guest.wasm not found; /wasm disabled (run `make wasm`)")
	}

	// /admin/banner?text=x：重提供 banner，hello 插件级联重载。
	// 管理面路由注册为根 Effect，Close 时一并摘除。
	if err := root.Effect(func() stc.Inverse {
		return rt.handle("/admin/banner", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			text := r.URL.Query().Get("text")
			if text == "" {
				http.Error(w, "missing ?text=", http.StatusBadRequest)
				return
			}
			bannerMu.Lock()
			defer bannerMu.Unlock()
			if err := bannerInv(); err != nil {
				http.Error(w, err.Error(), http.StatusInternalServerError)
				return
			}
			if bannerInv, err = root.Provide(bannerKey, text); err != nil {
				http.Error(w, err.Error(), http.StatusInternalServerError)
				return
			}
			fmt.Fprintf(w, "banner=%q; /hello will serve it after reload\n", text)
		}))
	}); err != nil {
		log.Fatal(err)
	}

	// HTTP 服务器本身也是可逆效应：逆操作 = 优雅关停。
	srv := &http.Server{Handler: rt}
	ln, err := net.Listen("tcp", ":8080")
	if err != nil {
		log.Fatal(err)
	}
	if err := root.Effect(func() stc.Inverse {
		go func() {
			if err := srv.Serve(ln); err != http.ErrServerClosed {
				log.Printf("serve: %v", err)
			}
		}()
		return func() error { return srv.Shutdown(context.Background()) }
	}); err != nil {
		log.Fatal(err)
	}

	log.Print("listening on :8080 — try /hello, /wasm, /admin/banner?text=v2")
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	<-ctx.Done()
	log.Print("shutting down: rewinding all effects")
	if err := root.Close(); err != nil {
		log.Printf("close: %v", err)
	}
	_ = wasmRt.Close()
}
