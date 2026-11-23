import { areaMethods, array, boolean as b, empty, enumeration as e, integer as i, nullable as n, object as o, string as s } from "./_schema.mjs";
import { browserRow, column, mutation, savedSearch } from "./_shapes.mjs";
const nativeBrowserRow = o({ ...browserRow.properties, color: e("default", "marked", "suspended", "flagRed", "flagOrange", "flagGreen", "flagBlue", "flagPink", "flagTurquoise", "flagPurple", "buried") });
export default areaMethods("browser", [
  ["columns", "List available and selected columns for cards or notes mode.", o({ mode: e("cards", "notes") }), o({ available: array(column, 64), selected: array(s(255), 64), defaults: array(s(255), 64) }), { mutating: false, longOperation: false }],
  ["setColumns", "Persist table column order for cards or notes mode.", o({ mode: e("cards", "notes"), columns: array(s(255), 64) }), mutation(), { mutating: true, longOperation: false }],
  ["page", "Read a native browser page in the selected layout mode, with a session cursor bound to search, sorting and revision.", o({ search: s(65536), mode: e("cards", "notes"), columns: array(s(255), 64), sort: s(255), descending: b, limit: i(1, 500), cursor: n(s(4096)), offset: i() }, ["search", "mode", "columns", "limit"]), o({ rows: array(nativeBrowserRow, 500), nextCursor: n(s(4096)), total: i(), revision: s(256) }), { mutating: false, longOperation: false }],
  ["savedSearches", "Read named browser searches.", empty, o({ searches: array(savedSearch, 1000) }), { mutating: false, longOperation: false }],
  ["saveSearch", "Create or replace a named saved search.", savedSearch, mutation(), { mutating: true, longOperation: false }],
  ["removeSavedSearch", "Delete a named saved search.", o({ name: s(255) }), mutation(), { mutating: true, longOperation: false }],
  ["getLayout", "Read persisted browser mode and detail-pane geometry.", empty, o({ mode: e("cards", "notes"), sidebarWidth: i(120, 1000), detailWidth: i(240, 2000), detailVisible: b }), { mutating: false, longOperation: false }],
  ["setLayout", "Change browser mode or pane geometry in one native undoable write; change both with two explicit requests.", o({ mode: e("cards", "notes"), sidebarWidth: i(120, 1000), detailWidth: i(240, 2000), detailVisible: b }), mutation(), { mutating: true, longOperation: false }],
]);
