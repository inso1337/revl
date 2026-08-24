import { GeoPoint } from './geo'

// The interface a plugin annotates with — with a nested record pulled from a
// further local module, and an optional field.
export interface UserRecord {
  id: string
  displayName: string
  home?: GeoPoint
  tags: string[]
}

// A bare type alias re-exported alongside it (resolves inline, no `type` decl).
export type UserId = string
