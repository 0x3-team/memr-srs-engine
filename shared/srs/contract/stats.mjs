import { areaMethods, array, boolean as b, empty, enumeration as e, integer as i, nullable as n, number as f, object as o, string as s } from "./_schema.mjs";
import { card, graph, id, mutation, retention, reviewLog, scope, today } from "./_shapes.mjs";

export const memory = o({ stability: f(), difficulty: f(0, 10), retrievability: n(f(0, 1)) });
const counts = o({ new: i(), learning: i(), relearning: i(), young: i(), mature: i(), suspended: i(), buried: i() });
const retentionCounts = o({ youngPassed: i(), youngFailed: i(), maturePassed: i(), matureFailed: i() });
const windows = (shape) => o({ month: shape, quarter: shape, year: shape, all: shape });
const histogram = (key, signed = false) => array(o({ [key]: i(signed ? -2147483648 : 0), count: i() }), 10000);
const reviewBuckets = array(o({ dayOffset: i(-2147483648), learning: i(), relearning: i(), young: i(), mature: i(), filtered: i() }), 10000);
export const graphData = o({
  rolloverHour: i(0, 23), fsrsEnabled: b,
  todayNative: o({ answerCount: i(), answerMilliseconds: i(), correctCount: i(), matureCorrect: i(), matureCount: i(), learningCount: i(), reviewCount: i(), relearningCount: i(), earlyReviewCount: i() }),
  futureDue: o({ buckets: histogram("dayOffset", true), haveBacklog: b, dailyLoad: i() }),
  added: histogram("dayOffset", true), intervals: histogram("days"), stability: histogram("days"),
  ease: o({ buckets: histogram("percent"), medianPercent: f() }),
  difficulty: o({ buckets: histogram("percent"), medianPercent: f() }),
  retrievability: o({ buckets: histogram("percent"), averagePercent: f(), sumByCard: f(), sumByNote: f() }),
  reviews: o({ counts: reviewBuckets, timeMilliseconds: reviewBuckets }),
  cardCounts: o({ includingInactive: counts, separateInactive: counts }),
  hours: windows(array(o({ hour: i(0, 23), total: i(), correct: i() }), 24)),
  answerButtons: windows(o({ learning: array(i(), 4), young: array(i(), 4), mature: array(i(), 4) })),
  trueRetention: o({ today: retentionCounts, yesterday: retentionCounts, week: retentionCounts, month: retentionCounts, year: retentionCounts, all: retentionCounts }),
});
const graphPreferences = o({ firstDayOfWeek: e(0, 1, 5, 6), showBacklog: b, separateInactive: b, browserLinks: b });
const nativeMemory = n(o({ stability: f(), difficulty: f(0, 10) }));
const details = o({ deck: s(1024), addedAt: i(), firstReviewAt: n(i()), latestReviewAt: n(i()), dueAt: n(i()), duePosition: n(i(-2147483648)),
  averageSeconds: f(), totalSeconds: f(), cardType: s(4096), notetype: s(255), customData: s(1048576), preset: s(255), originalDeck: n(s(1024)), desiredRetention: n(f(0, 1)), parameters: array(f(-100, 100), 64),
  reviewHistory: array(o({ reviewedAt: i(), kind: e("learning", "review", "relearning", "filtered", "manual", "rescheduled"), rating: i(0, 4), intervalSeconds: i(), lastIntervalSeconds: i(), easePerMill: i(), elapsedSeconds: f(), memoryState: nativeMemory }), 100000) });
export default areaMethods("stats", [
  ["graphs", "Return every native graph field, window and retention split plus chart projections for a scope and period.", o({ scope, period: e("month", "quarter", "year", "all") }), o({ today, graphs: array(graph, 16), trueRetention: array(retention, 16), data: graphData }), { mutating: false, longOperation: false }],
  ["graphPreferences", "Read statistics graph display preferences.", empty, graphPreferences, { mutating: false, longOperation: false }],
  ["setGraphPreferences", "Persist native graph preferences with undo; browserLinks is a read-only capability.", o({ preferences: graphPreferences }), mutation(), { mutating: true, longOperation: false }],
  ["cardStats", "Read bounded native card statistics and full review history without caching writes to the source collection.", o({ cardId: id }), o({ card: o({ ...card.properties, memoryState: n(memory) }), memoryState: n(memory), reviews: array(reviewLog, 100000), details }), { mutating: false, longOperation: false }],
  ["today", "Read today's statistics for a deck or the whole collection.", scope, today, { mutating: false, longOperation: false }],
]);
