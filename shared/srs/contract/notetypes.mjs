import { areaMethods, array, empty, enumeration as e, integer as i, nullable as n, object as o, string as s } from "./_schema.mjs";
import { cardRender, id, ids, mutation, notetype, operation, text } from "./_shapes.mjs";
const stockKind = e("basic", "basicReversed", "basicOptionalReversed", "typeIn", "cloze", "imageOcclusion");
// Array order is the desired order; ord retains an existing item's identity.
// A null ordinal creates a field or template instead of reusing stored content.
const editableNotetype = o({ ...notetype.properties,
  fields: array(o({ ...notetype.properties.fields.items.properties, ord: n(i(0, 511)) }), 512),
  templates: array(o({ ...notetype.properties.templates.items.properties, ord: n(i(0, 255)) }), 256),
});
export default areaMethods("notetypes", [
  ["list", "List note types with kinds and note-use counts.", empty, o({ notetypes: array(o({ id, name: s(255), kind: notetype.properties.kind, noteCount: i() }), 10000) }), { mutating: false, longOperation: false }],
  ["get", "Read fields, templates, stylesheet and note-type options.", o({ notetypeId: id }), notetype, { mutating: false, longOperation: false }],
  ["add", "Create from exactly one complete definition or stockKind; name overrides only a stock definition.", o({ notetype: editableNotetype, stockKind, name: s(255) }, []), mutation({ notetypeId: id }), { mutating: true, longOperation: false }],
  ["clone", "Clone a note type's fields, templates and styling.", o({ notetypeId: id, name: s(255) }), mutation({ notetypeId: id }), { mutating: true, longOperation: false }],
  ["update", "Update fields, templates and styling in one native undo step; await the terminal progress result. Array order is final, ord is the old ordinal or null for a new item, and sortField is the final field index. Selecting a new field before the last position is unsupported before writing when its index matches a retained old ordinal: save the schema with the last field as sortField, reload, then select the new sort field.", o({ notetype: editableNotetype }), operation, { mutating: true, longOperation: true }],
  ["rename", "Rename a note type without changing its identity.", o({ notetypeId: id, name: s(255) }), mutation(), { mutating: true, longOperation: false }],
  ["remove", "Remove a note type and its notes only after explicit confirmation; the terminal progress result includes the removed note count.", o({ notetypeId: id, deleteNotes: { type: "boolean" } }), operation, { mutating: true, longOperation: true }],
  ["stock", "Return stock definitions for Basic, reversed, optional reversed, type-in, Cloze and image occlusion.", o({ kind: stockKind }, []), o({ notetypes: array(notetype, 16) }), { mutating: false, longOperation: false }],
  ["previewTemplate", "Render unsaved edits without changing the active collection. templateOrdinal is the final template index or zero-based cloze number. A supplied cardId must match the selected template's retained ord or cloze number; a new normal template has no existing card identity. With noteId alone, use its matching existing card when available, otherwise native uncommitted sample/default context. Fields may be explicit, taken from the note, or native samples.", o({ notetype: editableNotetype, fields: array(text, 512), templateOrdinal: i(0, 499), noteId: id, cardId: id }, ["notetype", "templateOrdinal"]), cardRender, { mutating: false, longOperation: false }],
  ["changeNotetype", "Convert notes of one source type; the terminal progress result includes the converted note count. Maps list a source ordinal per destination, -1 means blank/new, and templateMap must be empty for cloze conversions.", o({ noteIds: ids, notetypeId: id, fieldMap: array(i(-1, 511), 512), templateMap: array(i(-1, 255), 256) }), operation, { mutating: true, longOperation: true }],
]);
