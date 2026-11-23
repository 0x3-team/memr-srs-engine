import { areaMethods, array, boolean as b, empty, enumeration as e, integer as i, nullable as n, object as o, string as s } from "./_schema.mjs";
export default areaMethods("engine", [
  ["status", "Report engine availability, collection state and public versions.", empty, o({ state: e("missing", "starting", "ready", "busy", "failed", "stopped"), engineVersion: n(s(255)), contractVersion: i(1, 1), collectionOpen: b, mediaPrefix: n(s(2048)), message: n(s(4096)) }), { mutating: false, longOperation: false }],
  ["describe", "List the versioned methods implemented by this engine.", empty, o({ version: i(1, 1), methods: array(o({ name: s(255), description: s(4096), mutating: b, longOperation: b }), 1000) }), { mutating: false, longOperation: false }],
]);
