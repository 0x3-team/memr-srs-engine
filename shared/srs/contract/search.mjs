import { areaMethods, array, boolean as b, enumeration as e, object as o, string as s } from "./_schema.mjs";
import { ids } from "./_shapes.mjs";
export default areaMethods("search", [
  ["cards", "Search card ids using Anki search syntax and sorting.", o({ search: s(65536), sort: s(255), descending: b }, ["search"]), o({ cardIds: ids }), { mutating: false, longOperation: false }],
  ["notes", "Search note ids using Anki search syntax and sorting.", o({ search: s(65536), sort: s(255), descending: b }, ["search"]), o({ noteIds: ids }), { mutating: false, longOperation: false }],
  ["build", "Build, join or replace escaped native search expressions from browser quick filters.", o({ terms: array(o({ kind: e("deck", "notetype", "tag", "flag", "state", "added", "due", "text", "field", "raw"), value: s(65536), field: s(255), negate: b }, ["kind", "value"]), 1000), join: e("and", "or"), existingSearch: s(65536), operation: e("join", "replace") }, ["terms", "join"]), o({ search: s(65536) }), { mutating: false, longOperation: false }],
]);
