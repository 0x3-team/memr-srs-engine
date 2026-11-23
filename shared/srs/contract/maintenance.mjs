import { areaMethods, array, empty, integer as i, object as o, string as s } from "./_schema.mjs";
import { epochSeconds, ids, operation } from "./_shapes.mjs";
export default areaMethods("maintenance", [
  ["checkDatabase", "Check and repair collection consistency using the engine's own checks.", empty, operation, { mutating: true, longOperation: true }],
  ["emptyCards", "Scan empty questions; completed progress returns emptyCards with the notes and cards.", empty, operation, { mutating: false, longOperation: true }],
  ["removeEmptyCards", "Scan and remove explicitly selected empty cards; completed progress returns count.", o({ cardIds: ids }), operation, { mutating: true, longOperation: true }],
  ["backupNow", "Create a retained collection backup and prove native restorability within the package safety limits.", empty, operation, { mutating: true, longOperation: true }],
  ["backups", "List collection backups without exposing local filesystem paths.", empty, o({ backups: array(o({ id: s(255), createdAt: epochSeconds, bytes: i() }), 10000) }), { mutating: false, longOperation: false }],
  ["restoreBackup", "Restore a backup within package safety limits after creating and verifying the current collection backup.", o({ backupId: s(255) }), operation, { mutating: true, longOperation: true }],
]);
