import { areaMethods, array, boolean as b, empty, enumeration as e, integer as i, object as o, string as s } from "./_schema.mjs";
import { id, mutation, name, operation } from "./_shapes.mjs";
export const filteredConfig = o({ deckId: id, name, terms: array(o({ search: s(65536), limit: i(1, 100000), order: e("oldest", "random", "intervalAscending", "intervalDescending", "lapses", "added", "due", "reverseAdded", "retrievabilityAscending", "retrievabilityDescending", "relativeOverdueness") }), 2), reschedule: b, stepsMinutes: array({ type: "number", minimum: 0, maximum: 1440 }, 32), previewAgainSeconds: i(0, 86400), previewHardSeconds: i(0, 86400), previewGoodSeconds: i(0, 86400) });
export default areaMethods("filtered", [
  ["defaults", "Read default settings for a new filtered deck.", empty, filteredConfig, { mutating: false, longOperation: false }],
  ["get", "Read an existing filtered deck's terms and preview delays.", o({ deckId: id }), filteredConfig, { mutating: false, longOperation: false }],
  ["createOrUpdate", "Create or update filtered-deck searches and scheduling options.", o({ config: filteredConfig }), mutation({ deckId: id }), { mutating: true, longOperation: false }],
  ["rebuild", "Refill a filtered deck; terminal progress supplies its native mutation result and selected count.", o({ deckId: id }), operation, { mutating: true, longOperation: true }],
  ["empty", "Return filtered cards to their original decks; terminal progress supplies its native mutation result and returned count.", o({ deckId: id }), operation, { mutating: true, longOperation: true }],
]);
