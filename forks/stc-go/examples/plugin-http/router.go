package main

import (
	"net/http"
	"sync"

	stc "github.com/0xdenny218/stc-go"
)

// router 是支持动态增删的复用器：注册返回逆操作，
// 撤销时精确摘除路由——时间可组合性最直观的形态。
type router struct {
	mu     sync.RWMutex
	routes map[string]http.Handler
}

func newRouter() *router { return &router{routes: map[string]http.Handler{}} }

var routerKey = stc.NewKey[*router]("router")

// handle 注册路由；返回的逆操作（幂等）摘除它。
func (rt *router) handle(pattern string, h http.Handler) stc.Inverse {
	rt.mu.Lock()
	rt.routes[pattern] = h
	rt.mu.Unlock()
	var once sync.Once
	return func() error {
		once.Do(func() {
			rt.mu.Lock()
			delete(rt.routes, pattern)
			rt.mu.Unlock()
		})
		return nil
	}
}

func (rt *router) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	rt.mu.RLock()
	h := rt.routes[r.URL.Path]
	rt.mu.RUnlock()
	if h == nil {
		http.NotFound(w, r)
		return
	}
	h.ServeHTTP(w, r)
}

// component 把 router 提供为服务，供其他 fiber 注入。
func (rt *router) component() stc.Component {
	return stc.Component{
		Name:    "router",
		Provide: []stc.Key{routerKey},
		Apply: func(c *stc.Context) (stc.Inverse, error) {
			_, err := c.Provide(routerKey, rt)
			return nil, err
		},
	}
}
