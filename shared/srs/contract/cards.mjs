import { areaMethods, array, boolean as b, integer as i, object as o, string as s } from "./_schema.mjs";
import { card, cardRender, cardSelection, flag, id, ids, mutation, reviewLog } from "./_shapes.mjs";
export default areaMethods("cards", [
  ["info", "Read scheduling metadata and review history for a card.", o({ cardId: id }), o({ card, reviews: array(reviewLog, 100000) }), { mutating: false, longOperation: false }],
  ["preview", "Render a card outside the study queue without scheduling it.", o({ cardId: id, fillEmpty: b }, ["cardId"]), cardRender, { mutating: false, longOperation: false }],
  ["bury", "Bury selected cards or all sibling cards until the next day.", cardSelection, mutation({ count: i() }), { mutating: true, longOperation: false }],
  ["unbury", "Restore selected buried cards or their siblings.", cardSelection, mutation({ count: i() }), { mutating: true, longOperation: false }],
  ["suspend", "Suspend selected cards or all cards of the selected notes.", cardSelection, mutation({ count: i() }), { mutating: true, longOperation: false }],
  ["unsuspend", "Restore suspended selected cards or their siblings.", cardSelection, mutation({ count: i() }), { mutating: true, longOperation: false }],
  ["flag", "Set or clear one of Anki's seven card flags.", o({ cardIds: ids, flag }), mutation({ count: i() }), { mutating: true, longOperation: false }],
  ["forget", "Reset cards to new, optionally resetting review and lapse counters.", o({ cardIds: ids, resetCounts: b, restorePosition: b }, ["cardIds"]), mutation({ count: i() }), { mutating: true, longOperation: false }],
  ["setDueDate", "Set due dates with Anki's day-range syntax and optional interval reset.", o({ cardIds: ids, days: s(100), forceReset: b }, ["cardIds", "days"]), mutation({ count: i() }), { mutating: true, longOperation: false }],
  ["reposition", "Reposition new cards with start, step, shuffle and shift controls.", o({ cardIds: ids, start: i(), step: i(1), randomize: b, shiftExisting: b }), mutation({ count: i() }), { mutating: true, longOperation: false }],
  ["setDeck", "Move cards to a normal deck, restoring filtered-deck state.", o({ cardIds: ids, deckId: id }), mutation({ count: i() }), { mutating: true, longOperation: false }],
  ["remove", "Delete cards and notes which no longer have any cards.", o({ cardIds: ids }), mutation({ count: i() }), { mutating: true, longOperation: false }],
  ["setUserFlagNames", "Persist user-visible labels for seven flags.", o({ names: array(s(255), 7) }), mutation(), { mutating: true, longOperation: false }],
  ["userFlagNames", "Read the seven user-visible flag labels.", o({}), o({ names: array(s(255), 7) }), { mutating: false, longOperation: false }],
]);
