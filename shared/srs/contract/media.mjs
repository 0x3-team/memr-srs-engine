import { areaMethods, array, empty, integer as i, object as o, string as s } from "./_schema.mjs";
import { handle, mutation, operation } from "./_shapes.mjs";
export default areaMethods("media", [
  ["addFromUpload", "Copy an uploaded file of at most 100 MiB into media using Anki's safe deduplicated naming.", o({ uploadId: handle, filename: s(1024) }), mutation({ filename: s(1024) }), { mutating: true, longOperation: false }],
  ["check", "Check referenced media, missing files and unused files.", empty, operation, { mutating: false, longOperation: true }],
  ["trashUnused", "Move explicitly selected unused media to the engine's trash.", o({ filenames: array(s(1024), 100000) }), mutation({ count: i() }), { mutating: true, longOperation: false }],
  ["emptyTrash", "Remove trashed media after a verified retained recovery backup; synchronous compatibility endpoint.", empty, mutation({ count: i() }), { mutating: true, longOperation: false }],
  ["emptyTrashWithBackup", "Require the confirmed trash inventory, back up and verify exact bytes, then remove them; completed progress returns count and the retained backup name in summary.", o({ filenames: array(s(1024), 100000) }), operation, { mutating: true, longOperation: true }],
  ["restoreTrash", "Restore trashed media files.", empty, mutation({ count: i() }), { mutating: true, longOperation: false }],
  ["trash", "List media-trash file names and sizes before restore or deletion.", empty, o({ files: array(o({ filename: s(1024), bytes: i() }), 100000) }), { mutating: false, longOperation: false }],
]);
