TINYGO ?= tinygo

# 用 TinyGo 把 guest/ 编译为 reactor 模式的 guest.wasm。
wasm:
	cd guest && $(TINYGO) build -target wasip1 -buildmode=c-shared -o ../guest.wasm .

# 构建 guest 并启动服务器（监听 :8080）。
run: wasm
	go run .

.PHONY: wasm run
