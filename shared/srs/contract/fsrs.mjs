import { areaMethods, array, boolean as b, enumeration as e, integer as i, nullable as n, number as f, object as o, string as s } from "./_schema.mjs";
import { epochSeconds, id, mutation, operation } from "./_shapes.mjs";
import { reviewOrder } from "./deckOptions.mjs";
const data = o({ search: s(65536), ignoreReviewsBefore: epochSeconds, parameters: array(f(-100, 100), 64) }, ["search"]);
const preferences = o({ loadBalancerEnabled: b, shortTermWithStepsEnabled: b });
const settings = { parameters: array(f(-100, 100), 64), desiredRetention: f(Math.fround(0.7), Math.fround(0.99)), days: i(1, 36500),
  newCardsPerDay: i(0, 4294967295), reviewLimit: i(0, 4294967295), maximumIntervalDays: i(1, 36500), easyDays: array(e(0, 0.5, 1), 7),
  additionalNewCards: i(0, 100000), newIgnoresReviewLimit: b, reviewOrder, suspendAfterLapses: n(i(1, 1000)),
  historicalRetention: f(Math.fround(0.7), Math.fround(0.97)), learningSteps: i(0, 32), relearningSteps: i(0, 32) };
const simulation = o({ deckId: id, ...settings }, ["deckId", "parameters", "desiredRetention", "days", "newCardsPerDay", "reviewLimit", "maximumIntervalDays", "easyDays"]);
export default areaMethods("fsrs", [
  ["getPreferences", "Read native collection-wide load balancing and short-term FSRS switches.", o({}), preferences, { mutating: false, longOperation: false }],
  ["setPreferences", "Save native load balancing and short-term FSRS switches as one undoable change, optionally guarding the previous values.", o({ ...preferences.properties, expected: preferences }, ["loadBalancerEnabled", "shortTermWithStepsEnabled"]), mutation(), { mutating: true, longOperation: false }],
  ["optimizeAll", "Optimize each saved preset using its search and cutoff, reporting successes and failures individually.", o({ deckId: id, reschedule: b }, ["deckId"]), operation, { mutating: true, longOperation: true }],
  ["historyCount", "Count the native included and total reviews for a search and history cutoff.", o({ search: s(65536), ignoreReviewsBeforeDate: s(10) }), o({ included: i(), total: i() }), { mutating: false, longOperation: false }],
  ["retentionWorkload", "Compute Anki's native relative workload curve for desired retention.", o({ search: s(65536), parameters: array(f(-100, 100), 64) }), operation, { mutating: false, longOperation: true }],
  ["simulateWorkload", "Simulate native workload, reviews and memorized cards across retention targets on a disposable collection.", simulation, operation, { mutating: false, longOperation: true }],
  ["optimize", "Optimize FSRS parameters against selected review history.", o({ presetId: id, search: s(65536), ignoreReviewsBefore: epochSeconds, reschedule: b }, ["presetId", "search"]), operation, { mutating: true, longOperation: true }],
  ["evaluate", "Evaluate explicit parameters or native time-series cross-validation; report the actual mode.", data, operation, { mutating: false, longOperation: true }],
  ["simulate", "Simulate daily reviews, new cards, memorized cards and time cost on a disposable collection.", simulation, operation, { mutating: false, longOperation: true }],
  ["optimalRetention", "Estimate native optimal retention with optional complete simulation settings; no history cutoff is implied.", o({ search: s(65536), parameters: array(f(-100, 100), 64), settings: o(settings) }, ["search"]), operation, { mutating: false, longOperation: true }],
  ["memoryState", "Compute native FSRS memory and retrievability without persisting scheduling caches.", o({ cardId: id }), n(o({ stability: f(), difficulty: f(0, 10), retrievability: n(f(0, 1)), desiredRetention: f(0, 1), decay: f(0, 1) })), { mutating: false, longOperation: false }],
]);
