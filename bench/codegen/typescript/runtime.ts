// Shim so the generated files under `emitted/` resolve their `../runtime.ts`
// import to the real backend runtime without being moved into the backend
// tree. Nothing is stubbed: this re-exports the shipping module.
export * from '../../../backends/typescript/runtime.ts'
