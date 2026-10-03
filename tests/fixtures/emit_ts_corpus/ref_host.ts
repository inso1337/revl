// The host module ref_externs.rvl's `@ts ref` externs name. The byte oracle
// only emits the lazy import thunks; nothing here runs in that test. The
// compiler hashes this file, so it has to exist.
export async function fetchRemote(url: string): Promise<string> {
  return url
}

export function mintToken(label: string): string {
  return `token:${label}`
}
