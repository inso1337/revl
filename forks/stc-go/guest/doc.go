// Package guest 是 stc-go WASM 组件的 guest 侧 SDK。
//
// 用法：在 guest 程序里 import 本包，用 TinyGo 以 reactor 模式编译：
//
//	tinygo build -target wasip1 -buildmode=c-shared -o guest.wasm .
//
// guest 程序经 //export start / //export stop 参与 fiber 生命周期；
// 本包函数是对 stc-go 宿主模块 "stc" 的薄封装（字符串值 ABI），
// start 内 Provide 的服务登记在 fiber 自己的 context 上，
// 组件卸载时由 stc-go 核心机制精确回卷，guest 无需自登记清理。
//
// 完整的 guest 示例见 examples/plugin-http/guest。
//
// 注意：本包只在 wasm 目标下可编译（宿主 ABI 以 32 位 int 为前提，
// 与 TinyGo 一致）；宿主页见 stc-go/wasm 包文档。
package guest
