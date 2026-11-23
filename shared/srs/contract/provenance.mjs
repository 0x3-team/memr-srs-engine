import { areaMethods, array, empty, enumeration as e, nullable as n, object as o, string as s } from "./_schema.mjs";
import { id, ids, mutation, source } from "./_shapes.mjs";
const checkedSource = o({ ...source.properties, status: e("current", "changed", "missing", "untracked", "unchecked") });
export default areaMethods("provenance", [
  ["ensureNotetypes", "Create Memr Basic, reversed, Cloze and Type-in types with a hidden Source field.", empty, mutation({ notetypeIds: ids }), { mutating: true, longOperation: false }],
  ["sourceOf", "Read source links preserved in a note's hidden Source field.", o({ noteId: id }), o({ sources: array(source, 1000) }), { mutating: false, longOperation: false }],
  ["cardsForSource", "Find cards by canonical page and optional block identity.", o({ pageId: s(256), blockId: s(256) }, ["pageId"]), o({ cardIds: ids, noteIds: ids }), { mutating: false, longOperation: false }],
  ["checkSources", "Compare stored revisions with current canonical hashes; null marks a missing source and cards remain unchanged.", o({ sources: array(o({ pageId: s(256), blockId: n(s(256)), revision: n(s(64)) }), 10000) }), o({ notes: array(o({ noteId: id, sources: array(checkedSource, 1000) }), 10000) }), { mutating: false, longOperation: false }],
]);
