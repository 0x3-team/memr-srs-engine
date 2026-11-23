import { array, boolean as b, enumeration as e, integer as i, nullable as n, number as f, object as o, string as s } from "./_schema.mjs";

export const id = i();
export const ids = array(id, 100000);
export const epochSeconds = i();
export const text = s(1048576);
export const name = s(255);
export const handle = s(256);
export const counts = o({ new: i(), learning: i(), review: i() });
export const changes = o(Object.fromEntries(["card", "note", "deck", "tag", "notetype", "config", "deckConfig", "mtime", "browserTable", "noteText", "studyQueues"].map((key) => [key, b])));
export const undo = o({ canUndo: b, undoLabel: n(name), canRedo: b, redoLabel: n(name), revision: s(256) }, ["canUndo", "undoLabel", "canRedo", "redoLabel"]);
export const mutation = (properties = {}, required = Object.keys(properties)) => o({ ...properties, changes, undo }, [...required, "changes", "undo"]);
export const operation = mutation({ operationId: handle });
export const rating = i(1, 4);
export const flag = i(0, 7);
export const queue = e(-3, -2, -1, 0, 1, 2, 3, 4);
export const source = o({ pageId: s(256), blockId: n(s(256)), revision: n(s(64)), uri: s(2048), changed: n(b) });
export const av = o({ kind: e("sound", "tts"), filename: n(s(1024)), text: n(text), language: n(name), voices: array(name, 32), speed: f(0.1, 10) });
export const nextState = o({ rating, label: name, secondsUntilDue: i() });
// ADR-03's exact top-level render payload. Null represents an absent optional concept.
export const cardRender = o({
  cardId: id, noteId: id, ord: i(0, 65535), deckId: id, queue,
  questionHtml: text, answerHtml: text, css: text, bodyClass: s(1024),
  av: o({ question: array(av, 128), answer: array(av, 128) }),
  typeAnswer: n(o({ field: name, correct: text, font: name, size: i(1, 200) })),
  autoplay: b, replayQuestionOnAnswer: b, timer: o({ show: b, limitSeconds: i(0, 9999) }),
  autoAdvance: o({ enabled: b, questionSeconds: f(0, 3600), answerSeconds: f(0, 3600), questionAction: e("showAnswer", "none"), answerAction: e("again", "hard", "good", "easy", "bury", "none"), waitForAudio: b }),
  nextStates: array(nextState, 4), counts, flags: flag, marked: b, source: n(source),
});
export const deck = o({ id, parentId: n(id), name: s(1024), description: text, collapsed: b, filtered: b, presetId: n(id), counts, totalCards: i() });
export const field = o({ name, ord: i(0, 65535), sticky: b, rtl: b, font: name, size: i(1, 200), plainText: b, collapsed: b, excludeFromSearch: b });
export const template = o({ name, ord: i(0, 65535), questionFormat: text, answerFormat: text, deckId: n(id), browserQuestionFormat: text, browserAnswerFormat: text });
export const notetype = o({ id, name, kind: e("normal", "cloze", "imageOcclusion"), fields: array(field, 512), templates: array(template, 256), css: text, sortField: i(0, 511), latexHeader: text, latexFooter: text, latexSvg: b, noteCount: i() });
export const note = o({ id, guid: s(64), notetypeId: id, fields: array(text, 512), tags: array(s(1024), 1000), cardIds: ids, modifiedAt: epochSeconds, source: n(source) });
export const memoryState = o({ stability: f(), difficulty: f(0, 10), retrievability: f(0, 1) });
export const reviewLog = o({ id, reviewedAt: epochSeconds, rating: i(0, 4), intervalDays: f(), lastIntervalDays: f(), ease: f(), elapsedSeconds: f(), kind: e("learning", "review", "relearning", "filtered", "manual", "rescheduled") });
// Native CardStatsResponse.due_position is signed int32; filtered new cards use negative positions.
export const card = o({ id, noteId: id, deckId: id, originalDeckId: n(id), ord: i(0, 65535), queue, type: e("new", "learning", "review", "relearning"), due: i(-Number.MAX_SAFE_INTEGER), originalDue: i(-Number.MAX_SAFE_INTEGER), intervalDays: f(), ease: f(), repetitions: i(), lapses: i(), flags: flag, position: n(i(-2147483648, 2147483647)), modifiedAt: epochSeconds, memoryState: n(memoryState) });
export const cardSelection = o({ cardIds: ids, scope: e("card", "note") }, ["cardIds"]);
export const scope = o({ deckId: n(id), search: n(s(65536)), includeChildren: b }, []);
export const preset = o({ id, name, deckCount: i() });
export const dailyLimits = o({ new: i(0, 9999), review: i(0, 9999), newIgnoresReviewLimit: b, limitsStartFromTop: b });
export const deckConfig = o({
  id, name,
  dailyLimits,
  newCards: o({ stepsMinutes: array(f(0, 1440), 32), graduatingIntervalDays: i(1, 36500), easyIntervalDays: i(1, 36500), insertionOrder: e("sequential", "random") }),
  lapses: o({ stepsMinutes: array(f(0, 1440), 32), minimumIntervalDays: i(1, 36500), leechThreshold: i(0, 1000), leechAction: e("suspend", "tagOnly") }),
  displayOrder: o({ newGather: e("deck", "deckThenRandomNotes", "ascendingPosition", "descendingPosition", "randomNotes", "randomCards"), newSort: e("templateThenPosition", "templateThenRandom", "position", "randomNoteThenTemplate", "random"), newReview: e("mix", "afterReviews", "beforeReviews"), interdayLearningReview: e("mix", "afterReviews", "beforeReviews"), reviewSort: e("due", "dueThenDeck", "deckThenDue", "intervalAscending", "intervalDescending", "easeAscending", "easeDescending", "retrievabilityAscending", "retrievabilityDescending", "random") }),
  fsrs: o({ enabled: b, desiredRetention: f(0.7, 0.99), parameters: array(f(-100, 100), 64), historicalRetention: f(0.7, 1), rescheduleOnChange: b, ignoreReviewsBefore: epochSeconds }),
  burying: o({ new: b, review: b, interdayLearning: b }),
  audio: o({ autoplay: b, replayQuestionOnAnswer: b }),
  timer: o({ maximumAnswerSeconds: i(0, 3600), show: b, stopOnAnswer: b }),
  autoAdvance: cardRender.properties.autoAdvance,
  easyDays: array(f(0, 1), 7),
  advanced: o({ maximumIntervalDays: i(1, 36500), startingEase: f(1.3, 10), easyBonus: f(0, 10), intervalModifier: f(0, 10), hardInterval: f(0, 10), newInterval: f(0, 1), customScheduling: text }),
});
export const preferences = o({ nextDayStartsAt: i(0, 23), learnAheadSeconds: i(0, 86400), timeboxSeconds: i(0, 86400), showIntervals: b, showRemainingCounts: b, autoplay: b, keyboardHints: b, reviewerFontSize: i(8, 96), defaultDeckId: n(id), defaultNotetypeId: n(id), backup: o({ enabled: b, intervalMinutes: i(1, 1440), daily: i(0, 999), weekly: i(0, 999), monthly: i(0, 999) }) });
export const column = o({ key: name, label: name, sortable: b, alignment: e("start", "end", "center") });
export const savedSearch = o({ name, search: s(65536) });
export const browserRow = o({ id, cardId: n(id), noteId: id, cells: array(s(65536), 64), flags: flag, marked: b, suspended: b, buried: b });
export const tag = o({ name: s(1024), parent: n(s(1024)), level: i(0, 100), collapsed: b, noteCount: i() });
export const graphPoint = o({ x: f(-Number.MAX_VALUE), y: f(), label: s(255) });
export const graph = o({ key: e("futureDue", "calendar", "reviews", "cardCounts", "intervals", "ease", "difficulty", "stability", "retrievability", "hourly", "answerButtons", "added"), label: name, unit: name, series: array(o({ name, points: array(graphPoint, 10000) }), 16) });
export const today = o({ cards: i(), reviews: i(), elapsedSeconds: f(), averageSeconds: f(), againCount: i(), correctCount: i() });
export const retention = o({ label: name, passed: i(), failed: i(), retention: n(f(0, 1)) });
export const graphPreferences = o({ firstDayOfWeek: i(0, 6), showBacklog: b, separateInactive: b, browserLinks: b });
export const simulation = o({ days: array(o({ day: i(), reviews: f(), memorized: f(), costSeconds: f() }), 36500), totalReviews: f(), totalCostSeconds: f(), averageRetention: f(0, 1) });
export const evaluation = o({ logLoss: f(), rmse: f(), reviewCount: i(), parameters: array(f(-100, 100), 64) });
export const error = o({ code: e("invalid_params", "not_found", "busy", "conflict", "engine_unavailable", "engine_failed", "unsupported", "aborted", "internal"), message: s(4096), data: o({ method: s(255), field: s(1024), operationId: handle, retryAfterSeconds: f() }, []) }, ["code", "message"]);
export const exportFile = o({ downloadToken: handle, filename: s(1024), bytes: i(), expiresAt: epochSeconds });
export const csvMapping = o({ column: i(0, 511), target: e("field", "tags", "deck", "notetype", "guid", "ignore"), fieldOrdinal: n(i(0, 511)) });
export const packageInspection = o({ kind: e("apkg", "colpkg"), notes: i(), cards: i(), decks: i(), notetypes: i(), media: i(), schedulingIncluded: b, warnings: array(s(4096), 10000), availableOptions: array(s(255), 16) });
export const csvInspection = o({ delimiter: s(1), columns: array(s(255), 512), rows: array(array(s(65536), 512), 100), totalRows: i(), notetypeId: n(id), deckId: n(id), mapping: array(csvMapping, 512), isHtml: b, previewTruncated: b, warnings: array(s(4096), 10000) });
export const emptyCards = o({ notes: array(o({ noteId: id, cardIds: ids, allCardsEmpty: b }), 100000) });
export const fsrsOptimization = o({ presetId: id, parameters: array(f(-100, 100), 64), trainingItems: i(), healthCheckPassed: n(b), search: s(65536) });
export const fsrsOptimizations = array(fsrsOptimization, 10000);
export const fsrsOptimizationFailures = array(o({ presetId: id, message: s(4096) }), 10000);
export const fsrsEvaluation = o({ logLoss: f(), rmse: f(0, 1), mode: e("parameters", "timeSeries"), parameters: n(array(f(-100, 100), 64)) });
export const fsrsSimulation = o({ days: array(o({ day: i(), reviews: i(), newCards: i(), memorizedCards: f(), costSeconds: f() }), 36500), totalReviews: i(), totalNewCards: i(), totalCostSeconds: f() });
export const fsrsOptimalRetention = o({ desiredRetention: f(0, 1) });
export const fsrsRetentionWorkload = array(o({ retention: f(0, 1), cost: f() }), 101);
export const fsrsWorkload = o({ reviewlessMemorizedCards: f(), points: array(o({ retention: f(0, 1), costSeconds: f(), memorizedCards: f(), reviews: i() }), 101) });
// Long calls acknowledge an operation; progress.get supplies the terminal outcome.
export const operationOutcome = mutation({ summary: s(65536), affectedIds: ids, search: s(65536), file: exportFile, evaluation, simulation, parameters: array(f(-100, 100), 64), desiredRetention: f(0.7, 0.99), missing: array(s(1024), 100000), unused: array(s(1024), 100000), warnings: array(s(4096), 10000), imported: o({ notes: i(), cards: i(), decks: i(), notetypes: i(), media: i(), updated: i(), skipped: i() }), syncRequired: e("none", "upload", "download", "choose"), packageInspection, csvInspection, emptyCards, count: i(), groups: array(o({ text: s(65536), noteIds: ids }), 10000), tags: array(tag, 100000), fsrsOptimization, fsrsOptimizations, fsrsOptimizationFailures, fsrsEvaluation, fsrsSimulation, fsrsOptimalRetention, fsrsRetentionWorkload, fsrsWorkload }, []);
export const progress = o({ operationId: n(handle), kind: n(name), status: e("idle", "running", "completed", "aborted", "failed"), done: i(), total: n(i()), label: s(4096), abortable: b, result: n(operationOutcome), error: n(error) });
export const uploadResult = o({ uploadId: handle, bytes: i() }, ["uploadId"]);
export const point = o({ x: f(0, 1), y: f(0, 1) });
export const occlusion = o({ id: name, kind: e("rectangle", "ellipse", "polygon", "text"), left: f(0, 1), top: f(0, 1), width: f(0, 1), height: f(0, 1), points: array(point, 10000), group: n(name), label: s(4096), inactive: b,
  text: s(4096), fontSize: f(.0001, 10), scale: f(.0001, 100), angle: f(-10000, 10000), fill: s(255),
}, ["id", "kind", "left", "top", "width", "height", "points", "group", "label", "inactive"]);
export const occlusionInput = o({ image: s(1024), masks: array(occlusion, 10000), mode: e("hideAllGuessOne", "hideOneGuessOne"), header: text, backExtra: text, tags: array(s(1024), 1000) });
export const syncStatus = o({ loggedIn: b, endpoint: n(s(2048)), username: n(name), lastSyncAt: n(epochSeconds), required: e("none", "normal", "full", "upload", "download"), local: o({ notes: i(), cards: i(), modifiedAt: epochSeconds }), remote: n(o({ notes: i(), cards: i(), modifiedAt: epochSeconds })), mediaPending: n(i()) });
export const syncServerStatus = o({ running: b, loopbackUrl: n(s(2048)), users: array(o({ username: name }), 1000), phoneReachabilityEnabled: b });
