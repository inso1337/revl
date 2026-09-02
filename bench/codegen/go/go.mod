// Codegen performance harness for the go backend. No external dependencies:
// the ir_version 3 pure tier the emitter targets here is ordinary Go, so
// nothing links against stc-go and `go test -bench` runs offline.
module revl.bench/codegen

go 1.25.0
