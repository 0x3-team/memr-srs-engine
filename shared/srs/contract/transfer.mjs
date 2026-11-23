import { areaMethods, array, boolean as b, enumeration as e, object as o, string as s } from "./_schema.mjs";
import { csvMapping as mapping, handle, id, operation, scope } from "./_shapes.mjs";
const updateMode = e("ifNewer", "always", "never");
const packageLimits = "Package admission uses a conservative 2 GiB expanded bound, 8 MiB zstd window, 16 MiB media map and ZIP directory, and 64 KiB metadata; large sets of tiny compressed media may exceed the conservative bound.";
export default areaMethods("transfer", [
  ["inspectPackage", `Inspect an uploaded package; completed progress returns packageInspection. ${packageLimits}`, o({ uploadId: handle }), operation, { mutating: false, longOperation: true }],
  ["importPackage", `Import an inspected package with note/type update and scheduling controls. ${packageLimits}`, o({ uploadId: handle, mergeNotetypes: b, updateNotes: updateMode, updateNotetypes: updateMode, includeScheduling: b, includeDeckConfigs: b }), operation, { mutating: true, longOperation: true }],
  ["inspectCsv", "Inspect CSV/TSV with exact row count and explicit preview truncation at 100 rows or 8 MiB; leading metadata is limited to 2 MiB and the native five-record sample to 64 MiB.", o({ uploadId: handle, delimiter: s(1), isHtml: b }, ["uploadId"]), operation, { mutating: false, longOperation: true }],
  ["importCsv", "Import mapped CSV/TSV fields with explicit duplicate handling; leading metadata is limited to 2 MiB and the native five-record sample to 64 MiB.", o({ uploadId: handle, delimiter: s(1), isHtml: b, notetypeId: id, deckId: id, mapping: array(mapping, 512), duplicateMode: e("update", "preserve", "new"), matchScope: e("notetype", "notetypeAndDeck"), tags: array(s(1024), 1000) }), operation, { mutating: true, longOperation: true }],
  ["exportPackage", "Export a deck/search package with scheduling, media and preset choices.", o({ scope, includeScheduling: b, includeMedia: b, includeDeckConfigs: b, legacy: b }, ["scope", "includeScheduling", "includeMedia", "includeDeckConfigs"]), operation, { mutating: false, longOperation: true }],
  ["exportCollection", "Export a full collection package suitable for backup and restore.", o({ includeMedia: b, legacy: b }, ["includeMedia"]), operation, { mutating: false, longOperation: true }],
  ["exportCsv", "Export notes or cards as CSV/TSV, including optional HTML, tags and ids.", o({ scope, mode: e("notes", "cards"), delimiter: s(1), includeHtml: b, includeTags: b, includeGuid: b, includeDeck: b, includeNotetype: b }), operation, { mutating: false, longOperation: true }],
]);
