import { areaMethods, integer as i, object as o, string as s } from "./_schema.mjs";
import { id, ids, mutation, occlusionInput } from "./_shapes.mjs";
export default areaMethods("imageOcclusion", [
  ["getImage", "Read safe media image metadata for the occlusion canvas.", o({ filename: s(1024) }), o({ filename: s(1024), width: i(1, 100000), height: i(1, 100000) }), { mutating: false, longOperation: false }],
  ["addNote", "Create normalized masks and native text annotations. Text uses text plus optional fontSize (image-height ratio) and scale; angle uses native 1/10000-turn units. Text cannot contain the native closing cloze delimiter. Background annotations use inactive:true.", o({ deckId: id, notetypeId: id, occlusion: occlusionInput }), mutation({ noteId: id, cardIds: ids }), { mutating: true, longOperation: false }],
  ["getNote", "Read native masks, text, formatting and a content revision. Unsupported native properties fail explicitly before editing.", o({ noteId: id }), o({ noteId: id, notetypeId: id, occlusion: occlusionInput, revision: s(64) }, ["noteId", "notetypeId", "occlusion"]), { mutating: false, longOperation: false }],
  ["updateNote", "Update masks, text, groups and fields; an optional expectedRevision rejects concurrent changes atomically, including changes within one timestamp second.", o({ noteId: id, occlusion: occlusionInput, expectedRevision: s(64) }, ["noteId", "occlusion"]), mutation({ noteId: id, cardIds: ids }), { mutating: true, longOperation: false }],
]);
