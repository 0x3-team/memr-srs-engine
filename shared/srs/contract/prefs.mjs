import { areaMethods, boolean as b, empty, integer as i, object as o } from "./_schema.mjs";
import { mutation } from "./_shapes.mjs";
const preferences = o({ nextDayStartsAt: i(0, 23), learnAheadSeconds: i(0, 86400),
  timeboxSeconds: i(0, 86400), showIntervals: b, showRemainingCounts: b,
  backup: o({ intervalMinutes: i(1, 1440), daily: i(0, 999), weekly: i(0, 999), monthly: i(0, 999) }) });
export default areaMethods("prefs", [
  ["get", "Read collection scheduling, reviewer display and native backup preferences.", empty, preferences, { mutating: false, longOperation: false }],
  ["set", "Persist collection scheduling, reviewer display and native backup preferences.", o({ preferences }), mutation(), { mutating: true, longOperation: false }],
]);
