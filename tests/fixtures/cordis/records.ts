import { Context, Service } from 'cordis'
import { UserRecord, UserId } from './models/user'

export const name = 'directory'

// A plugin whose operations traffic in a record type defined in another local
// module. The importer must follow the import and transcribe UserRecord (and
// the GeoPoint it nests) as revl `type`s, rather than refusing the nominal.
export class Directory extends Service {
  constructor(ctx: Context) {
    super(ctx, 'directory')
  }

  /** Register a user record. */
  register(user: UserRecord): void {}

  /** Fetch a user record by id. */
  lookup(id: UserId): UserRecord | undefined {
    return undefined
  }
}
