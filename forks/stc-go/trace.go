package stc

// TraceKind 枚举 fiber 与服务生命周期事件，供验收测试断言顺序。
type TraceKind uint8

const (
	TraceProvide TraceKind = iota + 1
	TraceUnprovide
	TraceLoading
	TraceApplyStart
	TraceApplied
	TraceActive
	TraceUnloading
	TraceUnwound
	TracePending
	TraceFailed
	TraceGone
)

func (k TraceKind) String() string {
	switch k {
	case TraceProvide:
		return "provide"
	case TraceUnprovide:
		return "unprovide"
	case TraceLoading:
		return "loading"
	case TraceApplyStart:
		return "apply-start"
	case TraceApplied:
		return "applied"
	case TraceActive:
		return "active"
	case TraceUnloading:
		return "unloading"
	case TraceUnwound:
		return "unwound"
	case TracePending:
		return "pending"
	case TraceFailed:
		return "failed"
	case TraceGone:
		return "gone"
	}
	return "unknown"
}

// TraceEvent 是全局单调递增序列号下的一次生命周期事件。
type TraceEvent struct {
	Seq   uint64
	Kind  TraceKind
	Fiber uint64 // 0 表示根 context
	Key   string
}
