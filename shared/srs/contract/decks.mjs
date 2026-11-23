import { areaMethods, array, boolean as b, empty, integer as i, nullable as n, object as o, string as s } from "./_schema.mjs";
import { deck, id, ids, mutation, today } from "./_shapes.mjs";
const deckView = o({ ...deck.properties, browserCollapsed: b, descriptionMarkdown: b }, deck.required);
export default areaMethods("decks", [
  ["tree", "Return flattened nested decks with parent ids and study counts.", o({ topDeckId: n(id) }, []), o({ decks: array(deckView, 10000), currentDeckId: n(id) }), { mutating: false, longOperation: false }],
  ["get", "Read a deck's description, collapse states and scheduler counts.", o({ deckId: id }), deckView, { mutating: false, longOperation: false }],
  ["create", "Create a deck, creating missing parent decks by name.", o({ name: s(1024), parentId: n(id) }, ["name"]), mutation({ deckId: id }), { mutating: true, longOperation: false }],
  ["rename", "Rename a deck and its descendants.", o({ deckId: id, name: s(1024) }), mutation(), { mutating: true, longOperation: false }],
  ["reparent", "Move decks under a parent or to the root.", o({ deckIds: ids, parentId: n(id) }), mutation(), { mutating: true, longOperation: false }],
  ["remove", "Remove decks, their cards and orphan notes; preserving cards is unsupported.", o({ deckIds: ids, deleteCards: b }), mutation({ removedCount: i() }), { mutating: true, longOperation: false }],
  ["setCollapsed", "Persist a deck's expanded state in study or browser context.", o({ deckId: id, collapsed: b, browser: b }, ["deckId", "collapsed"]), mutation(), { mutating: true, longOperation: false }],
  ["setCurrent", "Select the current deck and reset study queues.", o({ deckId: id }), mutation(), { mutating: true, longOperation: false }],
  ["setDescription", "Update a deck's description and optional Markdown mode without normalizing its content.", o({ deckId: id, description: s(1048576), markdown: b }, ["deckId", "description"]), mutation(), { mutating: true, longOperation: false }],
  ["todaySummary", "Return today's review attempts (cards and reviews) and elapsed time.", empty, today, { mutating: false, longOperation: false }],
]);
