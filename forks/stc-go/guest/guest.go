//go:build wasm

package guest

import (
	"errors"
	"unsafe"
)

// 宿主模块 "stc" 的导入函数（ABI 见 stc-go/wasm 包文档；值一律为字符串）。

//go:wasmimport stc provide
func hostProvide(keyPtr, keyLen, valPtr, valLen uint32) int32

//go:wasmimport stc get_size
func hostGetSize(keyPtr, keyLen uint32) int32

//go:wasmimport stc get
func hostGet(keyPtr, keyLen, bufPtr, bufLen uint32) int32

//go:wasmimport stc log
func hostLog(msgPtr, msgLen uint32)

// ErrProvide 是宿主拒绝服务登记时返回的错误（如重复提供）。
var ErrProvide = errors.New("stc-guest: provide rejected by host")

func strarg(s string) (uint32, uint32) {
	if len(s) == 0 {
		return 0, 0
	}
	return uint32(uintptr(unsafe.Pointer(unsafe.StringData(s)))), uint32(len(s))
}

// Provide 以 key 提供字符串服务。fiber 卸载时该提供被自动撤销，
// 无需（也不应）在 stop 里手动抵消。
func Provide(key, value string) error {
	kp, kl := strarg(key)
	vp, vl := strarg(value)
	if hostProvide(kp, kl, vp, vl) != 0 {
		return ErrProvide
	}
	return nil
}

// Get 读取 key 的字符串值；第二个返回值报告 key 是否存在。
func Get(key string) (string, bool) {
	kp, kl := strarg(key)
	n := hostGetSize(kp, kl)
	if n < 0 {
		return "", false
	}
	buf := make([]byte, n)
	w := hostGet(kp, kl, uint32(uintptr(unsafe.Pointer(unsafe.SliceData(buf)))), uint32(len(buf)))
	if w < 0 {
		return "", false
	}
	return string(buf[:w]), true
}

// MustGet 同 Get，key 不存在时 panic（依赖缺失本该由 Inject 门控挡住）。
func MustGet(key string) string {
	v, ok := Get(key)
	if !ok {
		panic("stc-guest: missing service " + key)
	}
	return v
}

// Log 追加一条消息到宿主 Runtime 的日志（host 侧经 Runtime.Logs 观察）。
func Log(msg string) {
	p, l := strarg(msg)
	hostLog(p, l)
}

// ------------------------------------------------------------------
// host→guest 调用（wasm.Handle.Call 的 guest 侧；ABI 见 stc-go/wasm 包文档）
// ------------------------------------------------------------------

var (
	invokeHandler func(args string) string
	callBufs      = map[uint32][]byte{} // 宿主可见缓冲的保活表，stc_free 删除
)

// OnInvoke 注册 "invoke" 调用的处理器：宿主经
// wasm.Handle.Call(ctx, "invoke", args) 触发，入参与返回值都是字符串
// （协议层通常携带 JSON）。应在模块开始服务调用前注册（包级变量初始化
// 或 start 内均可）。
func OnInvoke(fn func(args string) string) { invokeHandler = fn }

// stc_alloc 为宿主分配可写缓冲（宿主随后写入调用入参）；缓冲登记在
// callBufs 防 GC，stc_free 时删除。
//
//export stc_alloc
func stcAlloc(n uint32) uint32 {
	buf := make([]byte, n)
	ptr := uint32(uintptr(unsafe.Pointer(unsafe.SliceData(buf))))
	callBufs[ptr] = buf
	return ptr
}

//export stc_free
func stcFree(ptr, _ uint32) { delete(callBufs, ptr) }

// invoke 是 host→guest 调用的固定入口：解包入参 → 调 OnInvoke 注册的
// 处理器 → 把结果拷入受管缓冲并打包 (指针<<32)|长度 返回。
//
//export invoke
func invoke(ptr, n uint32) uint64 {
	if invokeHandler == nil {
		return 0
	}
	var arg string
	if n > 0 {
		arg = string(unsafe.Slice((*byte)(unsafe.Pointer(uintptr(ptr))), int(n)))
	}
	res := invokeHandler(arg)
	if len(res) == 0 {
		return 0
	}
	buf := make([]byte, len(res))
	copy(buf, res)
	rp := uint32(uintptr(unsafe.Pointer(unsafe.SliceData(buf))))
	callBufs[rp] = buf
	return uint64(rp)<<32 | uint64(len(buf))
}
