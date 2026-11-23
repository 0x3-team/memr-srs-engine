import { areaMethods, empty, enumeration as e, object as o, string as s } from "./_schema.mjs";
import { mutation, operation, syncStatus } from "./_shapes.mjs";
export default areaMethods("sync", [
  ["status", "Read sync endpoint and conflict summary without returning credentials.", empty, syncStatus, { mutating: false, longOperation: false }],
  ["login", "Authenticate to an allowed Anki-protocol server; AnkiWeb hosts are refused.", o({ endpoint: s(2048), username: s(255), password: s(4096) }), operation, { mutating: true, longOperation: true }],
  ["logout", "Forget the saved sync credentials.", empty, mutation(), { mutating: true, longOperation: false }],
  ["run", "Run normal collection sync or report that a full-sync choice is required.", empty, operation, { mutating: true, longOperation: true }],
  ["resolveFullSync", "Perform the user's explicit upload or download conflict choice.", o({ direction: e("upload", "download") }), operation, { mutating: true, longOperation: true }],
  ["media", "Synchronize media with progress and cancellation.", empty, operation, { mutating: true, longOperation: true }],
  ["abort", "Request cancellation of the active sync operation.", empty, mutation(), { mutating: true, longOperation: false }],
]);
