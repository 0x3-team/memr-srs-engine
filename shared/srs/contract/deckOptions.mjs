import { areaMethods, array, boolean as b, enumeration as e, integer as i, nullable as n, number as f, object as o, string as s } from "./_schema.mjs";
import { deckConfig, id, ids, mutation, operation, preset } from "./_shapes.mjs";

export const reviewOrder = e("due", "dueThenDeck", "deckThenDue", "intervalAscending", "intervalDescending", "easeAscending", "easeDescending", "retrievabilityAscending", "random", "added", "reverseAdded", "retrievabilityDescending", "relativeOverdueness");
const { enabled: _enabled, rescheduleOnChange: _reschedule, ignoreReviewsBefore: _cutoff, ...fsrsProperties } = deckConfig.properties.fsrs.properties;
// Collection flags and action-only rescheduling are kept outside the preset.
// Anki stores a calendar date, not an arbitrary instant, for the history cutoff.
export const optionsConfig = o({ ...deckConfig.properties,
  newCards: o({ ...deckConfig.properties.newCards.properties, stepsMinutes: array(f(1 / 60, 35791396), 32) }),
  lapses: o({ ...deckConfig.properties.lapses.properties, stepsMinutes: array(f(1 / 60, 35791396), 32), leechThreshold: i(1, 9999) }),
  displayOrder: o({ ...deckConfig.properties.displayOrder.properties, reviewSort: reviewOrder }),
  fsrs: o({ ...fsrsProperties, desiredRetention: f(Math.fround(0.7), Math.fround(0.99)), historicalRetention: f(Math.fround(0.7), Math.fround(0.97)), parameters4: array(f(-100, 100), 64), parameters5: array(f(-100, 100), 64),
    ignoreReviewsBeforeDate: s(10), parameterSearch: s(65536) }),
  timer: o({ maximumAnswerSeconds: i(1, 9999), show: b, stopOnAnswer: b }),
  autoAdvance: o({ questionSeconds: f(0, 9999), answerSeconds: f(0, 9999),
    questionAction: e("showAnswer", "showReminder"), answerAction: e("bury", "again", "good", "hard", "showReminder"), waitForAudio: b }),
  easyDays: array(e(0, 0.5, 1), 7),
  advanced: o({ ...deckConfig.properties.advanced.properties, startingEase: f(Math.fround(1.31), 5),
    easyBonus: f(1, 5), hardInterval: f(0.5, 1.3), intervalModifier: f(0.5, 2) }),
});
export const deckLimits = n(o({ new: n(i(0, 9999)), review: n(i(0, 9999)), desiredRetention: n(f(Math.fround(0.7), Math.fround(0.99))) }));
export const todayLimits = n(o({ new: n(i(0, 9999)), review: n(i(0, 9999)), newActive: b, reviewActive: b }));
export default areaMethods("deckOptions", [
  ["get", "Read every native preset, defaults, collection flags and independent deck/today overrides.", o({ deckId: id }), o({ deckId: id, config: optionsConfig, presets: array(preset, 10000), presetConfigs: array(optionsConfig, 10000), defaults: optionsConfig,
    deckLimits, todayLimits, parentPresetIds: array(id, 10000), fsrsEnabled: b, fsrsHealthCheck: b,
    fsrsLegacyEvaluate: b, schemaModified: b, daysSinceLastFsrsOptimize: i() }), { mutating: false, longOperation: false }],
  ["update", "Save a native preset and independent overrides, optionally applying to subdecks and rescheduling.", o({ deckId: id, config: optionsConfig, deckLimits, todayLimits, applyToChildren: b, fsrsEnabled: b, fsrsHealthCheck: b, reschedule: b },
    ["deckId", "config", "deckLimits", "todayLimits", "applyToChildren", "fsrsEnabled"]), operation, { mutating: true, longOperation: true }],
  ["presetAdd", "Create a named preset with defaults.", o({ name: s(255) }), mutation({ presetId: id }), { mutating: true, longOperation: false }],
  ["presetClone", "Clone a preset under a new name.", o({ presetId: id, name: s(255) }), mutation({ presetId: id }), { mutating: true, longOperation: false }],
  ["presetRename", "Rename a deck-options preset.", o({ presetId: id, name: s(255) }), mutation(), { mutating: true, longOperation: false }],
  ["presetRemove", "Delete a preset and explicitly reassign its decks.", o({ presetId: id, replacementPresetId: id }), mutation(), { mutating: true, longOperation: false }],
  ["presetAssign", "Assign a preset to selected decks.", o({ deckIds: ids, presetId: id }), mutation(), { mutating: true, longOperation: false }],
]);
